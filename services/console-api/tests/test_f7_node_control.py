import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_s12_owner_metrics import owner as make_owner

from console_api.isolated_node_control import Store, router
from console_api.node_control_ledger import Ledger


@pytest.fixture
def ledger():
    value = Ledger()
    value.seed("node", ["alice", "bob"])
    yield value
    value.close()


def prepare(store, action="drain", generation=0, actor="alice"):
    return store.prepare(actor, "node", action, generation, str(uuid4()))


def evidence(op, outcome="drained"):
    return dict(
        attempt=op["id"],
        node="node",
        action=op["action"],
        generation=op["generation"],
        boot_id="synthetic-boot",
        actor=op["actor"],
        version=1,
        outcome=outcome,
    )


def test_drain_ack_resume_and_unknown_recovery(ledger):
    op = prepare(ledger)
    assert ledger.recover("alice") == [op]  # prepare response discarded, no original form needed
    assert ledger.recover("bob") == []
    op = ledger.execute("alice", op["id"])
    assert ledger.execute("alice", op["id"]) == op
    with pytest.raises(HTTPException):
        ledger.ack("alice", op["id"], op)
    op = ledger.reconcile(op["id"], evidence(op))
    assert ledger.reconcile(op["id"], evidence(op)) == op
    ledger.ack("alice", op["id"], op)
    with ledger.transaction(write=False) as db:
        assert ledger.read(db, "nodes", "node")["state"] == "DRAINED"
    with pytest.raises(HTTPException):
        prepare(ledger, "resume", 1, "bob")
    resumed = prepare(ledger, "resume", 1)
    resumed = ledger.execute("alice", resumed["id"])
    resumed = ledger.reconcile(resumed["id"], evidence(resumed, "serving"))
    ledger.ack("alice", resumed["id"], resumed)
    assert ledger.recover("alice") == []


def test_withdraw_requires_separate_ack(ledger):
    op = prepare(ledger)
    op = ledger.withdraw("alice", op["id"])
    assert ledger.execute("alice", op["id"]) == op
    with pytest.raises(HTTPException):
        prepare(ledger)
    ledger.ack("alice", op["id"], op)
    assert prepare(ledger)["status"] == "PREPARED"


def test_parallel_preparation_has_one_winner(ledger):
    gate = Barrier(2)

    def run(actor):
        gate.wait()
        try:
            return prepare(ledger, actor=actor)["status"]
        except HTTPException as exc:
            assert exc.status_code == 409
            return "conflict"

    with ThreadPoolExecutor(2) as pool:
        a, b = pool.submit(run, "alice"), pool.submit(run, "bob")
        assert sorted([a.result(), b.result()]) == ["PREPARED", "conflict"]


def test_execute_withdraw_compete(ledger):
    op = prepare(ledger)
    gate = Barrier(2)

    def run(fn):
        gate.wait()
        try:
            return fn("alice", op["id"])["status"]
        except HTTPException:
            return "conflict"

    with ThreadPoolExecutor(2) as pool:
        a, b = pool.submit(run, ledger.execute), pool.submit(run, ledger.withdraw)
        results = [a.result(), b.result()]
    row = ledger.recover("alice")[0]
    assert row["status"] in {"CANCELLED", "EXECUTING"}
    assert not ("CANCELLED" in results and "EXECUTING" in results)


@pytest.mark.parametrize(
    "field,value", [("node", "other"), ("generation", 0), ("generation", True), ("boot_id", "old"), ("version", 2), ("action", "resume")]
)
def test_mismatched_evidence_keeps_lock(ledger, field, value):
    op = ledger.execute("alice", prepare(ledger)["id"])
    with pytest.raises(HTTPException):
        ledger.reconcile(op["id"], evidence(op) | {field: value})
    assert ledger.recover("alice")[0]["status"] == "EXECUTING"
    with pytest.raises(HTTPException):
        prepare(ledger)


def test_timeout_does_not_reopen_and_reconstructed_reader_recovers(ledger):
    op = ledger.execute("alice", prepare(ledger)["id"])
    op = ledger.reconcile(op["id"], evidence(op, "timeout"))
    reopened = object.__new__(Ledger)
    reopened.path = ledger.path  # new reader, no seed/init and no in-memory operation state
    assert reopened.recover("alice") == [op]
    with pytest.raises(HTTPException):
        reopened.ack("bob", op["id"], op)
    reopened.ack("alice", op["id"], op)
    with reopened.transaction(write=False) as db:
        node = reopened.read(db, "nodes", "node")
        assert (node["state"], node["desired"]) == ("DRAINING", "CLOSED")


@pytest.fixture
def owner(c, monkeypatch):
    return make_owner.__wrapped__(c, monkeypatch)


def test_signed_identity_and_reload_http(owner, monkeypatch):
    store = Store()
    actor = json.dumps([owner["profile"].issuer, owner["tenant"], "oidc-alice"], separators=(",", ":"))
    store.seed("node", [actor])
    app = FastAPI()
    app.include_router(router)
    store.admitted = True  # standalone FastAPI fixture has no legacy Console database
    app.state.isolated_node_control = store
    headers = owner["headers"](realm_access={"roles": ["platform-admin"]})
    try:
        with TestClient(app) as client:
            body = dict(attempt=str(uuid4()), node="node", action="drain", expected=0)
            assert client.post("/isolated-node-control/prepare", headers=headers, json=body).status_code == 200
        with TestClient(app) as client:
            rows = client.get("/isolated-node-control/operations", headers=headers).json()["operations"]
            assert rows[0]["id"] == body["attempt"]
            for bad in [
                {},
                {"X-Console-Role": "PLATFORM_ADMIN"},
                owner["headers"](),
                owner["headers"](identity_type="service", realm_access={"roles": ["platform-admin"]}),
            ]:
                assert client.get("/isolated-node-control/operations", headers=bad).status_code in {400, 401, 403}
            monkeypatch.setenv("CONSOLE_ENV", "prod")
            assert client.get("/isolated-node-control/operations", headers=headers).status_code == 503
    finally:
        store.close()


def test_application_response_loss_reconciles_without_reexecution(ledger):
    from runtime.node.isolated_control import SyntheticNode

    node = SyntheticNode("node")
    try:
        op = ledger.execute("alice", prepare(ledger)["id"])
        with ledger.transaction(write=False) as db:
            state = ledger.read(db, "nodes", "node")
        node.apply(op, state)  # discard response, simulate a new client and control-plane reader
        assert not node.accepting
        reopened = object.__new__(Ledger)
        reopened.path = ledger.path
        terminal = reopened.reconcile(op["id"], node.read(op["id"]))
        assert terminal["status"] == "SUCCEEDED"
        assert node.applied_calls == 1
        reopened.ack("alice", op["id"], terminal)
        assert not node.accepting
        resume = reopened.execute("alice", prepare(reopened, "resume", 1)["id"])
        with reopened.transaction(write=False) as db:
            state = reopened.read(db, "nodes", "node")
        terminal = reopened.reconcile(resume["id"], node.apply(resume, state))
        assert node.accepting and terminal["status"] == "SUCCEEDED"
    finally:
        node.close()


def test_crash_before_application_stays_unresolved(ledger):
    from runtime.node.isolated_control import SyntheticNode

    node = SyntheticNode("node")
    try:
        op = ledger.execute("alice", prepare(ledger)["id"])
        assert node.read(op["id"]) is None
        assert ledger.recover("alice")[0]["status"] == "EXECUTING"
        with ledger.transaction(write=False) as db:
            state = ledger.read(db, "nodes", "node")
        with pytest.raises(ValueError):
            node.startup(state)
        assert not node.accepting and node.applied_calls == 0
    finally:
        node.close()


@pytest.mark.parametrize("mode", ["valid", "wrong_key", "wrong_node", "expired", "wrong_actor", "service_delegate"])
def test_signed_node_evidence(mode):
    import time

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    store = Store()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    store.seed("node", ["alice"])
    store.node_keys["node"] = ("https://synthetic-node.test", "fixture", key.public_key())
    try:
        op = store.execute("alice", prepare(store)["id"])
        stamp = int(time.time())
        claims = dict(
            iss="https://synthetic-node.test",
            sub="node",
            aud="f7-isolated-node-evidence",
            iat=stamp,
            nbf=stamp,
            exp=stamp + 60,
            identity_type="node",
            evidence=evidence(op),
        )
        if mode == "wrong_key":
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        if mode == "wrong_node":
            claims["sub"] = "another"
        if mode == "expired":
            claims.update(iat=stamp - 120, nbf=stamp - 120, exp=stamp - 60)
        if mode == "wrong_actor":
            claims["evidence"]["actor"] = "bob"
        if mode == "service_delegate":
            claims["act"] = {"sub": "node"}
        token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "fixture"})
        if mode == "valid":
            result = store.reconcile_signed(op["id"], token)
            assert result["status"] == "SUCCEEDED"
            assert store.reconcile_signed(op["id"], token) == result
        else:
            with pytest.raises(HTTPException):
                store.reconcile_signed(op["id"], token)
            assert store.recover("alice")[0]["status"] == "EXECUTING"
    finally:
        store.close()


def test_legacy_ingress_cannot_bypass_isolated_lock(c, owner):
    store = Store()
    node_id = "synthetic-" + uuid4().hex
    store.seed(node_id, ["alice"])
    c.app.state.isolated_node_control = store
    try:
        for kind in ["reload_config", "reload_skills", "switch_model", "graceful_restart"]:
            result = c.post(f"/nodes/{node_id}/commands", headers={"X-Console-Role": "PLATFORM_ADMIN"}, json={"type": kind, "args": {}})
            assert result.status_code == 409
    finally:
        del c.app.state.isolated_node_control
        store.close()


@pytest.mark.parametrize("lost", [False, True])
@pytest.mark.parametrize("action", ["drain", "configure_limit"])
def test_http_explicit_execute_and_signed_read_recovery(owner, lost, action):
    import time

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    from runtime.node.isolated_control import SyntheticNode

    store, node = Store(), SyntheticNode("node")
    actor = json.dumps([owner["profile"].issuer, owner["tenant"], "oidc-alice"], separators=(",", ":"))
    store.seed("node", [actor])
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    store.node_keys["node"] = ("https://node.test", "node-key", key.public_key())

    def signed(body):
        stamp = int(time.time())
        return jwt.encode(
            dict(
                iss="https://node.test",
                aud="f7-isolated-node-evidence",
                sub="node",
                iat=stamp,
                nbf=stamp,
                exp=stamp + 60,
                identity_type="node",
                evidence=body,
            ),
            key,
            algorithm="RS256",
            headers={"kid": "node-key"},
        )

    def apply(op, authority):
        result = node.apply(op, authority)
        if lost:
            raise OSError("synthetic response lost after application")
        return signed(result)

    store.apply_node = apply
    store.read_evidence = lambda node_id, attempt: signed(node.read(attempt)) if node.read(attempt) else None
    app = FastAPI()
    app.include_router(router)
    store.admitted = True  # standalone FastAPI fixture has no legacy Console database
    app.state.isolated_node_control = store
    headers = owner["headers"](realm_access={"roles": ["platform-admin"]})
    root = "/isolated-node-control"
    attempt = str(uuid4())
    try:
        with TestClient(app) as client:
            assert (
                client.post(
                    root + "/prepare",
                    headers=headers,
                    json=dict(attempt=attempt, node="node", action=action, expected=0)
                    | ({"limit": 2} if action == "configure_limit" else {}),
                ).status_code
                == 200
            )
            result = client.post(root + f"/operations/{attempt}/execute", headers=headers)
            assert result.status_code == (503 if lost else 200)
        with TestClient(app) as client:
            op = client.get(root + "/operations", headers=headers).json()["operations"][0]
            assert op["status"] == ("EXECUTING" if lost else "SUCCEEDED")
            # Even an explicit duplicate execute cannot apply again.
            assert client.post(root + f"/operations/{attempt}/execute", headers=headers).status_code == 200
            assert node.applied_calls == 1
            result = client.post(root + f"/operations/{attempt}/reconcile", headers=headers)
            assert result.status_code == 200
            terminal = result.json()
            assert terminal["status"] == "SUCCEEDED"
            assert client.post(root + f"/operations/{attempt}/ack", headers=headers, json={"receipt": terminal}).status_code == 200
            assert not node.accepting and node.applied_calls == 1
    finally:
        store.close()
        node.close()


def test_cross_process_claim_and_abrupt_exit(ledger):
    import subprocess
    import sys

    op = prepare(ledger)
    code = """
import os,sys
from pathlib import Path
from console_api.node_control_ledger import Ledger
from fastapi import HTTPException
store=object.__new__(Ledger)
store.path=Path(sys.argv[1])
try:
    row,claimed=store.claim('alice',sys.argv[2])
except HTTPException as exc:
    if exc.status_code != 409: raise
    os._exit(19)
os._exit(17 if claimed else 18)
"""
    args = [sys.executable, "-c", code, str(ledger.path), op["id"]]
    first = subprocess.Popen(args)  # noqa: S603 — fixed Python program, owned temporary DB and generated UUID only
    second = subprocess.Popen(args)  # noqa: S603 — fixed Python program, owned temporary DB and generated UUID only
    results = [first.wait(), second.wait()]
    # A competing database transaction may fail immediately; no timeout/retry is added.
    assert results.count(17) == 1
    assert all(value in {17, 18, 19} for value in results)
    current = ledger.recover("alice")[0]
    assert current["status"] == "EXECUTING" and not current["acknowledged"]
    with pytest.raises(HTTPException):
        ledger.withdraw("alice", op["id"])
    with pytest.raises(HTTPException):
        ledger.ack("alice", op["id"], current)


def test_natural_drain_preserves_failed_receipt_and_requires_ack(ledger):
    from runtime.node.isolated_control import SyntheticNode

    node = SyntheticNode("node")
    try:
        node.active = 1
        op = ledger.execute("alice", prepare(ledger)["id"])
        with ledger.transaction(write=False) as db:
            authority = ledger.read(db, "nodes", "node")
        terminal = ledger.reconcile(op["id"], node.apply(op, authority))
        assert terminal["status"] == "FAILED"
        with pytest.raises(ValueError):
            node.observe_drained(op["id"])
        node.active = 0
        observation = node.observe_drained(op["id"])
        assert ledger.observe_drained(op["id"], observation)["state"] == "DRAINED"
        assert ledger.recover("alice")[0] == terminal
        with pytest.raises(HTTPException):
            prepare(ledger, "resume", 1)
        ledger.ack("alice", op["id"], terminal)
        assert prepare(ledger, "resume", 1)["status"] == "PREPARED"
        assert node.read(op["id"])["outcome"] == "timeout" and not node.accepting
    finally:
        node.close()


def test_snapshot_hides_other_actors_and_unassigned_nodes(ledger):
    ledger.seed("foreign", ["foreign-user"])
    prepare(ledger)
    rows = ledger.snapshot("bob")
    assert len(rows) == 1 and rows[0]["id"] == "node" and rows[0]["control_busy"]
    assert "owner" not in rows[0] and "actors" not in rows[0]
    assert ledger.snapshot("nobody") == []


@pytest.mark.parametrize("mode", ["valid", "old_authority", "rollback", "unavailable", "busy", "replay"])
def test_boot_handoff_is_closed_until_matched_authority(ledger, mode):
    from runtime.node.isolated_control import SyntheticNode

    node = SyntheticNode("node")
    try:
        op = ledger.execute("alice", prepare(ledger)["id"])
        with ledger.transaction(write=False) as db:
            authority = ledger.read(db, "nodes", "node")
        receipt = ledger.reconcile(op["id"], node.apply(op, authority))
        if mode != "busy":
            ledger.ack("alice", op["id"], receipt)
        with ledger.transaction(write=False) as db:
            old = ledger.read(db, "nodes", "node")
        node.reboot()
        assert not node.accepting
        if mode == "busy":
            with pytest.raises(HTTPException):
                ledger.boot_challenge("node")
            return
        challenge = ledger.boot_challenge("node")
        proof = node.boot_proof(challenge)
        if mode == "rollback":
            with pytest.raises(ValueError):
                node.boot_proof(challenge | {"generation": 0})
        elif mode == "old_authority":
            with pytest.raises(ValueError):
                node.startup(old)
        elif mode == "unavailable":
            with pytest.raises(TypeError):
                node.startup(None)
        else:
            authority = ledger.accept_boot("node", proof)
            node.startup(authority)
            assert authority["boot_id"] == node.boot_id and not node.accepting
            if mode == "replay":
                with pytest.raises(HTTPException):
                    ledger.accept_boot("node", proof)
        assert not node.accepting
    finally:
        node.close()


def test_signed_boot_handoff(owner):
    import time

    import jwt

    from runtime.node.isolated_control import SyntheticNode

    store, node = Store(), SyntheticNode("node")
    store.seed("node", ["alice"])
    store.node_keys["node"] = ("https://node.test", "node-key", owner["private"].public_key())
    try:
        op = store.execute("alice", prepare(store)["id"])
        with store.transaction(write=False) as db:
            authority = store.read(db, "nodes", "node")
        receipt = store.reconcile(op["id"], node.apply(op, authority))
        store.ack("alice", op["id"], receipt)
        node.reboot()
        proof = node.boot_proof(store.boot_challenge("node"))
        stamp = int(time.time())
        claims = dict(
            iss="https://node.test",
            sub="node",
            aud="f7-isolated-node-evidence",
            identity_type="node",
            iat=stamp,
            nbf=stamp,
            exp=stamp + 60,
            evidence=proof,
        )
        token = jwt.encode(claims, owner["private"], algorithm="RS256", headers={"kid": "node-key"})
        authority = store.accept_boot_signed("node", token)
        node.startup(authority)
        assert authority["state"] == "DRAINED" and not node.accepting
    finally:
        node.close()
        store.close()


def test_admission_rejects_legacy_command_even_terminal(c):
    import asyncio

    from console_api.db import NodeCommand, SessionLocal

    store = Store()
    node_id = "f7-existing-" + uuid4().hex
    store.seed(node_id, ["alice"])

    async def check():
        async with SessionLocal() as session:
            session.add(
                NodeCommand(
                    command_id=str(uuid4()),
                    node_id=node_id,
                    tenant_id="synthetic",
                    type="reload_config",
                    args={},
                    status="ok",
                    created_by="synthetic",
                    authorization_role="PLATFORM_ADMIN",
                )
            )
            await session.commit()
            with pytest.raises(HTTPException):
                await store.admit_fresh_nodes(session)
            assert not store.admitted

    try:
        asyncio.run(check())
    finally:
        store.close()


def test_fresh_node_admission(c):
    import asyncio

    from console_api.db import SessionLocal

    store = Store()
    store.seed("f7-new-" + uuid4().hex, ["alice"])

    async def check():
        async with SessionLocal() as session:
            await store.admit_fresh_nodes(session)

    try:
        asyncio.run(check())
        assert store.admitted
    finally:
        store.close()


def test_failed_drain_remains_recoverable_after_ack(ledger):
    op = ledger.execute("alice", prepare(ledger)["id"])
    terminal = ledger.reconcile(op["id"], evidence(op, "timeout"))
    ledger.ack("alice", op["id"], terminal)
    assert ledger.recover("alice") == []
    assert ledger.snapshot("alice")[0]["recovery_attempt"] == op["id"]
    assert ledger.snapshot("bob")[0]["recovery_attempt"] is None


def test_lifecycle_drain_preserves_stream_and_worker_then_resumes(ledger):
    import asyncio
    from types import SimpleNamespace

    from runtime.node.isolated_control import LifecycleNode
    from runtime.node.lifecycle import AdmissionMiddleware, NodeLifecycle

    async def scenario():
        worker = SimpleNamespace(paused=False, idle=asyncio.Event())
        lifecycle = NodeLifecycle(SimpleNamespace(worker=worker), None, durable=False)
        node = LifecycleNode("node", lifecycle)
        started, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_close = lifecycle.close_admission

        def close():
            original_close()
            closed.set()

        lifecycle.close_admission = close
        app = SimpleNamespace(state=SimpleNamespace(node_lifecycle=lifecycle))

        async def handler(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            if scope["path"] == "/events":
                started.set()
                await send({"type": "http.response.body", "body": b"event: persisted", "more_body": True})
                await release.wait()
            await send({"type": "http.response.body", "body": b"done", "more_body": False})

        middleware = AdmissionMiddleware(handler)

        async def request(path):
            messages = []

            async def send(message):
                messages.append(message)

            async def receive():
                return {"type": "http.request", "body": b""}

            await middleware({"type": "http", "path": path, "method": "GET", "app": app}, receive, send)
            return messages

        stream = draining = None
        try:
            lifecycle.resume()  # Synthetic already-admitted baseline, no real server or worker.
            stream = asyncio.create_task(request("/events"))
            await started.wait()
            op = ledger.execute("alice", prepare(ledger)["id"])
            with ledger.transaction(write=False) as db:
                authority = ledger.read(db, "nodes", "node")
            draining = asyncio.create_task(node.apply_lifecycle(op, authority, 30))
            await closed.wait()
            assert lifecycle.active_requests == 1 and worker.paused and not stream.done()
            assert (await request("/new"))[0]["status"] == 503
            assert (await request("/healthz"))[0]["status"] == 200
            release.set()
            messages = await stream
            assert messages[-1]["body"] == b"done" and not draining.done()
            worker.idle.set()
            result = await draining
            assert result["outcome"] == "drained" and not lifecycle.accepting
            terminal = ledger.reconcile(op["id"], result)
            ledger.ack("alice", op["id"], terminal)
            assert not lifecycle.accepting and worker.paused
            resume = ledger.execute("alice", prepare(ledger, "resume", 1)["id"])
            with ledger.transaction(write=False) as db:
                authority = ledger.read(db, "nodes", "node")
            await node.apply_lifecycle(resume, authority, 30)
            assert lifecycle.accepting and not worker.paused
        finally:
            release.set()
            worker.idle.set()
            for task in (stream, draining):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*[t for t in (stream, draining) if t is not None], return_exceptions=True)
            node.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_lifecycle_timeout_and_cancellation_never_resume(ledger, cancel):
    import asyncio
    from types import SimpleNamespace

    from runtime.node.isolated_control import LifecycleNode
    from runtime.node.lifecycle import NodeLifecycle

    async def scenario():
        worker = SimpleNamespace(paused=False, idle=asyncio.Event())
        lifecycle = NodeLifecycle(SimpleNamespace(worker=worker), None, durable=False)
        node = LifecycleNode("node", lifecycle)

        async def deadline():
            if cancel:
                raise asyncio.CancelledError()
            raise TimeoutError("injected deadline event")

        lifecycle.wait_idle = deadline
        try:
            op = ledger.execute("alice", prepare(ledger)["id"])
            with ledger.transaction(write=False) as db:
                authority = ledger.read(db, "nodes", "node")
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await node.apply_lifecycle(op, authority, 30)
                assert not lifecycle.accepting and worker.paused and node.read(op["id"]) is None
                assert ledger.recover("alice")[0]["status"] == "EXECUTING"
                return
            result = await node.apply_lifecycle(op, authority, 30)
            assert result["outcome"] == "timeout" and not lifecycle.accepting and worker.paused
            with pytest.raises(ValueError):
                node.observe_drained(op["id"])
            worker.idle.set()
            assert node.observe_drained(op["id"])["outcome"] == "observed_drained"
        finally:
            node.close()

    asyncio.run(scenario())


def test_resume_journal_failure_closes_and_has_no_terminal_evidence(ledger):
    import sqlite3

    from runtime.node.isolated_control import SyntheticNode

    node = SyntheticNode("node")
    try:
        op = ledger.execute("alice", prepare(ledger)["id"])
        with ledger.transaction(write=False) as db:
            authority = ledger.read(db, "nodes", "node")
        receipt = ledger.reconcile(op["id"], node.apply(op, authority))
        ledger.ack("alice", op["id"], receipt)
        resume = ledger.execute("alice", prepare(ledger, "resume", 1)["id"])
        with ledger.transaction(write=False) as db:
            authority = ledger.read(db, "nodes", "node")
        with node.connection() as db:
            db.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON effects BEGIN SELECT RAISE(ABORT,'synthetic disk failure'); END")
        with pytest.raises(sqlite3.IntegrityError):
            node.apply(resume, authority)
        assert not node.accepting and node.read(resume["id"]) is None
        with node.connection() as db:
            assert db.execute("SELECT generation,desired FROM state WHERE id=1").fetchone() == (1, "CLOSED")
        assert ledger.recover("alice")[0]["status"] == "EXECUTING"
    finally:
        node.close()


def test_serving_boot_restores_only_after_fresh_handoff(ledger, monkeypatch):
    from console_api import node_control_ledger
    from runtime.node.isolated_control import SyntheticNode

    node = SyntheticNode("node")
    try:
        for action, generation, outcome in [("drain", 0, "drained"), ("resume", 1, "serving")]:
            op = ledger.execute("alice", prepare(ledger, action, generation)["id"])
            with ledger.transaction(write=False) as db:
                authority = ledger.read(db, "nodes", "node")
            receipt = ledger.reconcile(op["id"], node.apply(op, authority))
            ledger.ack("alice", op["id"], receipt)
            assert receipt["evidence"]["outcome"] == outcome
        node.reboot()
        proof = node.boot_proof(ledger.boot_challenge("node"))
        now = node_control_ledger.time.time
        monkeypatch.setattr(node_control_ledger.time, "time", lambda: proof["expires_at"])
        with pytest.raises(HTTPException):
            ledger.accept_boot("node", proof)
        assert not node.accepting
        monkeypatch.setattr(node_control_ledger.time, "time", now)
        proof = node.boot_proof(ledger.boot_challenge("node"))
        authority = ledger.accept_boot("node", proof)
        assert not node.accepting
        node.startup(authority)
        assert node.accepting
    finally:
        node.close()


def test_limit_configuration_bound_receipt_and_shared_node_lock(ledger):
    from runtime.node.isolated_control import SyntheticNode

    node = SyntheticNode("node")
    try:
        attempt = str(uuid4())
        op = ledger.prepare("alice", "node", "configure_limit", 0, attempt, 2)
        with pytest.raises(HTTPException):
            ledger.prepare("alice", "node", "configure_limit", 0, attempt, 3)
        with pytest.raises(HTTPException):
            prepare(ledger)
        op, claimed = ledger.claim("alice", attempt)
        assert claimed and ledger.claim("alice", attempt) == (op, False)
        with ledger.transaction(write=False) as db:
            state = ledger.read(db, "nodes", "node")
        proof = node.apply(op, state)
        assert not node.accepting  # configuring never opens a closed node
        for changed in [3, True, None]:
            with pytest.raises(HTTPException):
                ledger.reconcile(attempt, proof | {"limit": changed})
        reopened = object.__new__(Ledger)
        reopened.path = ledger.path
        result = reopened.reconcile(attempt, node.read(attempt))
        assert result["status"] == "SUCCEEDED" and node.applied_calls == 1
        assert reopened.snapshot("alice")[0]["http_concurrency_limit"] == 2
        with pytest.raises(HTTPException):
            reopened.ack("bob", attempt, result)
        reopened.ack("alice", attempt, result)
        assert reopened.snapshot("alice")[0]["state"] == "SERVING"
        assert prepare(ledger, generation=1)["status"] == "PREPARED"
    finally:
        node.close()


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, None])
def test_limit_invalid_keeps_ledger_empty(ledger, limit):
    with pytest.raises(HTTPException):
        ledger.prepare("alice", "node", "configure_limit", 0, str(uuid4()), limit)
    assert ledger.recover("alice") == []


@pytest.mark.asyncio
async def test_lifecycle_limit_does_not_drain_active_work(ledger):
    import asyncio
    from types import SimpleNamespace

    from runtime.node.isolated_control import LifecycleNode
    from runtime.node.lifecycle import NodeLifecycle

    worker = SimpleNamespace(paused=False, idle=asyncio.Event())
    life = NodeLifecycle(SimpleNamespace(worker=worker), None, durable=False)
    node = LifecycleNode("node", life)
    try:
        life.resume()
        life.active_requests = 3
        life.http_idle.clear()
        op = ledger.prepare("alice", "node", "configure_limit", 0, str(uuid4()), 1)
        op = ledger.execute("alice", op["id"])
        with ledger.transaction(write=False) as db:
            state = ledger.read(db, "nodes", "node")
        proof = await node.apply_lifecycle(op, state, 1)
        assert life.accepting and not worker.paused and life.active_requests == 3
        assert life.http_concurrency_limit == 1
        assert ledger.reconcile(op["id"], proof)["status"] == "SUCCEEDED"
    finally:
        node.close()


def test_limit_startup_requires_durable_value_and_keeps_drain_owner(ledger):
    from runtime.node.isolated_control import SyntheticNode

    node = SyntheticNode("node")
    try:
        drain = ledger.execute("alice", prepare(ledger)["id"])
        with ledger.transaction(write=False) as db:
            state = ledger.read(db, "nodes", "node")
        done = ledger.reconcile(drain["id"], node.apply(drain, state))
        ledger.ack("alice", drain["id"], done)
        op = ledger.prepare("bob", "node", "configure_limit", 1, str(uuid4()), 3)
        op = ledger.execute("bob", op["id"])
        with ledger.transaction(write=False) as db:
            state = ledger.read(db, "nodes", "node")
        done = ledger.reconcile(op["id"], node.apply(op, state))
        ledger.ack("bob", op["id"], done)
        with ledger.transaction(write=False) as db:
            state = ledger.read(db, "nodes", "node")
        node.startup(state)
        assert not node.accepting and state["owner"] == "alice"
        with pytest.raises(ValueError):
            node.startup(state | {"http_concurrency_limit": 2})
        with pytest.raises(HTTPException):
            prepare(ledger, "resume", 2, "bob")
        assert prepare(ledger, "resume", 2)["status"] == "PREPARED"
    finally:
        node.close()


@pytest.mark.asyncio
async def test_limit_journal_failure_retains_unknown_and_closes_admission(ledger):
    import sqlite3
    from types import SimpleNamespace

    from runtime.node.isolated_control import LifecycleNode
    from runtime.node.lifecycle import NodeLifecycle

    life = NodeLifecycle(SimpleNamespace(worker=None), None, durable=False)
    node = LifecycleNode("node", life)
    try:
        life.resume()
        life.set_http_concurrency_limit(4)
        with node.connection() as db:
            db.execute("CREATE TRIGGER fail_effect BEFORE INSERT ON effects BEGIN SELECT RAISE(ABORT, 'synthetic disk failure'); END")
        op = ledger.prepare("alice", "node", "configure_limit", 0, str(uuid4()), 2)
        op = ledger.execute("alice", op["id"])
        with ledger.transaction(write=False) as db:
            authority = ledger.read(db, "nodes", "node")
        with pytest.raises(sqlite3.IntegrityError):
            await node.apply_lifecycle(op, authority, 1)
        assert not life.accepting and life.http_concurrency_limit == 4
        assert node.read(op["id"]) is None and node.applied_calls == 0
        with node.connection() as db:
            assert db.execute("SELECT generation FROM state").fetchone()[0] == 0
            assert db.execute("SELECT http_limit FROM configuration").fetchone()[0] is None
        assert ledger.recover("alice")[0]["status"] == "EXECUTING"
        assert ledger.claim("alice", op["id"])[1] is False
        with pytest.raises(HTTPException):
            ledger.ack("alice", op["id"], op)
    finally:
        node.close()

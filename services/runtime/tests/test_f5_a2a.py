"""Isolated E2: real transactions and signed test users; no real model/network."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
import test_f5_discovery as discovery_tests
from sqlalchemy import select
from test_f5_discovery import rule, seed, who

from runtime.api import isolated_a2a as a2a
from runtime.api import messages, workbench_create
from runtime.state import db

ROOT = a2a.PREFIX


@pytest.fixture(name="idp")
def signed_provider(monkeypatch):
    yield from discovery_tests.signed_provider.__wrapped__(monkeypatch)


@pytest.fixture(name="lab")
def discovery_lab(client, monkeypatch):
    return discovery_tests.lab.__wrapped__(client, monkeypatch)


@pytest.fixture
def fixture(lab, monkeypatch):
    from runtime.main import app

    client, snapshot = lab
    if not any(getattr(route, "path", "") == ROOT + "/prepare" for route in app.routes):
        app.include_router(a2a.router)
    monkeypatch.setattr(app.state, "isolated_a2a", a2a.ENABLED, raising=False)

    async def init():
        async with db.engine.begin() as connection:
            await connection.run_sync(a2a.Metadata.metadata.create_all)
            await connection.run_sync(lambda c: db.ContinuationInput.__table__.create(c, checkfirst=True))

    client.portal.call(init)
    agent = seed(client)
    snapshot["rules"] = [rule(agent)]
    queued = []

    async def enqueue(run, reason):
        queued.append((run, reason))

    monkeypatch.setattr(messages, "_requeue", enqueue)
    yield client, snapshot, agent, queued
    from test_s12_create_ledger import retire_create_runs

    async def created():
        async with db.SessionLocal() as session:
            return list((await session.scalars(select(db.Run.run_id).where(db.Run.agent_id == agent))).all())

    client.portal.call(retire_create_runs, client.portal.call(created), client.runtime_worker)


def headers(idp, **claims):
    return who(idp, **claims) | {"A2A-Version": "1.0", "A2A-Extensions": a2a.EXTENSION}


def payload(agent):
    return {
        "attempt_id": str(uuid4()),
        "agent_id": agent,
        "message": {"messageId": str(uuid4()), "role": "ROLE_USER", "parts": [{"text": "synthetic read only"}]},
        "configuration": {"returnImmediately": True},
    }


def evidence(receipt):
    return {key: receipt["receipt"][key] for key in ("attempt_id", "revision", "fingerprint")}


def send_body(body, prepared):
    return {"message": body["message"], "configuration": body["configuration"], "metadata": {a2a.EXTENSION: evidence(prepared)}}


def cleanup(client, body, h):
    r = client.post(ROOT + "/message:send", headers=h, json=body)
    assert r.status_code in (200, 409), r.text
    recovered = client.get(ROOT + "/attempts/" + body["metadata"][a2a.EXTENSION]["attempt_id"], headers=h)
    assert recovered.status_code == 200
    result = recovered.json()
    assert result["receipt"]["status"] == ("COMMITTED" if r.status_code == 200 else "REJECTED_NO_EFFECT")
    ack = client.post(ROOT + "/acknowledge", headers=h, json=evidence(result))
    assert ack.status_code == 200, ack.text
    return result


def test_prepare_execute_recover_ack_and_no_legacy_bypass(fixture, idp):
    client, _, agent, queued = fixture
    h, body = headers(idp), payload(agent)
    response = client.post(ROOT + "/prepare", headers=h, json=body)
    assert response.status_code == 200, response.text
    p = response.json()
    assert p["taskId"] is None and p["receipt"]["status"] == "PREPARED"
    assert client.get(ROOT + "/tasks/" + p["receipt"]["run_id"], headers=h).status_code == 404
    assert client.post(ROOT + "/acknowledge", headers=h, json=evidence(p)).status_code == 409
    assert (
        client.post(
            "/v1/workbench/create-attempts", headers=h, json={"attempt_id": str(uuid4()), "agent_id": agent, "text": "blocked"}
        ).status_code
        == 409
    )
    legacy = {"fingerprint": p["receipt"]["fingerprint"], "revision": 1}
    assert client.post("/v1/workbench/attempts/" + body["attempt_id"] + "/execute", headers=h, json=legacy).status_code == 409
    s = send_body(body, p)
    altered = {**s, "message": {**s["message"], "messageId": "different"}}
    assert client.post(ROOT + "/message:send", headers=h, json=altered).status_code == 409
    executed = client.post(ROOT + "/message:send", headers=h, json=s)
    assert executed.status_code == 200, executed.text
    task = executed.json()["task"]
    e = task["metadata"][a2a.EXTENSION]
    assert task["id"] == e["taskId"]
    assert task["status"]["state"] == "TASK_STATE_SUBMITTED"
    assert not e["receipt"]["acknowledged"]
    # Lost response/reload: GET is authoritative and does not queue again.
    assert client.get(ROOT + "/attempts/" + body["attempt_id"], headers=h).json()["receipt"] == e["receipt"]
    assert client.post(ROOT + "/message:send", headers=h, json=s).status_code == 409
    assert len(queued) == 1
    for claims in ({"sub": "bob"}, {"tenant": "foreign"}, {"sub": "bob", "realm_access": {"roles": ["platform-admin"]}}):
        foreign = headers(idp, **claims)
        assert client.get(ROOT + "/tasks/" + e["taskId"], headers=foreign).status_code == 404
        assert client.get(ROOT + "/attempts/" + body["attempt_id"], headers=foreign).status_code == 404
        assert client.post(ROOT + "/acknowledge", headers=foreign, json=evidence(e)).status_code == 404
    assert client.post(ROOT + "/acknowledge", headers=h, json=evidence(p)).status_code == 409
    ack = client.post(ROOT + "/acknowledge", headers=h, json=evidence(e))
    assert ack.status_code == 200 and ack.json()["receipt"]["acknowledged"]
    assert client.get(ROOT + "/attempts/" + body["attempt_id"], headers=headers(idp, jti="refreshed")).json()["receipt"]["acknowledged"]
    assert client.post(ROOT + "/acknowledge", headers=h, json=evidence(e)).status_code == 409
    assert client.post(ROOT + "/message:send", headers=h, json=s).status_code == 409
    assert client.post(ROOT + "/prepare", headers=h, json={**body, "attempt_id": str(uuid4())}).status_code == 409


def test_revocation_and_history(fixture, idp):
    client, rules, agent, queued = fixture
    h, body = headers(idp), payload(agent)
    p = client.post(ROOT + "/prepare", headers=h, json=body).json()
    rules.update(revision=2, rules=[])
    rejected = cleanup(client, send_body(body, p), h)
    assert rejected["receipt"]["status"] == "REJECTED_NO_EFFECT"
    assert rejected["taskId"] is None and not queued
    rules.update(revision=3, rules=[rule(agent)])
    body = payload(agent)
    p = client.post(ROOT + "/prepare", headers=h, json=body).json()
    e = cleanup(client, send_body(body, p), h)
    rules.update(revision=4, rules=[])
    assert client.get(ROOT + "/tasks/" + e["taskId"], headers=h).status_code == 200
    assert client.post(ROOT + "/prepare", headers=h, json=payload(agent)).status_code == 403


def test_negotiation_identity_and_unsupported_fields(fixture, idp):
    client, _, agent, _ = fixture
    body, h = payload(agent), headers(idp)
    assert client.post(ROOT + "/prepare", json=body).status_code == 401
    for key in ("A2A-Version", "A2A-Extensions"):
        assert client.post(ROOT + "/prepare", headers={k: v for k, v in h.items() if k != key}, json=body).status_code == 400
    for claims in ({"act": {}}, {"act": {"sub": "app:host"}, "azp": "host"}, {"client_id": "service"}):
        assert client.post(ROOT + "/prepare", headers=headers(idp, **claims), json=body).status_code == 403
    for message in ({**body["message"], "taskId": "other"}, {**body["message"], "parts": [{"data": {}}]}):
        assert client.post(ROOT + "/prepare", headers=h, json={**body, "message": message}).status_code == 422
    assert client.post(ROOT + "/prepare", headers=h, json={**body, "configuration": {"returnImmediately": False}}).status_code == 422


def test_concurrent_registration_and_execution(fixture, idp):
    client, _, agent, queued = fixture
    h, body = headers(idp), payload(agent)
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda _: client.post(ROOT + "/prepare", headers=h, json=body), range(2)))
    assert [r.status_code for r in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    s = send_body(body, responses[0].json())
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda _: client.post(ROOT + "/message:send", headers=h, json=s), range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409]
    e = next(r.json()["task"]["metadata"][a2a.EXTENSION] for r in responses if r.status_code == 200)
    assert len(queued) == 1
    assert client.post(ROOT + "/acknowledge", headers=h, json=evidence(e)).status_code == 200


def test_atomic_failure_preserves_prepared_and_no_run(fixture, idp, monkeypatch):
    client, _, agent, queued = fixture
    h, body = headers(idp), payload(agent)
    p = client.post(ROOT + "/prepare", headers=h, json=body).json()
    original = workbench_create.apply

    async def fail_after_insert(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("injected rollback")

    with monkeypatch.context() as patch:
        patch.setattr(workbench_create, "apply", fail_after_insert)
        with pytest.raises(RuntimeError, match="injected rollback"):
            client.post(ROOT + "/message:send", headers=h, json=send_body(body, p))
    assert client.get(ROOT + "/attempts/" + body["attempt_id"], headers=h).json()["receipt"]["status"] == "PREPARED"

    async def absent():
        async with db.SessionLocal() as session:
            assert await session.get(db.Run, p["receipt"]["run_id"]) is None
            assert await session.scalar(select(db.RunEventRow).where(db.RunEventRow.run_id == p["receipt"]["run_id"])) is None

    client.portal.call(absent)
    assert not queued
    # No automatic retry: operator chooses a later explicit execution using proven prepared evidence.
    cleanup(client, send_body(body, p), h)


def test_commit_response_failure_is_readonly_recoverable(fixture, idp, monkeypatch):
    client, _, agent, _ = fixture
    h, body = headers(idp), payload(agent)
    p = client.post(ROOT + "/prepare", headers=h, json=body).json()

    async def lost(*args):
        raise RuntimeError("injected after commit")

    with monkeypatch.context() as patch:
        patch.setattr(messages, "_requeue", lost)
        with pytest.raises(RuntimeError, match="injected after commit"):
            client.post(ROOT + "/message:send", headers=h, json=send_body(body, p))
    recovered = client.get(ROOT + "/attempts/" + body["attempt_id"], headers=h).json()
    assert recovered["receipt"]["status"] == "COMMITTED"
    assert not recovered["receipt"]["acknowledged"]
    assert client.post(ROOT + "/message:send", headers=h, json=send_body(body, p)).status_code == 409
    assert client.post(ROOT + "/acknowledge", headers=h, json=evidence(recovered)).status_code == 200


def test_unavailable_authority_does_not_invent_rejection(fixture, idp, monkeypatch):
    from runtime.main import app

    client, _, agent, _ = fixture
    h, body = headers(idp), payload(agent)
    p = client.post(ROOT + "/prepare", headers=h, json=body).json()

    async def unavailable(tenant):
        raise RuntimeError("synthetic outage")

    with monkeypatch.context() as patch:
        patch.setattr(app.state, "isolated_visibility_reader", unavailable)
        assert client.post(ROOT + "/message:send", headers=h, json=send_body(body, p)).status_code == 503
    assert client.get(ROOT + "/attempts/" + body["attempt_id"], headers=h).json()["receipt"]["status"] == "PREPARED"
    cleanup(client, send_body(body, p), h)


def test_terminal_states_do_not_unlock_or_reveal_events(fixture, idp):
    client, _, agent, _ = fixture
    h, body = headers(idp), payload(agent)
    p = client.post(ROOT + "/prepare", headers=h, json=body).json()
    response = client.post(ROOT + "/message:send", headers=h, json=send_body(body, p))
    e = response.json()["task"]["metadata"][a2a.EXTENSION]

    async def state(value):
        async with db.SessionLocal() as session:
            run = await session.get(db.Run, e["taskId"])
            run.state = value
            await session.commit()

    for value in ("working", "auth-required", "input-required", "completed", "failed", "canceled"):
        client.portal.call(state, value)
        response = client.get(ROOT + "/tasks/" + e["taskId"], headers=h)
        assert response.json() == {
            "id": e["taskId"],
            "contextId": e["contextId"],
            "status": {"state": "TASK_STATE_" + value.upper().replace("-", "_")},
        }
        assert not client.get(ROOT + "/attempts/" + body["attempt_id"], headers=h).json()["receipt"]["acknowledged"]
    assert client.post(ROOT + "/acknowledge", headers=h, json=evidence(e)).status_code == 200


def test_card_and_profile_disabled(fixture, idp, monkeypatch):
    from dataclasses import replace

    from runtime.api import isolated_discovery
    from runtime.main import app

    client, _, agent, _ = fixture
    h = headers(idp)
    card = client.get(ROOT + "/.well-known/agent-card.json", headers=h)
    assert card.status_code == 200
    assert card.json()["capabilities"]["extensions"][0]["required"] is True
    assert card.json()["capabilities"]["streaming"] is False
    with monkeypatch.context() as patch:
        patch.setattr(isolated_discovery, "settings", replace(isolated_discovery.settings, runtime_env="prod"))
        assert client.post(ROOT + "/prepare", headers=h, json=payload(agent)).status_code == 503
    monkeypatch.setattr(app.state, "isolated_a2a", None)
    assert client.post(ROOT + "/prepare", headers=h, json=payload(agent)).status_code == 503


def test_prepare_mapping_and_lock_are_one_transaction(fixture, idp, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    client, _, agent, _ = fixture
    h, body = headers(idp), payload(agent)
    original = AsyncSession.commit

    async def fail(session):
        if any(isinstance(row, a2a.Mapping) for row in session.new):
            await session.flush()
            raise RuntimeError("injected preparation failure")
        await original(session)

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", fail)
        with pytest.raises(RuntimeError, match="injected preparation failure"):
            client.post(ROOT + "/prepare", headers=h, json=body)

    async def absent():
        async with db.SessionLocal() as session:
            assert await session.get(a2a.Mapping, body["attempt_id"]) is None
            assert await session.get(db.WorkbenchAttempt, body["attempt_id"]) is None

    client.portal.call(absent)
    assert client.get("/v1/workbench/attempts", headers=h).json()["active"] is None


def test_corrupt_terminal_evidence_cannot_ack(fixture, idp):
    client, _, agent, _ = fixture
    h, body = headers(idp), payload(agent)
    p = client.post(ROOT + "/prepare", headers=h, json=body).json()
    result = client.post(ROOT + "/message:send", headers=h, json=send_body(body, p)).json()
    e = result["task"]["metadata"][a2a.EXTENSION]

    async def corrupt(restore=None):
        async with db.SessionLocal() as session:
            event = await session.get(db.RunEventRow, (e["taskId"], 1))
            original = dict(event.payload)
            event.payload = restore if restore is not None else {**original, "workbench_value_hmac": "wrong"}
            await session.commit()
            return original

    original = client.portal.call(corrupt)
    assert client.post(ROOT + "/acknowledge", headers=h, json=evidence(e)).status_code == 409
    assert client.get(ROOT + "/attempts/" + body["attempt_id"], headers=h).status_code == 409
    assert client.get(ROOT + "/tasks/" + e["taskId"], headers=h).status_code == 409
    client.portal.call(corrupt, original)
    assert client.post(ROOT + "/acknowledge", headers=h, json=evidence(e)).status_code == 200


@pytest.mark.parametrize("variant", ["valid", "missing-link", "withheld", "wrong-type", "empty", "oversized", "failed", "bad-terminal"])
def test_final_artifact_is_explicit_safe_projection(fixture, idp, variant):
    from ai_contract import RunState
    from runtime.state.eventlog import EventLog

    client, _, agent, _ = fixture
    h, body = headers(idp), payload(agent)
    p = client.post(ROOT + "/prepare", headers=h, json=body).json()
    result = client.post(ROOT + "/message:send", headers=h, json=send_body(body, p)).json()
    e = result["task"]["metadata"][a2a.EXTENSION]

    async def finish():
        async with db.SessionLocal() as session:
            log = EventLog(session)
            await log.transition(e["taskId"], RunState.WORKING, "synthetic")
            await log.append(e["taskId"], a2a.RunEventType.MODEL_RESPONSE, {"text": "OLD-DO-NOT-FALLBACK"})
            content = {"text": "<script>synthetic</script>", "thinking": "PRIVATE-THINKING", "tool": "PRIVATE-TOOL"}
            if variant == "withheld":
                content["withheld_text_hmac"] = "synthetic-hmac"
            if variant == "empty":
                content["text"] = ""
            if variant == "oversized":
                content["text"] = "x" * 65537
            kind = a2a.RunEventType.TOOL_RESULT if variant == "wrong-type" else a2a.RunEventType.MODEL_RESPONSE
            response = await log.append(e["taskId"], kind, content)
            done = {"stop_reason": "end_turn"}
            if variant != "missing-link":
                done["final_response_seq"] = response.seq
            await log.append(e["taskId"], a2a.RunEventType.RUN_DONE, done)
            await log.transition(e["taskId"], RunState.FAILED if variant == "failed" else RunState.COMPLETED, "synthetic")
            if variant == "bad-terminal":
                await log.append(e["taskId"], a2a.RunEventType.RUN_ERROR, {"message": "PRIVATE-ERROR"})
            await session.commit()
            return response.seq

    seq = client.portal.call(finish)
    response = client.get(ROOT + "/tasks/" + e["taskId"], headers=h)
    assert response.status_code == 200
    assert "PRIVATE" not in response.text and "OLD-DO-NOT-FALLBACK" not in response.text
    if variant == "valid":
        artifact = response.json()["artifacts"][0]
        assert artifact["artifactId"] == f"{e['taskId']}:final:{seq}"
        assert artifact["parts"] == [{"text": "<script>synthetic</script>"}]
        assert client.get(ROOT + "/tasks/" + e["taskId"], headers=h).json() == response.json()
    else:
        assert "artifacts" not in response.json()
    assert not client.get(ROOT + "/attempts/" + body["attempt_id"], headers=h).json()["receipt"]["acknowledged"]
    assert client.post(ROOT + "/acknowledge", headers=h, json=evidence(e)).status_code == 200


def withdraw_call(client, h, prepared):
    return client.post(ROOT + "/attempts/" + prepared["receipt"]["attempt_id"] + "/withdraw", headers=h, json=evidence(prepared))


def test_owner_withdrawal_revocation_independent_ack(fixture, idp, monkeypatch):
    from runtime.main import app

    client, rules, agent, queued = fixture
    h, body = headers(idp), payload(agent)
    prepared = client.post(ROOT + "/prepare", headers=h, json=body).json()
    for claims in ({"sub": "bob"}, {"tenant": "foreign"}, {"sub": "bob", "realm_access": {"roles": ["platform-admin"]}}):
        assert withdraw_call(client, headers(idp, **claims), prepared).status_code == 404
    for claims in ({"act": {}}, {"client_id": "service"}):
        assert withdraw_call(client, headers(idp, **claims), prepared).status_code == 403

    async def unavailable(tenant):
        raise RuntimeError("synthetic directory unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(app.state, "isolated_visibility_reader", unavailable)
        response = withdraw_call(client, h, prepared)
        assert response.status_code == 200, response.text
        terminal = response.json()
        assert terminal["receipt"]["rejection"] == "OWNER_WITHDRAWN"
        assert terminal["receipt"]["status"] == "REJECTED_NO_EFFECT" and terminal["taskId"] is None
        assert not terminal["receipt"]["acknowledged"]
        assert client.get("/v1/workbench/attempts", headers=h).json()["active"]["attempt_id"] == body["attempt_id"]
        assert withdraw_call(client, h, prepared).status_code == 409
        assert client.post(ROOT + "/message:send", headers=h, json=send_body(body, prepared)).status_code == 409
        assert client.post(ROOT + "/acknowledge", headers=h, json=evidence(prepared)).status_code == 409
        assert client.post(ROOT + "/acknowledge", headers=h, json=evidence(terminal)).status_code == 200
        recovered = client.get(ROOT + "/attempts/" + body["attempt_id"], headers=h).json()
        assert recovered["receipt"]["acknowledged"]
    assert not queued
    assert client.post(ROOT + "/message:send", headers=h, json=send_body(body, prepared)).status_code == 409
    assert client.post(ROOT + "/prepare", headers=h, json={**body, "attempt_id": str(uuid4())}).status_code == 409
    new_body = payload(agent)
    new_prepared = client.post(ROOT + "/prepare", headers=h, json=new_body).json()
    cleanup(client, send_body(new_body, new_prepared), h)


@pytest.mark.parametrize("first,second", [("send", "withdraw"), ("withdraw", "send"), ("withdraw", "withdraw")])
def test_withdraw_and_send_share_transaction_lock(fixture, idp, monkeypatch, first, second):
    import asyncio
    import itertools
    import threading

    from runtime.api import workbench

    client, _, agent, queued = fixture
    h, body = headers(idp), payload(agent)
    prepared = client.post(ROOT + "/prepare", headers=h, json=body).json()
    acquired, contender, release = threading.Event(), threading.Event(), threading.Event()
    original, counter = workbench.lock_scope, itertools.count()

    async def barrier(session, scope):
        index = next(counter)
        if index == 1:
            contender.set()
        locked = await original(session, scope)
        if index == 0:
            acquired.set()
            assert await asyncio.to_thread(release.wait, 5), "test release missing"
        return locked

    def operation(name):
        if name == "send":
            return client.post(ROOT + "/message:send", headers=h, json=send_body(body, prepared))
        return withdraw_call(client, h, prepared)

    with monkeypatch.context() as patch:
        patch.setattr(workbench, "lock_scope", barrier)
        with ThreadPoolExecutor(2) as pool:
            a = pool.submit(operation, first)
            try:
                assert acquired.wait(5)
                b = pool.submit(operation, second)
                assert contender.wait(5)
            finally:
                release.set()
            assert a.result().status_code == 200
            assert b.result().status_code == 409
    terminal = client.get(ROOT + "/attempts/" + body["attempt_id"], headers=h).json()
    assert terminal["receipt"]["status"] == ("COMMITTED" if first == "send" else "REJECTED_NO_EFFECT")
    assert len(queued) == (1 if first == "send" else 0)

    async def effects():
        async with db.SessionLocal() as session:
            records = list(
                (await session.scalars(select(db.RunEventRow).where(db.RunEventRow.run_id == prepared["receipt"]["run_id"]))).all()
            )
            assert sum(row.type == "run.created" for row in records) == (1 if first == "send" else 0)
            assert (await session.get(a2a.Withdrawal, body["attempt_id"]) is not None) == (first == "withdraw")

    client.portal.call(effects)
    assert client.post(ROOT + "/acknowledge", headers=h, json=evidence(terminal)).status_code == 200


def test_withdrawal_commit_failure_rolls_back_evidence_and_status(fixture, idp, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    client, _, agent, _ = fixture
    h, body = headers(idp), payload(agent)
    prepared = client.post(ROOT + "/prepare", headers=h, json=body).json()
    original = AsyncSession.commit

    async def fail(session):
        if any(isinstance(row, a2a.Withdrawal) for row in session.new):
            await session.flush()
            raise RuntimeError("withdrawal rollback injection")
        await original(session)

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", fail)
        with pytest.raises(RuntimeError, match="withdrawal rollback injection"):
            withdraw_call(client, h, prepared)
    recovered = client.get(ROOT + "/attempts/" + body["attempt_id"], headers=h).json()
    assert recovered == prepared

    async def absent():
        async with db.SessionLocal() as session:
            assert await session.get(a2a.Withdrawal, body["attempt_id"]) is None
            assert (
                await session.scalar(
                    select(db.Audit.id).where(
                        db.Audit.action == "isolated_a2a.withdraw", db.Audit.object == "attempt:" + body["attempt_id"]
                    )
                )
                is None
            )

    client.portal.call(absent)
    terminal = withdraw_call(client, h, prepared).json()
    assert client.post(ROOT + "/acknowledge", headers=h, json=evidence(terminal)).status_code == 200


def test_withdrawal_proof_corruption_and_orphan_effect_fail_closed(fixture, idp):
    client, _, agent, _ = fixture
    h, body = headers(idp), payload(agent)
    prepared = client.post(ROOT + "/prepare", headers=h, json=body).json()

    async def orphan(remove=False):
        async with db.SessionLocal() as session:
            if remove:
                await session.delete(await session.get(db.RunInput, prepared["receipt"]["run_id"]))
            else:
                session.add(db.RunInput(run_id=prepared["receipt"]["run_id"], messages=[], fingerprint="synthetic"))
            await session.commit()

    client.portal.call(orphan)
    assert withdraw_call(client, h, prepared).status_code == 409
    assert client.get(ROOT + "/attempts/" + body["attempt_id"], headers=h).json()["receipt"]["status"] == "PREPARED"
    client.portal.call(orphan, True)
    terminal = withdraw_call(client, h, prepared).json()

    async def change(delta):
        async with db.SessionLocal() as session:
            proof = await session.get(a2a.Withdrawal, body["attempt_id"])
            proof.revision += delta
            await session.commit()

    client.portal.call(change, 1)
    assert client.post(ROOT + "/acknowledge", headers=h, json=evidence(terminal)).status_code == 409
    assert client.get(ROOT + "/attempts/" + body["attempt_id"], headers=h).status_code == 409
    client.portal.call(change, -1)
    assert client.post(ROOT + "/acknowledge", headers=h, json=evidence(terminal)).status_code == 200

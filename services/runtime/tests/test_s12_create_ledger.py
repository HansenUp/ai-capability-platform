from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from sqlalchemy import select
from test_s7_run_api import _admin, _log, _publish, _user, _wait
from test_s7_run_api import idp as idp

from ai_contract import RunEventType, RunState
from runtime import credentials
from runtime.loop import runner
from runtime.state import db
from runtime.state.eventlog import EventLog, expired_unleased_runs


@pytest.fixture(autouse=True)
def isolate_create_runs(client: Any, idp: Any) -> Any:
    created: list[str] = []
    client.s12_create_runs = created
    yield
    try:
        worker = client.runtime_worker
        client.portal.call(retire_create_runs, created, worker)
    finally:
        del client.s12_create_runs


async def retire_create_runs(created: list[str], worker: Any) -> None:
    paused = worker.paused
    worker.paused = True
    try:
        await worker.idle.wait()
        async with db.SessionLocal() as session:
            for run_id in created:
                run = await session.get(db.Run, run_id)
                if run is not None and not RunState(run.state).terminal:
                    await EventLog(session).transition(run_id, RunState.CANCELED, "isolated create test cleanup")
                    await credentials.delete_credential(run_id, session)
            await session.commit()
        assert not set(created) & set(await expired_unleased_runs())
    finally:
        worker.paused = paused


def test_create_cleanup_uses_client_owned_worker_after_nested_lifespan(client: Any) -> None:
    from fastapi.testclient import TestClient

    from runtime.main import app

    original = app.state.node_lifecycle
    owned = client.runtime_worker
    try:
        with TestClient(app):
            nested = app.state.node_lifecycle.background.worker
            assert nested is not owned
            assert client.runtime_worker is owned
        assert app.state.node_lifecycle.background.worker is nested
        assert client.runtime_worker is owned
        client.portal.call(retire_create_runs, [], owned)
        assert not owned.paused
    finally:
        app.state.node_lifecycle = original


def scenario(client: Any, idp: Any) -> tuple[dict[str, str], str]:
    return _user(idp, "create-" + uuid.uuid4().hex[:10]), _publish(client, {"tools": ["echo"]}, _admin(idp))


def prepare(client: Any, owner: dict[str, str], agent: str, text: str = "synthetic create") -> dict[str, Any]:
    response = client.post(
        "/v1/workbench/create-attempts", headers=owner, json={"attempt_id": str(uuid.uuid4()), "agent_id": agent, "text": text}
    )
    assert response.status_code == 200, response.text
    row = response.json()
    client.s12_create_runs.append(row["run_id"])
    return row


def evidence(row: dict[str, Any]) -> dict[str, Any]:
    return {"fingerprint": row["fingerprint"], "revision": row["revision"]}


def test_create_prepare_reserves_only_and_recovery_needs_no_run_id(client: Any, idp: Any) -> None:
    owner, agent = scenario(client, idp)
    text = "PRIVATE-CREATE-SENTINEL"
    row = prepare(client, owner, agent, text)
    assert row["action"] == "create" and row["request_seq"] == 0 and row["status"] == "PREPARED"
    assert row["create_agent"] == agent and row["create_version"] == "1"
    assert text not in json.dumps(row)
    assert client.get("/v1/workbench/attempts", headers=owner).json()["active"] == row
    assert client.get(f"/v1/runs/{row['run_id']}", headers=owner).status_code == 404

    async def absent() -> None:
        async with db.SessionLocal() as session:
            assert await session.get(db.Run, row["run_id"]) is None
            assert await session.get(db.RunInput, row["run_id"]) is None
            assert await session.get(db.RunCredential, row["run_id"]) is None
            stored = await session.get(db.WorkbenchAttempt, row["attempt_id"])
            assert text not in json.dumps(
                {column.name: str(getattr(stored, column.name)) for column in db.WorkbenchAttempt.__table__.columns}
            )
            audits = (await session.execute(select(db.Audit).where(db.Audit.object == f"attempt:{row['attempt_id']}"))).scalars().all()
            assert text not in json.dumps([audit.detail for audit in audits])

    client.portal.call(absent)
    path = f"/v1/workbench/attempts/{row['attempt_id']}"
    assert client.post(path + "/acknowledge", headers=owner, json=evidence(row)).status_code == 409
    assert client.post("/v1/runs", headers=owner, json={"agent_id": agent, "input": [{"role": "user", "content": text}]}).status_code == 409
    assert client.post(path + "/execute", headers=owner, json=evidence(row)).status_code == 422
    assert client.post(path + "/execute", headers=owner, json=evidence(row) | {"value": "different"}).status_code == 409
    assert client.get("/v1/workbench/attempts", headers=owner).json()["active"] == row


def test_create_execute_atomic_receipt_and_owner_ack(client: Any, idp: Any) -> None:
    owner, agent = scenario(client, idp)
    text = "synthetic create"
    row = prepare(client, owner, agent, text)
    path = f"/v1/workbench/attempts/{row['attempt_id']}"
    response = client.post(path + "/execute", headers=owner, json=evidence(row) | {"value": text})
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["status"] == "COMMITTED" and receipt["evidence_seq"] == 1 and receipt["run_id"] == row["run_id"]
    _wait(client, row["run_id"], owner, "completed")
    assert client.get("/v1/workbench/attempts", headers=owner).json()["active"] == receipt
    assert client.post(path + "/execute", headers=owner, json=evidence(row) | {"value": text}).json() == receipt
    events = _log(client, row["run_id"], owner)
    assert len([event for event in events if event["type"] == "run.created"]) == 1
    assert events[0]["payload"]["workbench_attempt_id"] == row["attempt_id"]
    assert events[0]["payload"]["workbench_value_hmac"] == row["value_hmac"]
    other = _user(idp, "create-other")
    assert client.get(path, headers=other).status_code == 404
    assert client.post(path + "/acknowledge", headers=other, json=evidence(receipt)).status_code == 404
    assert client.post(path + "/acknowledge", headers=owner, json=evidence(receipt) | {"value": text}).status_code == 422
    assert client.post(path + "/acknowledge", headers=owner, json=evidence(receipt)).json()["acknowledged"] is True
    assert client.get("/v1/workbench/attempts", headers=owner).json()["active"] is None


@pytest.mark.parametrize("change", ["config", "retire", "selection"])
def test_create_changed_snapshot_rejects_without_run(client: Any, idp: Any, change: str) -> None:
    owner, agent = scenario(client, idp)
    row = prepare(client, owner, agent)

    async def mutate() -> None:
        async with db.SessionLocal() as session:
            current = (await session.execute(select(db.AgentVersion).where(db.AgentVersion.agent_id == agent))).scalar_one()
            if change == "config":
                current.config = {"tools": []}
            elif change == "retire":
                current.status = "retired"
            else:
                current.rollout_percent = 0
                session.add(
                    db.AgentVersion(agent_id=agent, version="2", tenant_id=current.tenant_id, status="published", rollout_percent=100)
                )
            await session.commit()

    client.portal.call(mutate)
    path = f"/v1/workbench/attempts/{row['attempt_id']}"
    response = client.post(path + "/execute", headers=owner, json=evidence(row) | {"value": "synthetic create"})
    assert response.status_code == 200
    receipt = response.json()
    assert receipt["status"] == "REJECTED_NO_EFFECT"
    assert client.get(f"/v1/runs/{row['run_id']}", headers=owner).status_code == 404
    assert client.post(path + "/acknowledge", headers=owner, json=evidence(receipt)).json()["acknowledged"] is True


def test_create_precommit_failure_rolls_back_run_input_credential_and_event(client: Any, idp: Any, monkeypatch: Any) -> None:
    owner, agent = scenario(client, idp)
    row = prepare(client, owner, agent)
    original = runner.persist_run

    async def crash(*args: Any, **kwargs: Any) -> Any:
        await original(*args, **kwargs)
        raise RuntimeError("synthetic create precommit crash")

    with monkeypatch.context() as patch:
        patch.setattr(runner, "persist_run", crash)
        with pytest.raises(RuntimeError, match="synthetic create precommit crash"):
            client.post(
                f"/v1/workbench/attempts/{row['attempt_id']}/execute", headers=owner, json=evidence(row) | {"value": "synthetic create"}
            )

    async def absent() -> None:
        async with db.SessionLocal() as session:
            for table in (db.Run, db.RunInput, db.RunCredential):
                assert await session.get(table, row["run_id"]) is None
            assert await EventLog(session).list(row["run_id"]) == []

    client.portal.call(absent)
    assert client.get("/v1/workbench/attempts", headers=owner).json()["active"] == row


def test_create_concurrent_same_attempt_creates_one_run(client: Any, idp: Any) -> None:
    owner, agent = scenario(client, idp)
    row = prepare(client, owner, agent)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda _: client.post(
                    f"/v1/workbench/attempts/{row['attempt_id']}/execute", headers=owner, json=evidence(row) | {"value": "synthetic create"}
                ),
                range(2),
            )
        )
    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    _wait(client, row["run_id"], owner, "completed")
    assert len([event for event in _log(client, row["run_id"], owner) if event["type"] == RunEventType.RUN_CREATED]) == 1


def test_create_wrong_binding_tenant_extra_fields_and_duplicate_prepare(client: Any, idp: Any) -> None:
    owner, agent = scenario(client, idp)
    body = {"attempt_id": str(uuid.uuid4()), "agent_id": agent, "text": "synthetic create"}
    assert (
        client.post("/v1/workbench/create-attempts", headers=_user(idp, "foreign-create", tenant="foreign"), json=body).status_code == 404
    )
    assert client.post("/v1/workbench/create-attempts", headers=owner, json=body | {"attachments": []}).status_code == 422
    assert client.post("/v1/workbench/create-attempts", headers=owner, json=body | {"text": " "}).status_code == 422
    response = client.post("/v1/workbench/create-attempts", headers=owner, json=body)
    assert response.status_code == 200
    row = response.json()
    client.s12_create_runs.append(row["run_id"])
    assert client.post("/v1/workbench/create-attempts", headers=owner, json=body).json() == row
    assert client.post("/v1/workbench/create-attempts", headers=owner, json=body | {"text": "changed"}).status_code == 409
    assert client.post("/v1/workbench/create-attempts", headers=owner, json=body | {"attempt_id": str(uuid.uuid4())}).status_code == 409


def test_create_conflicting_terminal_snapshot_cannot_unlock(client: Any, idp: Any) -> None:
    owner, agent = scenario(client, idp)
    row = prepare(client, owner, agent)
    path = f"/v1/workbench/attempts/{row['attempt_id']}"
    receipt = client.post(path + "/execute", headers=owner, json=evidence(row) | {"value": "synthetic create"}).json()
    _wait(client, row["run_id"], owner, "completed")

    async def corrupt() -> None:
        async with db.SessionLocal() as session:
            event = await session.get(db.RunEventRow, (row["run_id"], 1))
            event.payload = event.payload | {"workbench_snapshot_hash": "0" * 64}
            await session.commit()

    client.portal.call(corrupt)
    assert client.post(path + "/acknowledge", headers=owner, json=evidence(receipt)).status_code == 409


def test_create_digest_key_change_keeps_prepared_lock(client: Any, idp: Any, monkeypatch: Any) -> None:
    from dataclasses import replace

    from runtime.loop import toolbroker

    owner, agent = scenario(client, idp)
    row = prepare(client, owner, agent)
    with monkeypatch.context() as patch:
        patch.setattr(toolbroker, "settings", replace(toolbroker.settings, hook_digest_key="isolated-rotated-create-key"))
        response = client.post(
            f"/v1/workbench/attempts/{row['attempt_id']}/execute", headers=owner, json=evidence(row) | {"value": "synthetic create"}
        )
        assert response.status_code == 409
        assert client.get("/v1/workbench/attempts", headers=owner).json()["active"] == row
        assert client.get(f"/v1/runs/{row['run_id']}", headers=owner).status_code == 404


def test_legacy_already_admitted_create_is_not_retroactively_frozen(client: Any, idp: Any, monkeypatch: Any) -> None:
    import asyncio

    from runtime.api import messages

    owner, agent = scenario(client, idp)
    entered, release = threading.Event(), threading.Event()
    original = messages._load_agent
    first = True

    async def admitted(*args: Any, **kwargs: Any) -> Any:
        nonlocal first
        if first:
            first = False
            entered.set()
            assert await asyncio.to_thread(release.wait, 5)
        return await original(*args, **kwargs)

    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=1) as pool:
        patch.setattr(messages, "_load_agent", admitted)
        pending = pool.submit(
            client.post, "/v1/runs", headers=owner, json={"agent_id": agent, "input": [{"role": "user", "content": "admitted synthetic"}]}
        )
        try:
            assert entered.wait(5)
            row = prepare(client, owner, agent)
        finally:
            release.set()
        response = pending.result(timeout=5)
    assert response.status_code == 202
    client.s12_create_runs.append(response.json()["run_id"])
    assert response.json()["run_id"] != row["run_id"]
    assert client.get("/v1/workbench/attempts", headers=owner).json()["active"] == row
    assert client.get(f"/v1/runs/{row['run_id']}", headers=owner).status_code == 404


@pytest.mark.parametrize("paused", [False, True])
async def test_create_cleanup_quiesces_writer_before_reading_state(paused: bool) -> None:
    import asyncio

    from runtime.worker.queue import Delivery, InMemoryQueue
    from runtime.worker.settings import WorkerSettings
    from runtime.worker.worker import Worker

    run_id = "cleanup-" + uuid.uuid4().hex
    async with db.SessionLocal() as session:
        session.add(db.Run(run_id=run_id, agent_id="demo", agent_version="1", model_alias="m", subject="u", durable=True))
        await session.commit()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def drive(target: str) -> Any:
        async with db.SessionLocal() as session:
            await EventLog(session).transition(target, RunState.WORKING, "cleanup barrier")
            await session.commit()
        entered.set()
        await finish.wait()
        async with db.SessionLocal() as session:
            await EventLog(session).transition(target, RunState.COMPLETED, "cleanup barrier")
            await session.commit()
        yield None

    worker = Worker(InMemoryQueue(), WorkerSettings(heartbeat_s=1000), drive=drive)
    worker.paused = paused
    await worker._slots.acquire()
    worker._spawn(Delivery("cleanup", run_id, "test"))
    cleanup = None
    try:
        await entered.wait()
        cleanup = asyncio.create_task(retire_create_runs([run_id], worker))
        await asyncio.sleep(0)
        assert worker.paused and not cleanup.done()
        finish.set()
        await cleanup
        assert worker.paused is paused and worker.idle.is_set() and not worker._tasks
        async with db.SessionLocal() as session:
            row = await session.get(db.Run, run_id)
            assert row is not None and row.state == RunState.COMPLETED.value
            events = await EventLog(session).list(run_id)
            assert [event.payload["to"] for event in events if event.type == RunEventType.RUN_STATE_CHANGED] == ["working", "completed"]
    finally:
        finish.set()
        if cleanup is not None:
            await cleanup
        await worker.drain()

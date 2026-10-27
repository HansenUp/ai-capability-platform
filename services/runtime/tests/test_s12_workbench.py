import json
import time
from typing import Any

import pytest
from test_s7_run_api import SCHEMA, _admin, _create, _log, _publish, _user, _wait
from test_s7_run_api import idp as idp

from runtime import credentials
from runtime.loop import toolbroker
from runtime.state import db


def test_workbench_answer_is_initiator_only_and_resumes_same_run(client: Any, idp: Any) -> None:
    alice, bob = _user(idp, "s12-input-alice"), _user(idp, "s12-input-bob")
    agent = _publish(client, {"tools": ["echo"], "user_input": True}, _admin(idp))
    content = "!tool request_user_input " + json.dumps({"prompt": "哪个合成工单？", "schema": SCHEMA})
    run_id = _create(client, agent, content, alice)
    _wait(client, run_id, alice, "input-required")
    asked = next(row for row in _log(client, run_id, alice) if row["type"] == "input.requested")
    body = {"kind": "answer", "call_id": asked["payload"]["call_id"], "value": {"id": "SYNTHETIC-1"}}
    path = f"/v1/runs/{run_id}/input"
    assert client.post(path, headers=bob, json=body).status_code == 403
    assert client.post(path, headers=alice, json=body | {"value": {"unexpected": True}}).status_code == 400
    assert client.post(path, headers=alice, json=body | {"call_id": "wrong"}).status_code == 409
    assert client.post(path, headers=_user(idp, "foreign", tenant="s12-foreign"), json=body).status_code == 404
    assert client.post(path, headers=alice, json=body).status_code == 200
    _wait(client, run_id, alice, "completed")
    results = [row for row in _log(client, run_id, alice) if row["type"] == "input.provided"]
    assert len(results) == 1
    assert client.post(path, headers=alice, json=body).status_code == 409


def test_expired_stored_credential_refresh_is_initiator_only_and_resumes_same_run(client: Any, idp: Any) -> None:
    alice = _user(idp, "s12-credential-alice")
    bob = _user(idp, "s12-credential-bob")
    expired = _user(idp, "s12-credential-alice", exp=int(time.time()) - 120)
    foreign = _user(idp, "s12-credential-alice", tenant="s12-foreign")
    agent = _publish(client, {"tools": ["echo"], "user_input": True}, _admin(idp))
    run_id = _create(client, agent, '!tool request_user_input {"prompt":"synthetic expiry preparation"}', alice)
    _wait(client, run_id, alice, "input-required")
    owner = client.get(f"/v1/runs/{run_id}", headers=alice).json()["subject"]

    async def expire_stored() -> None:
        async with db.SessionLocal() as session:
            await credentials.store_credential(session, run_id, owner, expired["Authorization"].removeprefix("Bearer "))
            await session.commit()
        stored = await credentials.load_credential(run_id)
        assert stored is not None and not credentials.is_fresh(stored.expires_at)

    client.portal.call(expire_stored)
    asked = next(row["payload"] for row in _log(client, run_id, alice) if row["type"] == "input.requested")
    path = f"/v1/runs/{run_id}/input"
    assert client.post(path, headers=alice, json={"kind": "answer", "call_id": asked["call_id"], "value": "synthetic"}).status_code == 200
    _wait(client, run_id, alice, "input-required")
    history = _log(client, run_id, alice)
    assert [row["payload"]["kind"] for row in history if row["type"] == "input.requested"] == ["answer", "credential"]
    body = {"kind": "credential"}
    assert client.post(path, headers=expired, json=body).status_code == 401
    assert client.post(path, headers=bob, json=body).status_code == 403
    assert client.post(path, headers=foreign, json=body).status_code == 404
    assert not [row for row in _log(client, run_id, alice) if row["type"] == "input.provided" and row["payload"]["kind"] == "credential"]
    assert client.post(path, headers=alice, json=body | {"value": "ignored-body-not-a-token"}).status_code == 200
    completed = _wait(client, run_id, alice, "completed")
    assert completed["run_id"] == run_id and completed["subject"] == owner
    history = _log(client, run_id, alice)
    assert len([row for row in history if row["type"] == "run.created"]) == 1
    assert len([row for row in history if row["type"] == "input.provided" and row["payload"]["kind"] == "credential"]) == 1
    assert "ignored-body-not-a-token" not in json.dumps(history)
    assert client.portal.call(credentials.load_credential, run_id) is None
    assert client.post(path, headers=alice, json=body).status_code == 409


@pytest.mark.auto_resume
@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_workbench_approval_binds_fingerprint_and_never_self_approves(client: Any, idp: Any, monkeypatch: Any, decision: str) -> None:
    executions: list[dict[str, Any]] = []

    async def synthetic(tool: Any, arguments: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
        if dry_run:
            return {"impact_preview": {"simulation": True}}
        executions.append(arguments)
        return {"simulation": True, "external_execution": False}

    monkeypatch.setattr(toolbroker, "_dev_execute", synthetic)
    alice, bob = _user(idp, f"s12-{decision}-alice"), _user(idp, f"s12-{decision}-bob")
    run_id = _create(client, "demo", '!tool ticket.close_batch {"ids":["SYNTHETIC-ONLY"]}', alice)
    _wait(client, run_id, alice, "auth-required")
    events = _log(client, run_id, bob)
    gate = next(row for row in events if row["type"] == "tool.decision")
    body = {"call_id": gate["payload"]["call_id"], "params_hash": gate["payload"]["params_hash"]}
    path = f"/v1/runs/{run_id}/{decision}"
    assert client.post(path, headers=alice, json=body).status_code == 403
    assert client.post(path, headers=bob, json=body | {"params_hash": "wrong"}).status_code == 409
    assert executions == []
    assert client.post(path, headers=bob, json=body).status_code == 200
    _wait(client, run_id, alice, "completed" if decision == "approve" else "failed")
    assert len(executions) == (1 if decision == "approve" else 0)
    assert client.post(path, headers=bob, json=body).status_code == 409

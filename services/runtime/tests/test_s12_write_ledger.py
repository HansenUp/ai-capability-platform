import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from sqlalchemy import delete
from test_s7_run_api import _admin, _create, _log, _publish, _user, _wait
from test_s7_run_api import idp as idp

from runtime import credentials
from runtime.loop import user_input
from runtime.state import db


def scenario(client: Any, idp: Any) -> tuple[dict[str, str], dict[str, str], str]:
    name = f"ledger-{uuid.uuid4().hex}"
    owner = _user(idp, name)
    fresh = _user(idp, name, jti=uuid.uuid4().hex)
    agent = _publish(client, {"tools": ["echo"], "user_input": True}, _admin(idp))
    run_id = _create(client, agent, '!tool request_user_input {"prompt":"ledger preparation"}', owner)
    _wait(client, run_id, owner, "input-required")
    subject = client.get(f"/v1/runs/{run_id}", headers=owner).json()["subject"]
    expired = _user(idp, name, exp=int(time.time()) - 120)

    async def expire() -> None:
        async with db.SessionLocal() as session:
            await credentials.store_credential(session, run_id, subject, expired["Authorization"].removeprefix("Bearer "))
            await session.commit()

    client.portal.call(expire)
    request = next(row["payload"] for row in _log(client, run_id, owner) if row["type"] == "input.requested")
    assert (
        client.post(
            f"/v1/runs/{run_id}/input", headers=owner, json={"kind": "answer", "call_id": request["call_id"], "value": "synthetic"}
        ).status_code
        == 200
    )
    _wait(client, run_id, owner, "input-required")
    return owner, fresh, run_id


def prepare(client: Any, headers: dict[str, str], run_id: str) -> dict[str, Any]:
    response = client.post("/v1/workbench/attempts", headers=headers, json={"attempt_id": str(uuid.uuid4()), "run_id": run_id})
    assert response.status_code == 200, response.text
    return response.json()


def body(row: dict[str, Any]) -> dict[str, Any]:
    return {"fingerprint": row["fingerprint"], "revision": row["revision"]}


def test_ledger_blocks_all_legacy_writes_and_requires_explicit_exact_receipt(client: Any, idp: Any) -> None:
    owner, fresh, run_id = scenario(client, idp)
    row = prepare(client, owner, run_id)
    base = f"/v1/workbench/attempts/{row['attempt_id']}"
    assert (
        client.get("/v1/workbench/identity", headers=owner).json()["scope_id"]
        == client.get("/v1/workbench/identity", headers=fresh).json()["scope_id"]
    )
    for path, payload in [
        ("/v1/runs", {"agent_id": "demo", "input": [{"role": "user", "content": "must not create"}]}),
        (f"/v1/runs/{run_id}/input", {"kind": "credential"}),
        (f"/v1/runs/{run_id}/approve", {"call_id": "x", "params_hash": "x"}),
        (f"/v1/runs/{run_id}/reject", {"call_id": "x", "params_hash": "x"}),
        (f"/v1/runs/{run_id}/resume", {}),
    ]:
        assert client.post(path, headers=fresh, json=payload).status_code == 409
    assert client.post(base + "/acknowledge", headers=fresh, json=body(row)).status_code == 409
    committed = client.post(base + "/execute", headers=fresh, json=body(row))
    assert committed.status_code == 200
    receipt = committed.json()
    assert receipt["status"] == "COMMITTED" and receipt["evidence_seq"] is not None
    _wait(client, run_id, fresh, "completed")
    assert client.get("/v1/workbench/attempts", headers=fresh).json()["active"]["attempt_id"] == row["attempt_id"]
    assert client.post(base + "/execute", headers=fresh, json=body(row)).json() == receipt
    assert client.post(base + "/acknowledge", headers=fresh, json=body(row)).status_code == 409
    assert client.post(base + "/acknowledge", headers=fresh, json=body(receipt) | {"fingerprint": "0" * 64}).status_code == 409
    acknowledged = client.post(base + "/acknowledge", headers=fresh, json=body(receipt))
    assert acknowledged.status_code == 200 and acknowledged.json()["acknowledged"] is True
    assert client.get("/v1/workbench/attempts", headers=fresh).json()["active"] is None
    assert client.post(base + "/execute", headers=fresh, json=body(row)).json()["acknowledged"] is True
    assert client.post(base + "/acknowledge", headers=fresh, json=body(receipt)).json()["acknowledged"] is True
    events = _log(client, run_id, fresh)
    assert len([event for event in events if event["payload"].get("workbench_attempt_id") == row["attempt_id"]]) == 1
    assert len([event for event in events if event["type"] == "run.created"]) == 1


def test_ledger_isolates_actual_actor_tenant_and_payload(client: Any, idp: Any) -> None:
    owner, fresh, run_id = scenario(client, idp)
    row = prepare(client, owner, run_id)
    stranger = _user(idp, "other-ledger-user")
    foreign = _user(idp, "other-ledger-user", tenant="foreign-ledger")
    for headers in [stranger, foreign, _admin(idp)]:
        assert client.get(f"/v1/workbench/attempts/{row['attempt_id']}", headers=headers).status_code == 404
        assert client.post(f"/v1/workbench/attempts/{row['attempt_id']}/execute", headers=headers, json=body(row)).status_code == 404
        assert client.post(f"/v1/workbench/attempts/{row['attempt_id']}/acknowledge", headers=headers, json=body(row)).status_code == 404
        assert client.get("/v1/workbench/attempts", headers=headers).json()["active"] is None
    rejected = client.post("/v1/workbench/attempts", headers=stranger, json={"attempt_id": str(uuid.uuid4()), "run_id": run_id})
    assert rejected.status_code == 403
    assert (
        client.post(
            f"/v1/workbench/attempts/{row['attempt_id']}/execute", headers=fresh, json=body(row) | {"value": "not-storable"}
        ).status_code
        == 422
    )
    serialized = json.dumps(client.get("/v1/workbench/attempts", headers=fresh).json())
    assert "Authorization" not in serialized and "eyJ" not in serialized and "not-storable" not in serialized


def test_ledger_effect_and_receipt_roll_back_together(client: Any, idp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    owner, fresh, run_id = scenario(client, idp)
    row = prepare(client, owner, run_id)
    original = user_input.apply_input

    async def crash(*args: Any, **kwargs: Any) -> dict[str, Any]:
        await original(*args, **kwargs)
        raise RuntimeError("synthetic precommit failure")

    with monkeypatch.context() as patch:
        patch.setattr(user_input, "apply_input", crash)
        with pytest.raises(RuntimeError, match="synthetic precommit failure"):
            client.post(f"/v1/workbench/attempts/{row['attempt_id']}/execute", headers=fresh, json=body(row))
    assert client.get(f"/v1/runs/{run_id}", headers=fresh).json()["state"] == "input-required"
    assert client.get("/v1/workbench/attempts", headers=fresh).json()["active"]["status"] == "PREPARED"
    assert not [event for event in _log(client, run_id, fresh) if event["payload"].get("workbench_attempt_id") == row["attempt_id"]]


def test_ledger_concurrent_same_attempt_executes_once(client: Any, idp: Any) -> None:
    owner, fresh, run_id = scenario(client, idp)
    row = prepare(client, owner, run_id)
    path = f"/v1/workbench/attempts/{row['attempt_id']}/execute"
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: client.post(path, headers=fresh, json=body(row)), range(2)))
    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    _wait(client, run_id, fresh, "completed")
    assert len([event for event in _log(client, run_id, fresh) if event["payload"].get("workbench_attempt_id") == row["attempt_id"]]) == 1


def test_ledger_missing_evidence_cannot_unlock(client: Any, idp: Any) -> None:
    owner, fresh, run_id = scenario(client, idp)
    row = prepare(client, owner, run_id)
    path = f"/v1/workbench/attempts/{row['attempt_id']}"
    receipt = client.post(path + "/execute", headers=fresh, json=body(row)).json()
    _wait(client, run_id, fresh, "completed")

    async def lose_evidence() -> None:
        async with db.SessionLocal() as session:
            await session.execute(
                delete(db.RunEventRow).where(db.RunEventRow.run_id == run_id, db.RunEventRow.seq == receipt["evidence_seq"])
            )
            await session.commit()

    client.portal.call(lose_evidence)
    assert client.post(path + "/acknowledge", headers=fresh, json=body(receipt)).status_code == 409
    assert client.get("/v1/workbench/attempts", headers=fresh).json()["active"] is not None


def test_ledger_rejected_receipt_requires_owner_ack(client: Any, idp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    owner, fresh, run_id = scenario(client, idp)
    row = prepare(client, owner, run_id)
    path = f"/v1/workbench/attempts/{row['attempt_id']}"

    async def reject(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise user_input.InputNotPending("synthetic stale request")

    with monkeypatch.context() as patch:
        patch.setattr(user_input, "apply_input", reject)
        response = client.post(path + "/execute", headers=fresh, json=body(row))
    assert response.status_code == 200
    receipt = response.json()
    assert receipt["status"] == "REJECTED_NO_EFFECT"
    assert receipt["evidence_seq"] is None
    assert client.get("/v1/workbench/attempts", headers=fresh).json()["active"] == receipt
    assert client.get(f"/v1/runs/{run_id}", headers=fresh).json()["state"] == "input-required"
    assert client.post(path + "/acknowledge", headers=fresh, json=body(receipt)).json()["acknowledged"] is True
    assert client.get("/v1/workbench/attempts", headers=fresh).json()["active"] is None


def test_ledger_persists_across_database_connection_replacement(client: Any, idp: Any) -> None:
    owner, fresh, run_id = scenario(client, idp)
    row = prepare(client, owner, run_id)

    async def reconnect() -> None:
        await db.engine.dispose()

    client.portal.call(reconnect)
    assert client.get("/v1/workbench/attempts", headers=fresh).json()["active"] == row
    duplicate = client.post("/v1/workbench/attempts", headers=fresh, json={"attempt_id": row["attempt_id"], "run_id": run_id})
    assert duplicate.json() == row
    assert client.post("/v1/workbench/attempts", headers=fresh, json={"attempt_id": str(uuid.uuid4()), "run_id": run_id}).status_code == 409


def test_ledger_validation_failure_rolls_back_partial_input(client: Any, idp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    owner, fresh, run_id = scenario(client, idp)
    row = prepare(client, owner, run_id)
    original = user_input.apply_input

    async def reject_after_input(*args: Any, **kwargs: Any) -> dict[str, Any]:
        await original(*args, **kwargs)
        raise ValueError("synthetic validation failure after input")

    with monkeypatch.context() as patch:
        patch.setattr(user_input, "apply_input", reject_after_input)
        response = client.post(f"/v1/workbench/attempts/{row['attempt_id']}/execute", headers=fresh, json=body(row))
    assert response.status_code == 200
    assert response.json()["status"] == "REJECTED_NO_EFFECT"
    assert client.get(f"/v1/runs/{run_id}", headers=fresh).json()["state"] == "input-required"
    assert not [event for event in _log(client, run_id, fresh) if event["payload"].get("workbench_attempt_id") == row["attempt_id"]]


def test_ledger_lost_execute_response_recovers_readonly_without_unlock(client: Any, idp: Any) -> None:
    owner, fresh, run_id = scenario(client, idp)
    prepared = prepare(client, owner, run_id)
    path = f"/v1/workbench/attempts/{prepared['attempt_id']}"
    assert client.post(path + "/execute", headers=fresh, json=body(prepared)).status_code == 200
    del prepared

    async def reconnect() -> None:
        await db.engine.dispose()

    client.portal.call(reconnect)
    recovered = client.get("/v1/workbench/attempts", headers=fresh).json()["active"]
    assert recovered["status"] == "COMMITTED" and recovered["acknowledged"] is False
    _wait(client, run_id, fresh, "completed")
    assert client.get(path, headers=fresh).json() == recovered
    assert client.post(f"/v1/runs/{run_id}/input", headers=fresh, json={"kind": "credential"}).status_code == 409
    assert client.post(path + "/acknowledge", headers=fresh, json=body(recovered)).json()["acknowledged"] is True
    assert (
        len([event for event in _log(client, run_id, fresh) if event["payload"].get("workbench_attempt_id") == recovered["attempt_id"]])
        == 1
    )

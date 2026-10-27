import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_s7_run_api import _admin, _create, _log, _publish, _user, _wait
from test_s7_run_api import idp as idp

from ai_contract import RunEventType, RunState
from runtime.api import auth, messages
from runtime.state import db
from runtime.state.eventlog import EventLog, expired_unleased_runs


@pytest.fixture(autouse=True)
def isolate_approval_runs(client: Any, idp: Any) -> Iterator[None]:
    created: list[tuple[dict[str, str], str]] = []
    client.s12_approval_runs = created
    yield
    try:
        for owner, run_id in created:
            response = client.get(f"/v1/runs/{run_id}", headers=owner)
            assert response.status_code == 200, response.text
            if not RunState(response.json()["state"]).terminal:
                canceled = client.post(f"/v1/runs/{run_id}/cancel", headers=owner)
                assert canceled.status_code == 200, canceled.text
                assert canceled.json()["state"] == "canceled"
        candidates = client.portal.call(expired_unleased_runs)
        assert not {run_id for _, run_id in created}.intersection(candidates)
    finally:
        del client.s12_approval_runs


def scenario(client: Any, idp: Any) -> tuple[dict[str, str], dict[str, str], str, dict[str, Any]]:
    suffix = uuid.uuid4().hex
    owner, reviewer = _user(idp, f"owner-{suffix}"), _user(idp, f"reviewer-{suffix}")
    agent = _publish(client, {"tools": ["ticket.close_batch"]}, _admin(idp))
    run_id = _create(client, agent, '!tool ticket.close_batch {"ids":["synthetic"]}', owner)
    client.s12_approval_runs.append((owner, run_id))
    _wait(client, run_id, owner, "auth-required")
    decision = next(event for event in _log(client, run_id, owner) if event["type"] == "tool.decision")
    return owner, reviewer, run_id, {"call_id": decision["payload"]["call_id"], "params_hash": decision["payload"]["params_hash"]}


def prepare(client: Any, headers: dict[str, str], run_id: str, call: dict[str, Any], action: str) -> dict[str, Any]:
    response = client.post(
        "/v1/workbench/attempts", headers=headers, json={"attempt_id": str(uuid.uuid4()), "run_id": run_id, "action": action, **call}
    )
    assert response.status_code == 200, response.text
    return response.json()


def evidence(row: dict[str, Any]) -> dict[str, Any]:
    return {"fingerprint": row["fingerprint"], "revision": row["revision"]}


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_approval_ledger_binds_reviewer_and_reuses_exact_rules(client: Any, idp: Any, monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    owner, reviewer, run_id, call = scenario(client, idp)
    request = {"attempt_id": str(uuid.uuid4()), "run_id": run_id, "action": action, **call}
    assert client.post("/v1/workbench/attempts", headers=owner, json=request).status_code == 403
    assert client.post("/v1/workbench/attempts", headers=_user(idp, "foreign", tenant="other"), json=request).status_code == 404
    assert client.post("/v1/workbench/attempts", headers=reviewer, json=request | {"params_hash": "wrong"}).status_code == 409
    assert client.post("/v1/workbench/attempts", headers=reviewer, json=request | {"ttl_seconds": 999999}).status_code == 422
    row = prepare(client, reviewer, run_id, call, action)
    path = f"/v1/workbench/attempts/{row['attempt_id']}"
    assert row["action"] == action and row["call_id"] == call["call_id"] and row["params_hash"] == call["params_hash"]
    assert row["scope_id"] != client.get("/v1/workbench/identity", headers=owner).json()["scope_id"]
    assert client.get("/v1/workbench/attempts", headers=owner).json()["active"] is None
    assert client.get(path, headers=owner).status_code == 404
    assert client.post(f"/v1/runs/{run_id}/{action}", headers=reviewer, json=call).status_code == 409
    assert (
        client.post(
            "/v1/workbench/attempts",
            headers=reviewer,
            json=request | {"attempt_id": row["attempt_id"], "action": "reject" if action == "approve" else "approve"},
        ).status_code
        == 409
    )
    queued = []

    async def enqueue(target: str) -> None:
        queued.append(target)

    monkeypatch.setattr(messages, "continue_after_approval", enqueue)
    before = datetime.now(UTC)
    response = client.post(path + "/execute", headers=reviewer, json=evidence(row))
    after = datetime.now(UTC)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["status"] == "COMMITTED" and receipt["acknowledged"] is False
    assert client.post(path + "/execute", headers=reviewer, json=evidence(row)).json() == receipt
    events = _log(client, run_id, owner)
    applied = [event for event in events if event["payload"].get("workbench_attempt_id") == row["attempt_id"]]
    assert len(applied) == 1 and applied[0]["type"] == ("tool.approved" if action == "approve" else "tool.rejected")
    payload = applied[0]["payload"]
    assert payload["approver"] == client.get("/v1/workbench/identity", headers=reviewer).json()["subject"]
    assert payload["params_hash"] == call["params_hash"] and payload["comment"] == ""
    if action == "approve":
        expires = datetime.fromisoformat(payload["expires_at"])
        assert before + timedelta(seconds=900) <= expires <= after + timedelta(seconds=900)
        assert queued == [run_id]
    else:
        assert queued == []
        assert client.get(f"/v1/runs/{run_id}", headers=owner).json()["state"] == "failed"

        async def credential() -> Any:
            async with db.SessionLocal() as session:
                return await session.get(db.RunCredential, run_id)

        assert client.portal.call(credential) is None
    assert client.post(path + "/acknowledge", headers=owner, json=evidence(receipt)).status_code == 404
    assert client.post(path + "/acknowledge", headers=reviewer, json=evidence(receipt)).json()["acknowledged"] is True
    assert client.get("/v1/workbench/attempts", headers=reviewer).json()["active"] is None
    assert len([event for event in _log(client, run_id, owner) if event["payload"].get("workbench_attempt_id") == row["attempt_id"]]) == 1


def test_approval_new_decision_rejects_old_attempt_without_effect(client: Any, idp: Any) -> None:
    owner, reviewer, run_id, call = scenario(client, idp)
    row = prepare(client, reviewer, run_id, call, "approve")

    async def changed() -> None:
        async with db.SessionLocal() as session:
            await EventLog(session).append(run_id, RunEventType.TOOL_DECISION, {**call, "decision": "require_approval"})
            await session.commit()

    client.portal.call(changed)
    path = f"/v1/workbench/attempts/{row['attempt_id']}"
    receipt = client.post(path + "/execute", headers=reviewer, json=evidence(row)).json()
    assert receipt["status"] == "REJECTED_NO_EFFECT"
    assert client.get(f"/v1/runs/{run_id}", headers=owner).json()["state"] == "auth-required"
    assert not [event for event in _log(client, run_id, owner) if event["payload"].get("workbench_attempt_id") == row["attempt_id"]]
    assert client.post(path + "/acknowledge", headers=reviewer, json=evidence(receipt)).json()["acknowledged"] is True


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_approval_partial_write_rolls_back_with_receipt(client: Any, idp: Any, monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    owner, reviewer, run_id, call = scenario(client, idp)
    row = prepare(client, reviewer, run_id, call, action)
    original = messages.apply_approval

    async def fail(*args: Any, **kwargs: Any) -> Any:
        await original(*args, **kwargs)
        raise RuntimeError("synthetic approval precommit crash")

    with monkeypatch.context() as patch:
        patch.setattr(messages, "apply_approval", fail)
        with pytest.raises(RuntimeError, match="synthetic approval precommit crash"):
            client.post(f"/v1/workbench/attempts/{row['attempt_id']}/execute", headers=reviewer, json=evidence(row))
    assert client.get(f"/v1/runs/{run_id}", headers=owner).json()["state"] == "auth-required"
    assert client.get("/v1/workbench/attempts", headers=reviewer).json()["active"]["status"] == "PREPARED"
    assert not [event for event in _log(client, run_id, owner) if event["payload"].get("workbench_attempt_id") == row["attempt_id"]]


def test_approval_concurrent_replay_has_one_event(client: Any, idp: Any) -> None:
    owner, reviewer, run_id, call = scenario(client, idp)
    row = prepare(client, reviewer, run_id, call, "approve")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: client.post(f"/v1/workbench/attempts/{row['attempt_id']}/execute", headers=reviewer, json=evidence(row)), range(2)
            )
        )
    assert [result.status_code for result in results] == [200, 200]
    assert results[0].json() == results[1].json()
    assert len([event for event in _log(client, run_id, owner) if event["payload"].get("workbench_attempt_id") == row["attempt_id"]]) == 1


@pytest.mark.parametrize("field,value", [("params_hash", "different"), ("approver", "human:someone-else")])
def test_approval_conflicting_terminal_evidence_cannot_ack(client: Any, idp: Any, field: str, value: str) -> None:
    owner, reviewer, run_id, call = scenario(client, idp)
    row = prepare(client, reviewer, run_id, call, "reject")
    path = f"/v1/workbench/attempts/{row['attempt_id']}"
    receipt = client.post(path + "/execute", headers=reviewer, json=evidence(row)).json()

    async def corrupt() -> None:
        async with db.SessionLocal() as session:
            event = await session.get(db.RunEventRow, (run_id, receipt["evidence_seq"]))
            event.payload = event.payload | {field: value}
            await session.commit()

    client.portal.call(corrupt)
    assert client.post(path + "/acknowledge", headers=reviewer, json=evidence(receipt)).status_code == 409
    assert client.get("/v1/workbench/attempts", headers=reviewer).json()["active"] is not None


def test_verified_approval_protocol_never_uses_dev_self_approval_bypass(client: Any, idp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    owner, reviewer, run_id, call = scenario(client, idp)
    request = {"attempt_id": str(uuid.uuid4()), "run_id": run_id, "action": "approve", **call}
    with monkeypatch.context() as patch:
        patch.setattr(auth, "settings", replace(auth.settings, auth_required=False))
        for headers in [owner, reviewer, {"X-AI-Subject": "human:dev"}]:
            response = client.post("/v1/workbench/attempts", headers=headers, json=request)
            assert response.status_code == 403
            assert response.json()["detail"] == "workbench requires a verified user token"
            assert client.get("/v1/workbench/identity", headers=headers).status_code == 403
    accepted = client.post("/v1/workbench/attempts", headers=reviewer, json=request)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "PREPARED"

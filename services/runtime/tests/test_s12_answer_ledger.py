import json
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from sqlalchemy import select
from test_s7_run_api import _admin, _create, _log, _publish, _user, _wait
from test_s7_run_api import idp as idp

from ai_contract import RunEventType, RunState
from runtime import credentials
from runtime.loop import user_input
from runtime.state import db
from runtime.state.eventlog import EventLog, expired_unleased_runs


@pytest.fixture(autouse=True)
def isolate_answer_runs(client: Any, idp: Any) -> Iterator[None]:
    created: list[str] = []
    client.s12_answer_runs = created
    yield
    try:

        async def retire() -> None:
            async with db.SessionLocal() as session:
                for run_id in created:
                    run = await session.get(db.Run, run_id)
                    if run is not None and not RunState(run.state).terminal:
                        await EventLog(session).transition(run_id, RunState.CANCELED, "isolated test cleanup")
                        await credentials.delete_credential(run_id, session)
                await session.commit()
            assert not set(created).intersection(await expired_unleased_runs())

        client.portal.call(retire)
    finally:
        del client.s12_answer_runs


def scenario(client: Any, idp: Any, *, hooks: bool = False, schema: Any = None) -> tuple[dict[str, str], str, str]:
    owner = _user(idp, f"answer-{uuid.uuid4().hex}")
    config: dict[str, Any] = {"tools": ["echo"], "user_input": True}
    if hooks:
        config["hooks"] = [{"point": "post_tool", "name": "redact_pii", "params": {"kinds": ["email"]}}]
    agent = _publish(client, config, _admin(idp))
    request = {
        "prompt": "synthetic answer",
        "schema": schema or {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    }
    run_id = _create(client, agent, "!tool request_user_input " + json.dumps(request), owner)
    client.s12_answer_runs.append(run_id)
    _wait(client, run_id, owner, "input-required")
    event = next(event for event in _log(client, run_id, owner) if event["type"] == "input.requested")
    return owner, run_id, event["payload"]["call_id"]


def prepare(client: Any, owner: dict[str, str], run_id: str, call_id: str, value: Any) -> dict[str, Any]:
    response = client.post(
        "/v1/workbench/attempts",
        headers=owner,
        json={"attempt_id": str(uuid.uuid4()), "run_id": run_id, "action": "answer", "call_id": call_id, "value": value},
    )
    assert response.status_code == 200, response.text
    return response.json()


def evidence(row: dict[str, Any]) -> dict[str, Any]:
    return {"fingerprint": row["fingerprint"], "revision": row["revision"]}


def test_answer_ledger_binds_original_value_without_storing_it(client: Any, idp: Any) -> None:
    owner, run_id, call_id = scenario(client, idp)
    value = {"id": "PRIVATE-ANSWER-SENTINEL", "extra": 1}
    row = prepare(client, owner, run_id, call_id, value)
    path = f"/v1/workbench/attempts/{row['attempt_id']}"
    assert row["action"] == "answer" and row["call_id"] == call_id and len(row["value_hmac"]) == 64
    assert "PRIVATE-ANSWER-SENTINEL" not in json.dumps(row)

    async def stored() -> str:
        async with db.SessionLocal() as session:
            attempt = await session.get(db.WorkbenchAttempt, row["attempt_id"])
            audit = (await session.execute(select(db.Audit).where(db.Audit.object == f"run:{run_id}"))).scalars().all()
            return json.dumps(
                {
                    "ledger": {column.name: str(getattr(attempt, column.name)) for column in db.WorkbenchAttempt.__table__.columns},
                    "audit": [item.detail for item in audit],
                }
            )

    assert "PRIVATE-ANSWER-SENTINEL" not in client.portal.call(stored)
    assert client.post(path + "/execute", headers=owner, json=evidence(row)).status_code == 422
    assert client.post(path + "/execute", headers=owner, json=evidence(row) | {"value": {"id": "different"}}).status_code == 409
    assert client.get(f"/v1/runs/{run_id}", headers=owner).json()["state"] == "input-required"
    duplicate = {"attempt_id": row["attempt_id"], "run_id": run_id, "action": "answer", "call_id": call_id, "value": {"id": "different"}}
    assert client.post("/v1/workbench/attempts", headers=owner, json=duplicate).status_code == 409
    receipt = client.post(path + "/execute", headers=owner, json=evidence(row) | {"value": {"extra": 1, "id": value["id"]}}).json()
    assert receipt["status"] == "COMMITTED" and receipt["value_hmac"] == row["value_hmac"]
    _wait(client, run_id, owner, "completed")
    assert client.get("/v1/workbench/attempts", headers=owner).json()["active"] == receipt
    assert client.post(path + "/acknowledge", headers=owner, json=evidence(receipt) | {"value": value}).status_code == 422
    assert client.post(path + "/acknowledge", headers=owner, json=evidence(receipt)).json()["acknowledged"] is True
    assert client.post(path + "/execute", headers=owner, json=evidence(row) | {"value": value}).json()["acknowledged"] is True
    events = [event for event in _log(client, run_id, owner) if event["payload"].get("workbench_attempt_id") == row["attempt_id"]]
    assert len(events) == 1 and events[0]["payload"]["workbench_value_hmac"] == row["value_hmac"]
    assert "PRIVATE-ANSWER-SENTINEL" not in json.dumps(events)


def test_answer_registration_schema_actor_and_tenant_fail_closed(client: Any, idp: Any) -> None:
    owner, run_id, call_id = scenario(client, idp)
    body = {"attempt_id": str(uuid.uuid4()), "run_id": run_id, "action": "answer", "call_id": call_id, "value": {"id": "safe"}}
    assert client.post("/v1/workbench/attempts", headers=owner, json=body | {"value": {"wrong": True}}).status_code == 400
    assert client.get("/v1/workbench/attempts", headers=owner).json()["active"] is None
    assert client.post("/v1/workbench/attempts", headers=_user(idp, "not-initiator"), json=body).status_code == 403
    assert client.post("/v1/workbench/attempts", headers=_user(idp, "foreign", tenant="answer-foreign"), json=body).status_code == 404
    assert client.post("/v1/workbench/attempts", headers=owner, json=body | {"call_id": "old-call"}).status_code == 409
    row = prepare(client, owner, run_id, call_id, body["value"])
    assert (
        client.post(
            f"/v1/runs/{run_id}/input", headers=owner, json={"kind": "answer", "call_id": call_id, "value": body["value"]}
        ).status_code
        == 409
    )
    assert client.get(f"/v1/workbench/attempts/{row['attempt_id']}", headers=_user(idp, "not-initiator")).status_code == 404


def test_answer_hmac_is_domain_separated_per_attempt(client: Any, idp: Any) -> None:
    values = []
    for _ in range(2):
        owner, run_id, call_id = scenario(client, idp)
        values.append(prepare(client, owner, run_id, call_id, {"id": "same-low-entropy-value"})["value_hmac"])
    assert values[0] != values[1]


def test_answer_null_json_value_is_distinct_from_missing_value(client: Any, idp: Any) -> None:
    owner, run_id, call_id = scenario(client, idp, schema={"type": "null"})
    row = prepare(client, owner, run_id, call_id, None)
    result = client.post(f"/v1/workbench/attempts/{row['attempt_id']}/execute", headers=owner, json=evidence(row) | {"value": None})
    assert result.status_code == 200 and result.json()["status"] == "COMMITTED"
    _wait(client, run_id, owner, "completed")


def test_answer_keeps_existing_post_tool_redaction(client: Any, idp: Any) -> None:
    owner, run_id, call_id = scenario(client, idp, hooks=True)
    value = {"id": "private-answer@example.test"}
    row = prepare(client, owner, run_id, call_id, value)
    path = f"/v1/workbench/attempts/{row['attempt_id']}"
    receipt = client.post(path + "/execute", headers=owner, json=evidence(row) | {"value": value}).json()
    assert receipt["status"] == "COMMITTED"
    _wait(client, run_id, owner, "completed")
    events = _log(client, run_id, owner)
    assert "private-answer@example.test" not in json.dumps(events)
    assert any(event["type"] == "tool.result" and "[REDACTED:email]" in json.dumps(event["payload"]) for event in events)
    assert client.post(path + "/acknowledge", headers=owner, json=evidence(receipt)).json()["acknowledged"] is True


def test_answer_partial_input_and_receipt_roll_back_together(client: Any, idp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    owner, run_id, call_id = scenario(client, idp)
    row = prepare(client, owner, run_id, call_id, {"id": "safe"})
    original = user_input.apply_input

    async def crash(*args: Any, **kwargs: Any) -> Any:
        await original(*args, **kwargs)
        raise RuntimeError("synthetic answer precommit crash")

    with monkeypatch.context() as patch:
        patch.setattr(user_input, "apply_input", crash)
        with pytest.raises(RuntimeError, match="synthetic answer precommit crash"):
            client.post(
                f"/v1/workbench/attempts/{row['attempt_id']}/execute", headers=owner, json=evidence(row) | {"value": {"id": "safe"}}
            )
    assert client.get(f"/v1/runs/{run_id}", headers=owner).json()["state"] == "input-required"
    assert client.get("/v1/workbench/attempts", headers=owner).json()["active"]["status"] == "PREPARED"
    assert not [event for event in _log(client, run_id, owner) if event["payload"].get("workbench_attempt_id") == row["attempt_id"]]


def test_answer_concurrent_execution_records_one_input(client: Any, idp: Any) -> None:
    owner, run_id, call_id = scenario(client, idp)
    value = {"id": "safe"}
    row = prepare(client, owner, run_id, call_id, value)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda _: client.post(
                    f"/v1/workbench/attempts/{row['attempt_id']}/execute", headers=owner, json=evidence(row) | {"value": value}
                ),
                range(2),
            )
        )
    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    _wait(client, run_id, owner, "completed")
    assert len([event for event in _log(client, run_id, owner) if event["payload"].get("workbench_attempt_id") == row["attempt_id"]]) == 1


def test_answer_changed_request_rejects_without_effect(client: Any, idp: Any) -> None:
    owner, run_id, call_id = scenario(client, idp)
    row = prepare(client, owner, run_id, call_id, {"id": "safe"})

    async def change() -> None:
        async with db.SessionLocal() as session:
            await EventLog(session).append(run_id, RunEventType.INPUT_REQUESTED, {"kind": "answer", "call_id": "new-call", "prompt": "new"})
            await session.commit()

    client.portal.call(change)
    path = f"/v1/workbench/attempts/{row['attempt_id']}"
    receipt = client.post(path + "/execute", headers=owner, json=evidence(row) | {"value": {"id": "safe"}}).json()
    assert receipt["status"] == "REJECTED_NO_EFFECT"
    assert client.post(path + "/acknowledge", headers=owner, json=evidence(receipt)).json()["acknowledged"] is True


def test_answer_conflicting_content_evidence_cannot_unlock(client: Any, idp: Any) -> None:
    owner, run_id, call_id = scenario(client, idp)
    row = prepare(client, owner, run_id, call_id, {"id": "safe"})
    path = f"/v1/workbench/attempts/{row['attempt_id']}"
    receipt = client.post(path + "/execute", headers=owner, json=evidence(row) | {"value": {"id": "safe"}}).json()
    _wait(client, run_id, owner, "completed")

    async def corrupt() -> None:
        async with db.SessionLocal() as session:
            event = await session.get(db.RunEventRow, (run_id, receipt["evidence_seq"]))
            event.payload = event.payload | {"workbench_value_hmac": "0" * 64}
            await session.commit()

    client.portal.call(corrupt)
    assert client.post(path + "/acknowledge", headers=owner, json=evidence(receipt)).status_code == 409


def test_answer_digest_key_change_fails_closed(client: Any, idp: Any, monkeypatch: Any) -> None:
    from dataclasses import replace

    from runtime.loop import toolbroker

    owner, run_id, call_id = scenario(client, idp)
    value = {"id": "original"}
    row = prepare(client, owner, run_id, call_id, value)
    path = f"/v1/workbench/attempts/{row['attempt_id']}"
    with monkeypatch.context() as patch:
        patch.setattr(toolbroker, "settings", replace(toolbroker.settings, hook_digest_key="isolated-test-rotated-key"))
        response = client.post(path + "/execute", headers=owner, json=evidence(row) | {"value": value})
        assert response.status_code == 409
        active = client.get("/v1/workbench/attempts", headers=owner).json()["active"]
        assert active == row
        assert client.post(path + "/acknowledge", headers=owner, json=evidence(row)).status_code == 409
    receipt = client.post(path + "/execute", headers=owner, json=evidence(row) | {"value": value}).json()
    assert receipt["status"] == "COMMITTED"
    _wait(client, run_id, owner, "completed")

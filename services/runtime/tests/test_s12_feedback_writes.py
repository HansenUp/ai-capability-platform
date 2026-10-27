from __future__ import annotations

import dataclasses
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from test_s7_run_api import _user
from test_s7_run_api import idp as idp

from ai_contract import RunState
from observe_adapter import feedback_protocol
from observe_adapter.main import app as observe_app
from runtime.api import feedback_writes
from runtime.state import db
from runtime.state.eventlog import EventLog


@pytest.fixture
def scenario(client: Any, idp: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    subject = "feedback-" + uuid.uuid4().hex
    owner = _user(idp, subject)
    run_id = "fb-" + uuid.uuid4().hex[:20]

    async def create() -> None:
        async with db.SessionLocal() as session:
            session.add(
                db.Run(
                    run_id=run_id,
                    agent_id="demo",
                    agent_version="1",
                    model_alias="m",
                    subject="human:u-" + subject,
                    tenant_id="default",
                    state="working",
                    trace_id="a" * 32,
                    durable=False,
                )
            )
            await session.flush()
            await EventLog(session).transition(run_id, RunState.COMPLETED, "isolated feedback fixture")
            await session.commit()

    client.portal.call(create)
    token = uuid.uuid4().hex + uuid.uuid4().hex
    for key, value in {
        "FEEDBACK_LOCAL_PROTOCOL": "1",
        "EGRESS_ALLOWLIST": os.environ.get("EGRESS_ALLOWLIST", "") + ",http://127.0.0.1:8765",
        "FEEDBACK_LOCAL_OBSERVE_URL": "http://127.0.0.1:8765",
        "FEEDBACK_LOCAL_SERVICE_TOKEN": token,
        "OBSERVE_ENV": "dev",
        "LANGFUSE_HOST": "http://127.0.0.1:9876",
        "LANGFUSE_PUBLIC_KEY": uuid.uuid4().hex,
        "LANGFUSE_SECRET_KEY": uuid.uuid4().hex,
    }.items():
        monkeypatch.setenv(key, value)
    state: dict[str, Any] = {"scores": {}, "calls": [], "missing": False, "drop": False, "tamper": None}

    def downstream(request: httpx.Request) -> httpx.Response:
        state["calls"].append(request)
        assert request.headers["authorization"].startswith("Basic ")
        assert token not in request.headers["authorization"]
        if request.method == "POST":
            payload = json.loads(request.content)
            state["scores"][payload["id"]] = payload
            if state["drop"]:
                raise httpx.ReadError("isolated response lost")
            return httpx.Response(200, json={"id": payload["id"]})
        assert request.method == "GET"
        if state["missing"]:
            return httpx.Response(404)
        if "/traces/" in request.url.path:
            trace = {
                "id": "a" * 32,
                "sessionId": run_id,
                "metadata": {
                    "attributes": {
                        "ai.tenant_id": "default",
                        "ai.agent_id": state.get("agent_id", "demo"),
                        "ai.run_id": run_id,
                        "ai.subject": "human:u-" + subject,
                    }
                },
            }
            if state.get("bad_trace"):
                trace["metadata"]["attributes"]["ai.tenant_id"] = "foreign"
            return httpx.Response(200, json=trace)
        payload = deepcopy(state["scores"].get(request.url.path.rsplit("/", 1)[-1], {}))
        if state["tamper"]:
            state["tamper"](payload)
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(feedback_protocol, "_transport", httpx.MockTransport(downstream))
    monkeypatch.setattr(feedback_writes, "_transport", httpx.ASGITransport(app=observe_app))
    return client, idp, owner, run_id, state


def prepare(scenario: Any, score: int = 1, expected: int = 0, attempt: str | None = None) -> dict[str, Any]:
    client, _, owner, run_id, _ = scenario
    response = client.post(
        "/v1/workbench/feedback-attempts",
        headers=owner,
        json={"attempt_id": attempt or str(uuid.uuid4()), "run_id": run_id, "score": score, "expected_revision": expected},
    )
    assert response.status_code == 200, response.text
    return response.json()


def call(scenario: Any, row: dict[str, Any], action: str, headers: Any = None) -> Any:
    client, _, owner, _, _ = scenario
    return client.post(
        f"/v1/workbench/feedback-attempts/{row['attempt_id']}/{action}",
        headers=headers or owner,
        json={"fingerprint": row["fingerprint"], "revision": row["revision"]},
    )


def test_feedback_single_delivery_strict_observation_ack_and_neutral_revision(scenario: Any) -> None:
    client, _, owner, run_id, state = scenario
    row = prepare(scenario)
    assert row["status"] == "PREPARED" and row["score_revision"] == 1 and state["calls"] == []
    assert call(scenario, row, "acknowledge").status_code == 409
    sent = call(scenario, row, "execute").json()
    assert sent["status"] == "PENDING_OBSERVATION"
    assert call(scenario, sent, "execute").json() == sent
    assert len(state["calls"]) == 1
    assert client.post(f"/v1/runs/{run_id}/feedback", headers=owner, json={"score": 1}).status_code == 409
    assert client.get("/v1/workbench/attempts", headers=owner).json()["active"]["attempt_id"] == row["attempt_id"]
    observed = call(scenario, sent, "reconcile").json()
    assert observed["status"] == "OBSERVED"
    assert client.get("/v1/workbench/attempts", headers=owner).json()["active"] is not None
    assert call(scenario, observed, "acknowledge").json()["acknowledged"] is True
    assert client.get("/v1/workbench/attempts", headers=owner).json()["active"] is None
    neutral = prepare(scenario, 0, 1)
    assert neutral["score_revision"] == 2
    assert len([request for request in state["calls"] if request.method == "POST"]) == 1


def test_feedback_response_loss_and_missing_read_never_resend_or_unlock(scenario: Any) -> None:
    _, _, _, _, state = scenario
    state["drop"] = True
    sent = call(scenario, prepare(scenario), "execute").json()
    state["missing"] = True
    assert call(scenario, sent, "reconcile").json() == sent
    assert call(scenario, sent, "acknowledge").status_code == 409
    assert call(scenario, sent, "execute").json() == sent
    assert len([request for request in state["calls"] if request.method == "POST"]) == 1
    state["missing"] = False
    assert call(scenario, sent, "reconcile").json()["status"] == "OBSERVED"


@pytest.mark.parametrize(
    "field", ["tenant_id", "subject", "run_id", "score_revision", "completion_seq", "agent_version", "attempt_id", "scope_id"]
)
def test_feedback_wrong_observation_never_unlocks(scenario: Any, field: str) -> None:
    _, _, _, _, state = scenario
    sent = call(scenario, prepare(scenario), "execute").json()
    state["tamper"] = lambda payload: payload["metadata"].update({field: "wrong"})
    assert call(scenario, sent, "reconcile").json() == sent
    assert call(scenario, sent, "acknowledge").status_code == 409


def test_feedback_duplicate_binding_identity_and_concurrent_execute(scenario: Any) -> None:
    client, idp, owner, run_id, state = scenario
    row = prepare(scenario)
    assert prepare(scenario, attempt=row["attempt_id"]) == row
    for score, expected in [(0, 0), (1, 1)]:
        response = client.post(
            "/v1/workbench/feedback-attempts",
            headers=owner,
            json={"attempt_id": row["attempt_id"], "run_id": run_id, "score": score, "expected_revision": expected},
        )
        assert response.status_code == 409
    other = _user(idp, "other-feedback")
    foreign = _user(idp, "other-feedback", tenant="foreign")
    for headers in (other, foreign):
        assert client.get(f"/v1/workbench/feedback-attempts/{row['attempt_id']}", headers=headers).status_code == 404
        assert call(scenario, row, "execute", headers).status_code == 404
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: call(scenario, row, "execute"), range(2)))
    assert sorted(reply.status_code for reply in replies) == [200, 409]
    assert len([request for request in state["calls"] if request.method == "POST"]) == 1


@pytest.mark.parametrize("score", [True, 0.5, "1", 2])
def test_feedback_rejects_non_numeric_contract(scenario: Any, score: Any) -> None:
    client, _, owner, run_id, state = scenario
    response = client.post(
        "/v1/workbench/feedback-attempts",
        headers=owner,
        json={"attempt_id": str(uuid.uuid4()), "run_id": run_id, "score": score, "expected_revision": 0},
    )
    assert response.status_code == 422 and state["calls"] == []


def test_feedback_profile_and_independent_service_auth(scenario: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, owner, _, state = scenario
    with TestClient(observe_app) as observe:
        assert observe.get("/internal/local-feedback/fb_" + "a" * 32).status_code == 401
        assert observe.get("/internal/local-feedback/fb_" + "a" * 32, headers={"Authorization": "Bearer wrong"}).status_code == 401
    monkeypatch.setenv("FEEDBACK_LOCAL_PROTOCOL", "0")
    assert client.get("/v1/workbench/feedback-attempts/" + str(uuid.uuid4()), headers=owner).status_code == 503
    assert state["calls"] == []


def test_feedback_trace_conflict_and_unproven_bool_never_observed(scenario: Any) -> None:
    _, _, _, _, state = scenario
    sent = call(scenario, prepare(scenario), "execute").json()
    state["bad_trace"] = True
    assert call(scenario, sent, "reconcile").json() == sent
    state["bad_trace"] = False
    state["tamper"] = lambda payload: payload.update(value=True)
    assert call(scenario, sent, "reconcile").json() == sent
    state["tamper"] = None
    assert call(scenario, sent, "reconcile").json()["status"] == "OBSERVED"


def test_feedback_subject_lock_stale_revision_and_identity_guards(scenario: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client, idp, owner, run_id, state = scenario
    body = {"attempt_id": str(uuid.uuid4()), "run_id": run_id, "score": 1, "expected_revision": 0}
    assert client.post("/v1/workbench/feedback-attempts", json=body).status_code == 401
    assert (
        client.post("/v1/workbench/feedback-attempts", headers={"X-Tenant-ID": "default", "X-User-ID": "fake"}, json=body).status_code
        == 401
    )
    assert client.post("/v1/workbench/feedback-attempts", headers=_user(idp, "not-owner"), json=body).status_code == 403
    assert client.post("/v1/workbench/feedback-attempts", headers=_user(idp, "not-owner", tenant="foreign"), json=body).status_code == 404
    assert client.post("/v1/workbench/feedback-attempts", headers=owner, json=body | {"comment": "forbidden"}).status_code == 422
    row = prepare(scenario)
    assert client.post("/v1/workbench/feedback-attempts", headers=owner, json=body).status_code == 409
    assert client.post("/v1/workbench/attempts", headers=owner, json={"attempt_id": str(uuid.uuid4()), "run_id": run_id}).status_code == 409
    pending = call(scenario, row, "execute").json()
    observed = call(scenario, pending, "reconcile").json()
    assert call(scenario, observed, "acknowledge").status_code == 200
    assert client.post("/v1/workbench/feedback-attempts", headers=owner, json=body).status_code == 409
    monkeypatch.setattr(feedback_writes, "settings", dataclasses.replace(feedback_writes.settings, runtime_env="prod"))
    assert client.get(f"/v1/workbench/feedback-attempts/{row['attempt_id']}", headers=owner).status_code == 503
    assert len([request for request in state["calls"] if request.method == "POST"]) == 1


def test_feedback_prepare_transaction_rolls_back_outbox_and_lock(scenario: Any) -> None:
    from sqlalchemy import event

    client, _, owner, run_id, state = scenario
    attempt_id = str(uuid.uuid4())

    def fail(*args: Any) -> None:
        raise RuntimeError("isolated outbox insert failure")

    event.listen(db.FeedbackOutbox, "before_insert", fail)
    try:
        with pytest.raises(RuntimeError, match="isolated outbox"):
            client.post(
                "/v1/workbench/feedback-attempts",
                headers=owner,
                json={"attempt_id": attempt_id, "run_id": run_id, "score": -1, "expected_revision": 0},
            )
    finally:
        event.remove(db.FeedbackOutbox, "before_insert", fail)
    assert client.get("/v1/workbench/attempts", headers=owner).json()["active"] is None
    assert client.get("/v1/workbench/feedback-attempts/" + attempt_id, headers=owner).status_code == 404
    assert state["calls"] == []
    assert prepare(scenario, attempt=attempt_id)["score_revision"] == 1


def test_feedback_corrupt_receipt_never_acknowledges(scenario: Any) -> None:
    client, _, owner, _, _ = scenario
    pending = call(scenario, prepare(scenario), "execute").json()
    observed = call(scenario, pending, "reconcile").json()

    async def corrupt() -> None:
        async with db.SessionLocal() as session:
            row = await session.get(db.FeedbackOutbox, observed["attempt_id"])
            assert row is not None
            row.observed_hash = "0" * 64
            await session.commit()

    client.portal.call(corrupt)
    assert call(scenario, observed, "acknowledge").status_code == 409
    assert client.get("/v1/workbench/attempts", headers=owner).json()["active"] is not None


def test_feedback_head_read_is_owner_scoped_and_does_not_unlock(scenario: Any) -> None:
    client, idp, owner, run_id, state = scenario
    path = f"/v1/workbench/feedback-attempts/runs/{run_id}/head"
    assert client.get(path, headers=owner).json()["score_revision"] == 0
    prepared = prepare(scenario, 0)
    assert prepared["score"] == 0
    assert client.get(path, headers=owner).json()["score_revision"] == 1
    assert client.get(path, headers=_user(idp, "not-owner")).status_code == 403
    assert client.get(path, headers=_user(idp, "not-owner", tenant="foreign")).status_code == 404
    assert client.get("/v1/workbench/attempts", headers=owner).json()["active"]["attempt_id"] == prepared["attempt_id"]
    assert state["calls"] == []


def test_admin_readonly_feedback_evidence_is_bounded_and_has_no_payload(scenario: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime, timedelta

    from test_s7_run_api import _admin

    client, idp, owner, run_id, state = scenario

    async def isolate_agent() -> None:
        async with db.SessionLocal() as session:
            run = await session.get(db.Run, run_id)
            run.agent_id = "metrics-" + run_id
            await session.commit()

    client.portal.call(isolate_agent)
    state["agent_id"] = "metrics-" + run_id
    row = prepare(scenario)
    params = {
        "agent_id": "metrics-" + run_id,
        "since": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "until": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }
    path = "/v1/metrics/feedback-evidence"
    monkeypatch.setenv("FEEDBACK_LOCAL_EVIDENCE_READ", "1")
    assert client.get(path, params=params, headers=owner).status_code == 403
    reply = client.get(path, params=params, headers=_admin(idp))
    assert reply.status_code == 200, reply.text
    proof = next(item for item in reply.json()["proofs"] if item["run_id"] == run_id)
    assert proof["score_revision"] == 1 and proof["observed"] is False
    assert proof["payload_hash"] == row["fingerprint"] and "payload" not in proof
    assert state["calls"] == []
    observed = call(scenario, call(scenario, row, "execute").json(), "reconcile").json()
    before = len(state["calls"])
    proof = next(item for item in client.get(path, params=params, headers=_admin(idp)).json()["proofs"] if item["run_id"] == run_id)
    assert proof["observed"] is True and len(state["calls"]) == before
    assert client.get("/v1/workbench/attempts", headers=owner).json()["active"]["attempt_id"] == observed["attempt_id"]


def test_readonly_evidence_cannot_enable_delivery_or_release_lock(scenario: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime, timedelta

    from test_s7_run_api import _admin

    client, idp, owner, run_id, state = scenario

    async def isolate() -> None:
        async with db.SessionLocal() as session:
            run = await session.get(db.Run, run_id)
            run.agent_id = "read-only-" + run_id
            await session.commit()

    client.portal.call(isolate)
    row = prepare(scenario)
    monkeypatch.setenv("FEEDBACK_LOCAL_EVIDENCE_READ", "1")
    monkeypatch.delenv("FEEDBACK_LOCAL_PROTOCOL")
    params = {
        "agent_id": "read-only-" + run_id,
        "since": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "until": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }
    before = client.get("/v1/workbench/attempts", headers=owner).json()
    for _ in range(2):
        reply = client.get("/v1/metrics/feedback-evidence", params=params, headers=_admin(idp))
        assert reply.status_code == 200, reply.text
        assert reply.json()["proofs"] == [
            {
                "run_id": run_id,
                "score_id": "fb_" + row["attempt_id"].replace("-", ""),
                "score_revision": 1,
                "payload_hash": row["fingerprint"],
                "observed": False,
                "trace_id": "a" * 32,
            }
        ]
    for action in ("execute", "reconcile", "acknowledge"):
        assert call(scenario, row, action).status_code == 503
    assert (
        client.post(
            "/v1/workbench/feedback-attempts",
            headers=owner,
            json={"attempt_id": str(uuid.uuid4()), "run_id": run_id, "score": 0, "expected_revision": 1},
        ).status_code
        == 503
    )
    assert client.get(f"/v1/workbench/feedback-attempts/{row['attempt_id']}", headers=owner).status_code == 503
    assert client.get(f"/v1/workbench/feedback-attempts/runs/{run_id}/head", headers=owner).status_code == 503
    with TestClient(observe_app) as observe:
        assert (
            observe.get(
                "/internal/local-feedback/fb_" + "a" * 32, headers={"Authorization": "Bearer " + os.environ["FEEDBACK_LOCAL_SERVICE_TOKEN"]}
            ).status_code
            == 503
        )
        assert (
            observe.post(
                "/internal/local-feedback",
                headers={"Authorization": "Bearer " + os.environ["FEEDBACK_LOCAL_SERVICE_TOKEN"]},
                json={
                    "id": "fb_" + "a" * 32,
                    "name": "user_feedback",
                    "dataType": "NUMERIC",
                    "traceId": "a" * 32,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "value": 0,
                    "metadata": {},
                },
            ).status_code
            == 503
        )
    assert client.get("/v1/workbench/attempts", headers=owner).json() == before
    assert state["calls"] == []

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import delete
from test_s7_run_api import _admin, _user
from test_s7_run_api import idp as idp

from runtime.api import auth, feedback_metrics
from runtime.state import db

START = datetime(2026, 10, 18, tzinfo=UTC)
PARAMS = {"agent_id": "feedback-cohort", "since": START.isoformat(), "until": (START + timedelta(days=1)).isoformat()}


@pytest.fixture
def cohort_rows(client: Any) -> Iterator[list[str]]:
    ids = []

    async def insert() -> None:
        async with db.SessionLocal() as session:
            for state, offset, tenant, agent in [
                ("completed", 0, "default", "feedback-cohort"),
                ("failed", 1, "default", "feedback-cohort"),
                ("canceled", 2, "default", "feedback-cohort"),
                ("input-required", 3, "default", "feedback-cohort"),
                ("completed", 24, "default", "feedback-cohort"),
                ("completed", 1, "foreign-feedback", "feedback-cohort"),
                ("completed", 1, "default", "other-feedback"),
            ]:
                run_id = "feedback-" + uuid4().hex[:16]
                ids.append(run_id)
                created = START + timedelta(hours=offset)
                session.add(
                    db.Run(
                        run_id=run_id,
                        tenant_id=tenant,
                        agent_id=agent,
                        agent_version="1",
                        model_alias="default-chat",
                        subject="human:original",
                        state=state,
                        created_at=created,
                        finished_at=created + timedelta(minutes=1) if state == "completed" else None,
                    )
                )
            await session.commit()

    client.portal.call(insert)
    yield ids

    async def remove() -> None:
        async with db.SessionLocal() as session:
            await session.execute(delete(db.RunEventRow).where(db.RunEventRow.run_id.in_(ids)))
            await session.execute(delete(db.Run).where(db.Run.run_id.in_(ids)))
            await session.commit()

    client.portal.call(remove)


def test_cohort_verified_tenant_half_open_and_state_counts(client: Any, idp: Any, cohort_rows: list[str]) -> None:
    response = client.get("/v1/metrics/feedback-cohort", params=PARAMS, headers=_admin(idp))
    assert response.status_code == 200, response.text
    body = response.json()
    assert {row["run_id"] for row in body["runs"]} == set(cohort_rows[:4])
    assert body["counts"] == {"completed": 1, "failed": 1, "canceled": 1, "pending": 1}
    assert body["tenant_id"] == "default" and body["auth_method"] == "user" and body["subject"] == "human:u-root"
    assert body["snapshot_guaranteed"] is False and body["complete_scan"] is True
    assert response.headers["cache-control"] == "no-store"
    foreign = _user(idp, "metric-foreign", tenant="foreign-feedback", realm_access={"roles": ["platform-admin"]})
    assert len(client.get("/v1/metrics/feedback-cohort", params=PARAMS, headers=foreign).json()["runs"]) == 1


def test_cohort_authorization_and_production_guard(client: Any, idp: Any, monkeypatch: Any) -> None:
    path = "/v1/metrics/feedback-cohort"
    assert client.get(path, params=PARAMS).status_code == 401
    assert client.get(path, params=PARAMS, headers={"X-AI-Subject": "human:root", "X-AI-Roles": "platform-admin"}).status_code == 401
    assert client.get(path, params=PARAMS, headers=_user(idp, "not-admin")).status_code == 403
    service = _user(
        idp,
        "svc",
        azp="console-api",
        client_id="console-api",
        preferred_username="service-account-console-api",
        realm_access={"roles": ["platform-admin"]},
    )
    assert client.get(path, params=PARAMS, headers=service).status_code == 403
    with monkeypatch.context() as patch:
        patch.setattr(auth, "settings", replace(auth.settings, auth_required=False))
        assert client.get(path, params=PARAMS, headers=_admin(idp)).status_code == 403
    with monkeypatch.context() as patch:
        patch.setattr(feedback_metrics, "settings", replace(feedback_metrics.settings, runtime_env="prod"))
        assert client.get(path, params=PARAMS, headers=_admin(idp)).status_code == 503


@pytest.mark.parametrize("updates", [{"since": "2026-10-18"}, {"until": "2026-10-18T00:00:00Z"}, {"until": "2026-10-26T00:00:00Z"}])
def test_cohort_invalid_time_range(client: Any, idp: Any, updates: dict[str, str]) -> None:
    assert client.get("/v1/metrics/feedback-cohort", params=PARAMS | updates, headers=_admin(idp)).status_code == 422


def test_cohort_bounded_scan_and_invalid_completion(client: Any, idp: Any, cohort_rows: list[str], monkeypatch: Any) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(feedback_metrics, "MAX_RUNS", 3)
        assert client.get("/v1/metrics/feedback-cohort", params=PARAMS, headers=_admin(idp)).status_code == 503

    async def corrupt() -> None:
        async with db.SessionLocal() as session:
            run = await session.get(db.Run, cohort_rows[0])
            run.finished_at = None
            await session.commit()

    client.portal.call(corrupt)
    assert client.get("/v1/metrics/feedback-cohort", params=PARAMS, headers=_admin(idp)).status_code == 503


def test_cohort_reads_terminal_event_when_finished_column_is_unset(client: Any, idp: Any, cohort_rows: list[str]) -> None:
    from sqlalchemy import event

    finished = START + timedelta(minutes=2)

    async def recorded() -> None:
        async with db.SessionLocal() as session:
            run = await session.get(db.Run, cohort_rows[0])
            run.finished_at = None
            run.last_seq = 1
            session.add(
                db.RunEventRow(
                    run_id=run.run_id, seq=1, type="run.state_changed", payload={"from": "working", "to": "completed"}, ts=finished
                )
            )
            await session.commit()

    client.portal.call(recorded)
    statements = []

    def count_query(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: Any) -> None:
        if statement.lstrip().upper().startswith("SELECT") and "FROM t_run" in statement:
            statements.append(statement)

    event.listen(db.engine.sync_engine, "before_cursor_execute", count_query)
    try:
        response = client.get("/v1/metrics/feedback-cohort", params=PARAMS, headers=_admin(idp))
    finally:
        event.remove(db.engine.sync_engine, "before_cursor_execute", count_query)
    assert response.status_code == 200, response.text
    row = next(row for row in response.json()["runs"] if row["run_id"] == cohort_rows[0])
    assert row["finished_at"] == finished.isoformat() and row["finished_at_source"] == "terminal_event"
    assert len(statements) == 1

    async def conflict() -> None:
        async with db.SessionLocal() as session:
            run = await session.get(db.Run, cohort_rows[0])
            run.finished_at = finished + timedelta(seconds=1)
            await session.commit()

    client.portal.call(conflict)
    assert client.get("/v1/metrics/feedback-cohort", params=PARAMS, headers=_admin(idp)).status_code == 503


@pytest.mark.parametrize("read_enabled,write_enabled", [(False, False), (False, True), (True, False), (True, True)])
def test_evidence_independent_switches(
    client: Any, idp: Any, cohort_rows: list[str], monkeypatch: Any, read_enabled: bool, write_enabled: bool
) -> None:
    monkeypatch.setenv("FEEDBACK_LOCAL_EVIDENCE_READ", "1" if read_enabled else "0")
    monkeypatch.setenv("FEEDBACK_LOCAL_PROTOCOL", "1" if write_enabled else "0")
    reply = client.get("/v1/metrics/feedback-evidence", params=PARAMS, headers=_admin(idp))
    assert reply.status_code == (200 if read_enabled else 503), reply.text
    if read_enabled:
        assert reply.json()["proofs"] == []
        assert reply.json()["counts"] == {"completed": 1, "failed": 1, "canceled": 1, "pending": 1}
        assert {row["run_id"] for row in reply.json()["runs"]} == set(cohort_rows[:4])
    assert client.get("/v1/metrics/feedback-cohort", params=PARAMS, headers=_admin(idp)).status_code == 200


def test_evidence_read_guards_and_scope(client: Any, idp: Any, cohort_rows: list[str], monkeypatch: Any) -> None:
    path = "/v1/metrics/feedback-evidence"
    monkeypatch.setenv("FEEDBACK_LOCAL_EVIDENCE_READ", "1")
    monkeypatch.delenv("FEEDBACK_LOCAL_PROTOCOL", raising=False)
    assert client.get(path, params=PARAMS).status_code == 401
    assert client.get(path, params=PARAMS, headers={"X-AI-Subject": "human:root", "X-AI-Roles": "platform-admin"}).status_code == 401
    assert client.get(path, params=PARAMS, headers=_user(idp, "reader-not-admin")).status_code == 403
    service = _user(
        idp,
        "reader-svc",
        azp="console-api",
        client_id="console-api",
        preferred_username="service-account-console-api",
        realm_access={"roles": ["platform-admin"]},
    )
    assert client.get(path, params=PARAMS, headers=service).status_code == 403
    foreign = _user(idp, "reader-foreign", tenant="foreign-feedback", realm_access={"roles": ["platform-admin"]})
    reply = client.get(path, params=PARAMS, headers=foreign)
    assert reply.status_code == 200 and {row["run_id"] for row in reply.json()["runs"]} == {cohort_rows[5]}
    assert reply.json()["proofs"] == []
    for updates in [{"since": "2026-10-18"}, {"until": "2026-10-18T00:00:00Z"}, {"until": "2026-10-26T00:00:00Z"}]:
        assert client.get(path, params=PARAMS | updates, headers=_admin(idp)).status_code == 422
    with monkeypatch.context() as patch:
        patch.setattr(feedback_metrics, "MAX_RUNS", 3)
        assert client.get(path, params=PARAMS, headers=_admin(idp)).status_code == 503
    with monkeypatch.context() as patch:
        patch.setattr(auth, "settings", replace(auth.settings, auth_required=False))
        assert client.get(path, params=PARAMS, headers=_admin(idp)).status_code == 403
    for environment in ("prod", "staging"):
        with monkeypatch.context() as patch:
            patch.setattr(feedback_metrics, "settings", replace(feedback_metrics.settings, runtime_env=environment))
            assert client.get(path, params=PARAMS, headers=_admin(idp)).status_code == 503
    for value in ("true", "yes", ""):
        monkeypatch.setenv("FEEDBACK_LOCAL_EVIDENCE_READ", value)
        assert client.get(path, params=PARAMS, headers=_admin(idp)).status_code == 503
    monkeypatch.delenv("FEEDBACK_LOCAL_EVIDENCE_READ")
    assert client.get(path, params=PARAMS, headers=_admin(idp)).status_code == 503

"""本机反馈审计的只读 Run cohort；不信任开发头或服务身份。"""

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select

from ai_contract import RunEventType, RunState
from runtime.api import auth
from runtime.api.auth import Principal, current_principal
from runtime.api.workbench import binding, user_scope
from runtime.config import settings
from runtime.state.db import Run, RunEventRow, SessionLocal

router = APIRouter(prefix="/v1/metrics", tags=["Local feedback audit"])
MAX_RUNS = 2000


@router.get("/feedback-cohort")
async def feedback_cohort(
    response: Response,
    since: datetime,
    until: datetime,
    agent_id: str = Query(min_length=1, max_length=64),
    principal: Principal = Depends(current_principal),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    if settings.runtime_env == "prod":
        raise HTTPException(503, "feedback cohort is local only")
    user_scope(principal)
    if not auth.is_admin(principal):
        raise HTTPException(403, "feedback cohort requires platform-admin")
    if since.tzinfo is None or until.tzinfo is None or not timedelta(0) < until - since <= timedelta(days=7):
        raise HTTPException(422, "aware increasing range of at most seven days required")
    try:
        since, until = since.astimezone(UTC), until.astimezone(UTC)
    except (ValueError, OverflowError):
        raise HTTPException(422, "time range cannot be represented") from None
    started = datetime.now(UTC)
    terminal_filter = (
        RunEventRow.run_id == Run.run_id,
        RunEventRow.seq <= Run.last_seq,
        RunEventRow.type == RunEventType.RUN_STATE_CHANGED.value,
        RunEventRow.payload["to"].as_string() == RunState.COMPLETED.value,
    )
    terminal_time = select(RunEventRow.ts).where(*terminal_filter).order_by(RunEventRow.seq.desc()).limit(1).scalar_subquery()
    terminal_count = select(func.count()).select_from(RunEventRow).where(*terminal_filter).scalar_subquery()
    async with SessionLocal() as session:
        rows = list(
            (
                await session.execute(
                    select(Run, terminal_time, terminal_count)
                    .where(
                        Run.tenant_id == principal.tenant_id,
                        Run.agent_id == agent_id,
                        Run.created_at >= since,
                        Run.created_at < until,
                    )
                    .order_by(Run.created_at, Run.run_id)
                    .limit(MAX_RUNS + 1)
                )
            ).all()
        )
    if len(rows) > MAX_RUNS:
        raise HTTPException(503, "feedback cohort exceeds bounded scan")
    counts = {name: 0 for name in ("completed", "failed", "canceled", "pending")}
    facts = []
    for run, event_time, event_count in rows:
        try:
            state = RunState(run.state)
        except ValueError:
            raise HTTPException(503, "invalid cohort state") from None
        finished = run.finished_at if run.finished_at is not None else event_time
        if state == RunState.COMPLETED and (
            finished is None
            or finished < run.created_at
            or event_count > 1
            or (run.finished_at is not None and event_time is not None and run.finished_at != event_time)
        ):
            raise HTTPException(503, "completed run has invalid completion time")
        counts[state.value if state.terminal else "pending"] += 1
        facts.append(
            {
                "run_id": run.run_id,
                "tenant_id": run.tenant_id,
                "agent_id": run.agent_id,
                "subject": run.subject,
                "state": state.value,
                "created_at": run.created_at.isoformat(),
                "finished_at": finished.isoformat() if finished else None,
                "finished_at_source": "run_row" if run.finished_at is not None else "terminal_event" if event_time is not None else None,
            }
        )
    return {
        "scope": "runtime_feedback_cohort_v1",
        "tenant_id": principal.tenant_id,
        "agent_id": agent_id,
        "subject": principal.subject,
        "auth_method": "user",
        "since": since.isoformat(),
        "until": until.isoformat(),
        "read_started_at": started.isoformat(),
        "read_finished_at": datetime.now(UTC).isoformat(),
        "complete_scan": True,
        "snapshot_guaranteed": False,
        "counts": counts,
        "runs": facts,
    }


@router.get("/feedback-evidence")
async def feedback_evidence(
    response: Response,
    since: datetime,
    until: datetime,
    agent_id: str = Query(min_length=1, max_length=64),
    principal: Principal = Depends(current_principal),
) -> dict[str, Any]:
    from runtime.api.feedback_writes import digest, load
    from runtime.state.db import FeedbackOutbox, WorkbenchAttempt

    user_scope(principal)
    if not auth.is_admin(principal):
        raise HTTPException(403, "feedback evidence requires platform-admin")
    if settings.runtime_env != "dev" or os.environ.get("FEEDBACK_LOCAL_EVIDENCE_READ") != "1":
        raise HTTPException(503, "local feedback evidence reading disabled")
    body = await feedback_cohort(response, since, until, agent_id, principal)
    completed = {row["run_id"]: row for row in body["runs"] if row["state"] == "completed"}
    proofs = []
    async with SessionLocal() as session:
        rows = list(
            (
                await session.execute(
                    select(FeedbackOutbox, WorkbenchAttempt)
                    .join(WorkbenchAttempt, WorkbenchAttempt.attempt_id == FeedbackOutbox.attempt_id, isouter=True)
                    .join(Run, Run.run_id == FeedbackOutbox.run_id)
                    .where(Run.tenant_id == principal.tenant_id, Run.agent_id == agent_id, Run.created_at >= since, Run.created_at < until)
                    .order_by(FeedbackOutbox.run_id, FeedbackOutbox.score_revision)
                    .limit(MAX_RUNS + 1)
                )
            ).all()
        )
        if len(rows) > MAX_RUNS:
            raise HTTPException(503, "feedback evidence exceeds bounded scan")
        accepted = await session.scalar(
            select(func.count())
            .select_from(WorkbenchAttempt)
            .join(Run, Run.run_id == WorkbenchAttempt.run_id)
            .where(
                WorkbenchAttempt.action == "feedback",
                Run.tenant_id == principal.tenant_id,
                Run.agent_id == agent_id,
                Run.created_at >= since,
                Run.created_at < until,
            )
        )
        if accepted != len(rows):
            raise HTTPException(503, "feedback acceptance evidence incomplete")
        for outbox, attempt in rows:
            if attempt is None or outbox.run_id not in completed:
                raise HTTPException(503, "feedback evidence incomplete")
            try:
                await load(session, outbox.attempt_id, attempt.scope_id)
                run = await session.get(Run, outbox.run_id)
                payload = outbox.payload
                metadata = payload["metadata"]
                event = await session.get(RunEventRow, (outbox.run_id, attempt.request_seq))
                if (
                    event is None
                    or event.type != "run.state_changed"
                    or event.payload.get("to") != "completed"
                    or run is None
                    or event.seq > run.last_seq
                    or datetime.fromisoformat(payload["timestamp"]) < event.ts
                    or payload["name"] != "user_feedback"
                    or payload["dataType"] != "NUMERIC"
                    or type(payload["value"]) is not int
                    or payload["value"] not in (-1, 0, 1)
                    or payload["traceId"] != run.trace_id
                    or metadata["tenant_id"] != principal.tenant_id
                    or metadata["subject"] != run.subject
                    or metadata["agent_id"] != run.agent_id
                    or metadata["agent_version"] != run.agent_version
                    or metadata["protocol"] != "runtime-feedback-v1"
                    or attempt.scope_id != binding(Principal(tenant_id=run.tenant_id, subject=run.subject, auth_method="user"))["scope_id"]
                    or attempt.status not in {"PREPARED", "PENDING_OBSERVATION", "OBSERVED"}
                    or (attempt.status == "OBSERVED" and outbox.observed_hash != digest(payload))
                ):
                    raise ValueError
            except (HTTPException, KeyError, TypeError, ValueError):
                raise HTTPException(503, "feedback evidence invalid") from None
            proofs.append(
                {
                    "run_id": outbox.run_id,
                    "score_id": payload["id"],
                    "score_revision": outbox.score_revision,
                    "payload_hash": outbox.payload_hash,
                    "observed": attempt.status == "OBSERVED",
                    "trace_id": payload["traceId"],
                }
            )
    body["scope"] = "runtime_feedback_evidence_v2"
    body["proofs"] = proofs
    body["read_finished_at"] = datetime.now(UTC).isoformat()
    return body

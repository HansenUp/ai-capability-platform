"""本机反馈写前登记、单次显式交付、只读对账与原操作者收口。"""

import hashlib
import json
import os
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import func, select

from runtime import net
from runtime.api import workbench
from runtime.api.auth import Principal, current_principal
from runtime.config import settings
from runtime.state.db import Audit, FeedbackOutbox, Run, RunEventRow, SessionLocal, WorkbenchAttempt

router = APIRouter(prefix="/v1/workbench/feedback-attempts", tags=["Local feedback protocol"])
_transport: httpx.AsyncBaseTransport | None = None


def enabled() -> None:
    if settings.runtime_env != "dev" or os.environ.get("FEEDBACK_LOCAL_PROTOCOL") != "1":
        raise HTTPException(503, "local feedback protocol disabled")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def service() -> tuple[str, str]:
    target = os.environ.get("FEEDBACK_LOCAL_OBSERVE_URL", "")
    token = os.environ.get("FEEDBACK_LOCAL_SERVICE_TOKEN", "")
    parsed = urlsplit(target)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or len(token) < 32
    ):
        raise HTTPException(503, "isolated feedback service configuration required")
    try:
        if parsed.port is None:
            raise ValueError
    except ValueError:
        raise HTTPException(503, "invalid local feedback port") from None
    return target.rstrip("/"), token


class Prepare(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: UUID
    run_id: str = Field(min_length=1, max_length=40)
    expected_revision: StrictInt = Field(ge=0, le=2**31 - 2)
    score: StrictInt = Field(ge=-1, le=1)


async def load(session: Any, attempt_id: str, scope_id: str) -> tuple[WorkbenchAttempt, FeedbackOutbox]:
    row = await workbench.owned(session, attempt_id, scope_id)
    outbox = await session.get(FeedbackOutbox, attempt_id)
    if (
        row.action != "feedback"
        or outbox is None
        or outbox.run_id != row.run_id
        or digest(outbox.payload) != outbox.payload_hash
        or row.fingerprint != outbox.payload_hash
        or outbox.payload.get("metadata", {}).get("scope_id") != scope_id
        or outbox.payload.get("metadata", {}).get("attempt_id") != attempt_id
        or outbox.payload.get("metadata", {}).get("run_id") != row.run_id
        or outbox.payload.get("metadata", {}).get("completion_seq") != row.request_seq
        or outbox.payload.get("metadata", {}).get("score_revision") != outbox.score_revision
    ):
        raise HTTPException(409, "feedback evidence unavailable")
    return row, outbox


def view(row: WorkbenchAttempt, outbox: FeedbackOutbox) -> dict[str, Any]:
    return workbench.view(row) | {
        "score_revision": outbox.score_revision,
        "payload_hash": outbox.payload_hash,
        "score": outbox.payload["value"],
    }


def evidence(row: WorkbenchAttempt, body: workbench.EvidenceBody) -> None:
    if "value" in body.model_fields_set:
        raise HTTPException(422, "feedback accepts evidence only")
    if row.fingerprint != body.fingerprint or row.revision != body.revision:
        raise HTTPException(409, "feedback evidence changed")


@router.post("")
async def prepare(body: Prepare, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    scope_id = workbench.user_scope(principal)
    enabled()
    attempt_id = str(body.attempt_id)
    async with SessionLocal() as session:
        scope = await workbench.lock_scope(session, scope_id)
        prior = await session.get(WorkbenchAttempt, attempt_id)
        if prior is not None:
            row, outbox = await load(session, attempt_id, scope_id)
            if row.run_id != body.run_id or outbox.score_revision != body.expected_revision + 1 or outbox.payload["value"] != body.score:
                raise HTTPException(409, "feedback binding mismatch")
            return view(row, outbox)
        if scope.active_attempt is not None:
            raise HTTPException(409, "WORKBENCH_LOCKED")
        run = await session.get(Run, body.run_id)
        if run is None or run.tenant_id != principal.tenant_id:
            raise HTTPException(404, "run not found")
        if run.subject != principal.subject:
            raise HTTPException(403, "only the original initiator can rate")
        if run.state != "completed" or not run.trace_id:
            raise HTTPException(409, "completed run with trace required")
        events = list(
            (
                await session.scalars(
                    select(RunEventRow).where(
                        RunEventRow.run_id == run.run_id,
                        RunEventRow.seq <= run.last_seq,
                        RunEventRow.type == "run.state_changed",
                        RunEventRow.payload["to"].as_string() == "completed",
                    )
                )
            ).all()
        )
        if len(events) != 1:
            raise HTTPException(409, "unique completion evidence required")
        latest = await session.scalar(select(func.max(FeedbackOutbox.score_revision)).where(FeedbackOutbox.run_id == run.run_id)) or 0
        if body.expected_revision != latest:
            raise HTTPException(409, "feedback revision changed")
        revision = latest + 1
        payload = {
            "id": "fb_" + body.attempt_id.hex,
            "name": "user_feedback",
            "dataType": "NUMERIC",
            "traceId": run.trace_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "value": body.score,
            "metadata": {
                "protocol": "runtime-feedback-v1",
                "attempt_id": attempt_id,
                "scope_id": scope_id,
                "tenant_id": run.tenant_id,
                "subject": run.subject,
                "run_id": run.run_id,
                "agent_id": run.agent_id,
                "agent_version": run.agent_version,
                "completion_seq": events[0].seq,
                "score_revision": revision,
            },
        }
        payload_hash = digest(payload)
        row = WorkbenchAttempt(
            attempt_id=attempt_id,
            scope_id=scope_id,
            run_id=run.run_id,
            action="feedback",
            request_seq=events[0].seq,
            fingerprint=payload_hash,
            status="PREPARED",
            revision=1,
            acknowledged=False,
        )
        outbox = FeedbackOutbox(
            attempt_id=attempt_id, run_id=run.run_id, score_revision=revision, payload=payload, payload_hash=payload_hash
        )
        session.add_all([row, outbox])
        scope.active_attempt = attempt_id
        await session.commit()
        return view(row, outbox)


@router.get("/{attempt_id}")
async def receipt(attempt_id: UUID, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    scope_id = workbench.user_scope(principal)
    enabled()
    async with SessionLocal() as session:
        return view(*(await load(session, str(attempt_id), scope_id)))


@router.get("/runs/{run_id}/head")
async def head(run_id: str, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    scope_id = workbench.user_scope(principal)
    enabled()
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if run is None or run.tenant_id != principal.tenant_id:
            raise HTTPException(404, "run not found")
        if run.subject != principal.subject:
            raise HTTPException(403, "only the original initiator can rate")
        if run.state != "completed":
            raise HTTPException(409, "completed run required")
        revision = await session.scalar(select(func.max(FeedbackOutbox.score_revision)).where(FeedbackOutbox.run_id == run_id)) or 0
        return {"scope_id": scope_id, "run_id": run_id, "score_revision": revision}


@router.post("/{attempt_id}/execute")
async def execute(attempt_id: UUID, body: workbench.EvidenceBody, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    scope_id = workbench.user_scope(principal)
    enabled()
    target, token = service()
    async with SessionLocal() as session:
        scope = await workbench.lock_scope(session, scope_id)
        row, outbox = await load(session, str(attempt_id), scope_id)
        evidence(row, body)
        if row.status != "PREPARED":
            return view(row, outbox)
        if scope.active_attempt != row.attempt_id:
            raise HTTPException(409, "feedback scope changed")
        payload = outbox.payload
        row.status = "PENDING_OBSERVATION"
        row.revision += 1
        await session.commit()
        result = view(row, outbox)
    try:
        async with net.client(
            base_url=target,
            headers={"Authorization": "Bearer " + token},
            timeout=5,
            follow_redirects=False,
            trust_env=False,
            transport=_transport,
        ) as client:
            async with client.stream("POST", "/internal/local-feedback", json=payload):
                pass
    except httpx.HTTPError:
        pass
    return result


@router.post("/{attempt_id}/reconcile")
async def reconcile(attempt_id: UUID, body: workbench.EvidenceBody, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    scope_id = workbench.user_scope(principal)
    enabled()
    target, token = service()
    async with SessionLocal() as session:
        row, outbox = await load(session, str(attempt_id), scope_id)
        evidence(row, body)
        if row.status != "PENDING_OBSERVATION":
            return view(row, outbox)
        payload = outbox.payload
    found = None
    try:
        async with net.client(
            base_url=target,
            headers={"Authorization": "Bearer " + token},
            timeout=5,
            follow_redirects=False,
            trust_env=False,
            transport=_transport,
        ) as client:
            async with client.stream("GET", "/internal/local-feedback/" + payload["id"]) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 1024 * 1024:
                        raise ValueError
                if response.status_code == 200:
                    found = json.loads(raw)
    except (httpx.HTTPError, ValueError):
        pass
    matches = observation_matches(found, payload)
    async with SessionLocal() as session:
        scope = await workbench.lock_scope(session, scope_id)
        row, outbox = await load(session, str(attempt_id), scope_id)
        evidence(row, body)
        if matches and scope.active_attempt == row.attempt_id and row.status == "PENDING_OBSERVATION":
            row.status = "OBSERVED"
            row.revision += 1
            outbox.observed_hash = outbox.payload_hash
            await session.commit()
        return view(row, outbox)


def observation_matches(found: Any, payload: dict[str, Any]) -> bool:
    if not isinstance(found, dict) or not isinstance(found.get("score"), dict) or not isinstance(found.get("trace"), dict):
        return False
    score, trace = found["score"], found["trace"]
    if type(score.get("value")) not in (int, float) or score.get("comment"):
        return False
    if digest({key: score.get(key) for key in payload}) != digest(payload):
        return False
    metadata = trace.get("metadata")
    if not isinstance(metadata, dict):
        return False
    attributes = metadata.get("attributes", {})
    if not isinstance(attributes, dict):
        return False
    for key in ("tenant_id", "agent_id", "run_id", "subject"):
        name = "ai." + key
        values = [source[name] for source in (metadata, attributes) if name in source]
        if not values or any(value != payload["metadata"][key] for value in values):
            return False
    return bool(trace.get("id") == payload["traceId"] and trace.get("sessionId") == payload["metadata"]["run_id"])


@router.post("/{attempt_id}/acknowledge")
async def acknowledge(attempt_id: UUID, body: workbench.EvidenceBody, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    scope_id = workbench.user_scope(principal)
    enabled()
    async with SessionLocal() as session:
        scope = await workbench.lock_scope(session, scope_id)
        row, outbox = await load(session, str(attempt_id), scope_id)
        evidence(row, body)
        if row.acknowledged:
            return view(row, outbox)
        if row.status != "OBSERVED" or scope.active_attempt != row.attempt_id or outbox.observed_hash != outbox.payload_hash:
            raise HTTPException(409, "matching observed receipt required")
        row.acknowledged = True
        row.revision += 1
        scope.active_attempt = None
        session.add(
            Audit(
                actor=principal.subject,
                tenant_id=principal.tenant_id,
                auth_method="user",
                action="workbench.feedback.acknowledge",
                object="attempt:" + row.attempt_id,
                result="allow",
            )
        )
        await session.commit()
        return view(row, outbox)

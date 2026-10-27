"""工作台主体锁、写前登记、原子回执与本人确认收口。"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import CursorResult, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_contract import RunEventType, RunState
from runtime.api import auth
from runtime.api.auth import Principal, current_principal
from runtime.api.workbench_create import CreateBody
from runtime.loop import user_input
from runtime.state.db import Audit, Run, RunEventRow, SessionLocal, WorkbenchAttempt, WorkbenchScope
from runtime.state.eventlog import EventLog, RunNotFound, StaleWriter

router = APIRouter(prefix="/v1/workbench")


def binding(principal: Principal) -> dict[str, Any]:
    dimensions = {
        "protocol": "workbench-v1",
        "issuer": auth.settings.jwt_issuer,
        "audience": auth.settings.jwt_audience,
        "tenant_id": principal.tenant_id,
        "subject": principal.subject,
        "auth_method": principal.auth_method,
    }
    scope_id = hashlib.sha256(json.dumps(dimensions, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return dimensions | {"scope_id": scope_id, "actor": principal.actor, "client_id": principal.client_id}


def user_scope(principal: Principal) -> str:
    if principal.auth_method != "user" or not principal.token:
        raise HTTPException(403, "workbench requires a verified user token")
    if principal.actor not in (None, f"app:{principal.client_id}"):
        raise HTTPException(403, "delegated workbench identity is not enabled")
    return str(binding(principal)["scope_id"])


def view(row: WorkbenchAttempt) -> dict[str, Any]:
    result = {
        "scope_id": row.scope_id,
        "attempt_id": row.attempt_id,
        "action": row.action,
        "run_id": row.run_id,
        "request_seq": row.request_seq,
        "fingerprint": row.fingerprint,
        "status": row.status,
        "revision": row.revision,
        "evidence_seq": row.evidence_seq,
        "rejection": row.rejection,
        "acknowledged": row.acknowledged,
    }
    if row.action in ("approve", "reject"):
        result.update(call_id=row.call_id, params_hash=row.params_hash)
    elif row.action == "answer":
        result.update(call_id=row.call_id, value_hmac=row.value_hmac)
    elif row.action == "create":
        result.update(
            value_hmac=row.value_hmac,
            create_agent=row.create_agent,
            create_version=row.create_version,
            create_model=row.create_model,
            create_snapshot=row.create_snapshot,
            create_reason=row.create_reason,
        )
    return result


async def ensure_unlocked(request: Request, principal: Principal = Depends(current_principal)) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    scope_id = str(binding(principal)["scope_id"])
    async with SessionLocal() as session:
        scope = await session.get(WorkbenchScope, scope_id)
        if scope is not None and scope.active_attempt is not None:
            raise HTTPException(409, "WORKBENCH_LOCKED: resolve the original attempt before any new write")


async def lock_scope(session: AsyncSession, scope_id: str) -> WorkbenchScope:
    dialect = session.get_bind().dialect.name
    values = {"scope_id": scope_id, "active_attempt": None, "revision": 0}
    if dialect == "sqlite":
        await session.execute(sqlite_insert(WorkbenchScope).values(**values).on_conflict_do_nothing(index_elements=["scope_id"]))
    elif dialect == "mysql":
        await session.execute(mysql_insert(WorkbenchScope).values(**values).on_duplicate_key_update(scope_id=scope_id))
    else:
        raise HTTPException(503, "unsupported workbench database")
    await session.execute(update(WorkbenchScope).where(WorkbenchScope.scope_id == scope_id).values(revision=WorkbenchScope.revision + 1))
    scope = await session.get(WorkbenchScope, scope_id, populate_existing=True)
    assert scope is not None
    return scope


async def owned(session: AsyncSession, attempt_id: str, scope_id: str) -> WorkbenchAttempt:
    row = await session.get(WorkbenchAttempt, attempt_id, populate_existing=True)
    if row is None or row.scope_id != scope_id:
        raise HTTPException(404, "attempt not found")
    return row


async def pending(session: AsyncSession, run_id: str, principal: Principal) -> int:
    run = await session.get(Run, run_id, populate_existing=True)
    if run is None or run.tenant_id != principal.tenant_id:
        raise HTTPException(404, "run not found")
    if run.subject != principal.subject:
        raise HTTPException(403, "only the run initiator can refresh credential")
    prior = await EventLog(session).list(run_id)
    requested = user_input._pending_request(prior, "credential")
    if run.state != RunState.INPUT_REQUIRED or requested is None:
        raise HTTPException(409, "no pending credential request")
    return requested.seq


class PrepareBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: UUID
    run_id: str = Field(min_length=1, max_length=40)
    action: Literal["credential", "approve", "reject", "answer"] = "credential"
    call_id: str | None = Field(default=None, min_length=1, max_length=128)
    params_hash: str | None = Field(default=None, min_length=1, max_length=64)
    value: Any = Field(default=None, repr=False, exclude=True)

    @model_validator(mode="after")
    def action_binding(self) -> PrepareBody:
        if self.action == "credential":
            if self.call_id is not None or self.params_hash is not None or "value" in self.model_fields_set:
                raise ValueError("credential has no approval binding")
        elif self.action == "answer":
            if not self.call_id or self.params_hash is not None or "value" not in self.model_fields_set:
                raise ValueError("answer requires call_id and value only")
        elif not self.call_id or not self.params_hash or "value" in self.model_fields_set:
            raise ValueError("approval requires call_id and params_hash")
        return self


def answer_binding(scope_id: str, attempt_id: str, run_id: str, call_id: str | None, value: Any) -> str:
    from runtime.loop.toolbroker import hmac_json

    try:
        if user_input.json_depth(value) > user_input.MAX_ANSWER_DEPTH:
            raise ValueError("answer depth")
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return hmac_json(
            {
                "protocol": "workbench-answer-v1",
                "scope_id": scope_id,
                "attempt_id": attempt_id,
                "run_id": run_id,
                "call_id": call_id,
                "value": value,
            }
        )
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise HTTPException(400, "answer must be bounded finite JSON") from None


async def pending_action(session: AsyncSession, body: PrepareBody, request: Request, principal: Principal) -> int:
    if body.action == "credential":
        seq = await pending(session, body.run_id, principal)
        request.state.audit_object = f"run:{body.run_id}"
        return seq
    from runtime.api import messages

    run = await session.get(Run, body.run_id, populate_existing=True)
    if run is None or run.tenant_id != principal.tenant_id:
        raise HTTPException(404, "run not found")
    request.state.audit_object = f"run:{body.run_id}"
    if body.action == "answer":
        if run.subject != principal.subject:
            raise HTTPException(403, "only the run initiator can provide input")
        if run.state != RunState.INPUT_REQUIRED:
            raise HTTPException(409, "run is not awaiting input")
        try:
            return user_input.validate_answer_request(await EventLog(session).list(body.run_id), body.call_id, body.value).seq
        except user_input.InputNotPending:
            raise HTTPException(409, "answer request changed") from None
        except ValueError:
            raise HTTPException(400, "answer does not satisfy the pending input schema") from None
    if run.subject == principal.subject:
        raise HTTPException(403, "the run initiator cannot approve or reject their own run")
    log = await messages._check_pending_approval(
        session, body.run_id, messages.ApprovalBody(call_id=body.call_id, params_hash=body.params_hash), request, principal
    )
    events = await log.list(body.run_id)
    if body.call_id not in messages._awaiting_approval(events):
        raise HTTPException(409, "call no longer awaits approval")
    event = next(
        event for event in reversed(events) if event.type == RunEventType.TOOL_DECISION and event.payload.get("call_id") == body.call_id
    )
    return event.seq


class EvidenceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1, le=2**53 - 1)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    value: Any = Field(default=None, repr=False, exclude=True)


@router.get("/identity")
async def identity(principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    user_scope(principal)
    return binding(principal)


@router.get("/attempts")
async def attempts(principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    scope_id = user_scope(principal)
    async with SessionLocal() as session:
        scope = await session.get(WorkbenchScope, scope_id)
        row = await owned(session, scope.active_attempt, scope_id) if scope and scope.active_attempt else None
        return {"scope_id": scope_id, "active": view(row) if row else None}


@router.get("/attempts/{attempt_id}")
async def receipt(attempt_id: UUID, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    async with SessionLocal() as session:
        return view(await owned(session, str(attempt_id), user_scope(principal)))


@router.post("/attempts")
async def prepare(body: PrepareBody, request: Request, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    scope_id, attempt_id = user_scope(principal), str(body.attempt_id)
    value_hmac = answer_binding(scope_id, attempt_id, body.run_id, body.call_id, body.value) if body.action == "answer" else None
    async with SessionLocal() as session:
        scope = await lock_scope(session, scope_id)
        existing = await session.get(WorkbenchAttempt, attempt_id)
        if existing is not None:
            row = await owned(session, attempt_id, scope_id)
            if (
                row.run_id != body.run_id
                or row.action != body.action
                or row.call_id != body.call_id
                or row.params_hash != body.params_hash
                or row.value_hmac != value_hmac
            ):
                raise HTTPException(409, "attempt binding mismatch")
            return view(row)
        if scope.active_attempt is not None:
            raise HTTPException(409, "WORKBENCH_LOCKED")
        seq = await pending_action(session, body, request, principal)
        descriptor = f"{scope_id}:{body.action}:{body.run_id}:{seq}"
        if body.action == "answer":
            descriptor += json.dumps({"call_id": body.call_id, "value_hmac": value_hmac}, sort_keys=True, separators=(",", ":"))
        elif body.action != "credential":
            descriptor += json.dumps(
                {"call_id": body.call_id, "params_hash": body.params_hash, "ttl_seconds": 900, "comment": ""},
                sort_keys=True,
                separators=(",", ":"),
            )
        fingerprint = hashlib.sha256(descriptor.encode()).hexdigest()
        row = WorkbenchAttempt(
            attempt_id=attempt_id,
            scope_id=scope_id,
            run_id=body.run_id,
            action=body.action,
            call_id=body.call_id,
            params_hash=body.params_hash,
            value_hmac=value_hmac,
            request_seq=seq,
            fingerprint=fingerprint,
            status="PREPARED",
            revision=1,
            acknowledged=False,
        )
        session.add(row)
        scope.active_attempt = attempt_id
        await session.commit()
        return view(row)


@router.post("/create-attempts")
async def prepare_create(body: CreateBody, request: Request, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    from runtime.api import workbench_create

    return await workbench_create.prepare(body, request, principal)


@router.post("/attempts/{attempt_id}/execute")
async def execute(
    attempt_id: UUID, body: EvidenceBody, request: Request, principal: Principal = Depends(current_principal)
) -> dict[str, Any]:
    scope_id = user_scope(principal)
    queue_reason: str | None = None
    async with SessionLocal() as session:
        scope = await lock_scope(session, scope_id)
        row = await owned(session, str(attempt_id), scope_id)
        if body.fingerprint != row.fingerprint:
            raise HTTPException(409, "attempt fingerprint mismatch")
        if row.action == "create":
            from runtime.api import workbench_create

            if "value" not in body.model_fields_set:
                raise HTTPException(422, "create execution requires the original text")
            current_hmac = workbench_create.content_binding(row, body.value)
            if row.value_hmac is None or not hmac.compare_digest(row.value_hmac, current_hmac):
                raise HTTPException(409, "create content binding mismatch")
        elif row.action == "answer":
            if "value" not in body.model_fields_set:
                raise HTTPException(422, "answer execution requires the original value")
            current_hmac = answer_binding(scope_id, row.attempt_id, row.run_id, row.call_id, body.value)
            if row.value_hmac is None or not hmac.compare_digest(row.value_hmac, current_hmac):
                raise HTTPException(409, "answer content binding mismatch")
        elif "value" in body.model_fields_set:
            raise HTTPException(422, "this action does not accept a value")
        if row.status in ("COMMITTED", "REJECTED_NO_EFFECT"):
            return view(row)
        if row.status != "PREPARED" or scope.active_attempt != row.attempt_id or body.revision != row.revision:
            raise HTTPException(409, "attempt state mismatch")
        if row.action not in ("credential", "approve", "reject", "answer", "create"):
            raise HTTPException(409, "unsupported attempt action")
        try:
            async with session.begin_nested():
                from runtime.api import messages

                if row.action != "create":
                    details: dict[str, Any] = {
                        "attempt_id": attempt_id,
                        "run_id": row.run_id,
                        "action": row.action,
                        "call_id": row.call_id,
                        "params_hash": row.params_hash,
                    }
                    if row.action == "answer":
                        details["value"] = body.value
                    prepared = PrepareBody(**details)
                    if await pending_action(session, prepared, request, principal) != row.request_seq:
                        raise HTTPException(409, "pending request changed")
                if row.action == "create":
                    await workbench_create.apply(session, row, body.value, request, principal)
                    event_type = RunEventType.RUN_CREATED
                    queue_reason = "created"
                elif row.action in ("credential", "answer"):
                    await user_input.apply_input(
                        session,
                        row.run_id,
                        principal.subject,
                        row.action,
                        call_id=row.call_id,
                        value=principal.token if row.action == "credential" else body.value,
                        attempt_id=row.attempt_id,
                        value_hmac=row.value_hmac,
                    )
                    event_type = RunEventType.INPUT_PROVIDED
                    queue_reason = "input"
                else:
                    state, durable = await messages.apply_approval(
                        session,
                        row.run_id,
                        messages.ApprovalBody(call_id=row.call_id, params_hash=row.params_hash),
                        request,
                        principal,
                        row.action == "approve",
                        attempt_id=row.attempt_id,
                    )
                    event_type = RunEventType.TOOL_APPROVED if row.action == "approve" else RunEventType.TOOL_REJECTED
                    if durable and state == RunState.WORKING:
                        queue_reason = "approved"
                events = await EventLog(session).list(row.run_id)
                event = next(
                    event
                    for event in reversed(events)
                    if event.type == event_type and event.payload.get("workbench_attempt_id") == row.attempt_id
                )
                row.evidence_seq = event.seq
                row.status = "COMMITTED"
        except (HTTPException, ValueError, RunNotFound, user_input.InputForbidden, user_input.InputNotPending, StaleWriter) as error:
            queue_reason = None
            row.status = "REJECTED_NO_EFFECT"
            row.rejection = "VALIDATION_REJECTED" if not isinstance(error, StaleWriter) else "STALE_WRITER"
        row.revision += 1
        await session.commit()
        result = view(row)
    if queue_reason:
        from runtime.api import messages

        if queue_reason == "approved":
            await messages.continue_after_approval(str(result["run_id"]))
        else:
            await messages._requeue(str(result["run_id"]), queue_reason)
    return result


@router.post("/attempts/{attempt_id}/acknowledge")
async def acknowledge(attempt_id: UUID, body: EvidenceBody, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    scope_id = user_scope(principal)
    if "value" in body.model_fields_set:
        raise HTTPException(422, "acknowledge accepts evidence only")
    async with SessionLocal() as session:
        scope = await lock_scope(session, scope_id)
        row = await owned(session, str(attempt_id), scope_id)
        if body.fingerprint != row.fingerprint:
            raise HTTPException(409, "attempt fingerprint mismatch")
        if row.acknowledged:
            return view(row)
        if scope.active_attempt != row.attempt_id or body.revision != row.revision:
            raise HTTPException(409, "attempt revision mismatch")
        if row.action not in ("credential", "approve", "reject", "answer", "create"):
            raise HTTPException(409, "unsupported terminal action")
        if row.status == "COMMITTED":
            event = await session.get(RunEventRow, (row.run_id, row.evidence_seq))
            matches = event is not None and event.payload.get("workbench_attempt_id") == row.attempt_id
            if matches and event is not None:
                if row.action == "create":
                    matches = (
                        event.type == RunEventType.RUN_CREATED.value
                        and row.request_seq == 0
                        and event.seq == 1
                        and event.payload.get("subject") == principal.subject
                        and event.payload.get("agent_id") == row.create_agent
                        and event.payload.get("agent_version") == row.create_version
                        and event.payload.get("model_alias") == row.create_model
                        and row.create_snapshot is not None
                        and event.payload.get("workbench_snapshot_hash") == row.create_snapshot
                        and row.value_hmac is not None
                        and event.payload.get("workbench_value_hmac") == row.value_hmac
                    )
                elif row.action == "credential":
                    matches = event.type == RunEventType.INPUT_PROVIDED.value and event.payload.get("kind") == "credential"
                elif row.action == "answer":
                    matches = (
                        event.type == RunEventType.INPUT_PROVIDED.value
                        and event.payload.get("kind") == "answer"
                        and event.payload.get("call_id") == row.call_id
                        and event.payload.get("workbench_value_hmac") == row.value_hmac
                        and row.value_hmac is not None
                    )
                elif row.action in ("approve", "reject"):
                    expected_type = RunEventType.TOOL_APPROVED if row.action == "approve" else RunEventType.TOOL_REJECTED
                    matches = (
                        event.type == expected_type.value
                        and event.payload.get("call_id") == row.call_id
                        and event.payload.get("params_hash") == row.params_hash
                        and event.payload.get("approver") == principal.subject
                    )
                else:
                    matches = False
            if not matches:
                raise HTTPException(409, "terminal evidence unavailable")
        elif row.status != "REJECTED_NO_EFFECT" or row.rejection not in ("VALIDATION_REJECTED", "STALE_WRITER"):
            raise HTTPException(409, "no terminal receipt")
        row.acknowledged = True
        row.revision += 1
        result = cast(
            CursorResult[Any],
            await session.execute(
                update(WorkbenchScope)
                .where(WorkbenchScope.scope_id == scope_id, WorkbenchScope.active_attempt == row.attempt_id)
                .values(active_attempt=None)
            ),
        )
        if result.rowcount != 1:
            raise HTTPException(409, "scope changed")
        session.add(
            Audit(
                actor=principal.subject,
                act=principal.actor,
                auth_method=principal.auth_method,
                tenant_id=principal.tenant_id,
                action="workbench.acknowledge",
                object=f"attempt:{row.attempt_id}",
                result="ok",
                detail={"status": row.status, "fingerprint": row.fingerprint, "revision": row.revision, "action": row.action},
            )
        )
        await session.commit()
        return view(row)

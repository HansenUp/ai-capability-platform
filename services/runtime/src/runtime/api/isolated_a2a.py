"""Opt-in E2 HTTP+JSON subset. Separate metadata: never migrated into normal Runtime.

The fixture must create this table in its disposable SQLite before mounting the router.
No automatic ACK, retransmission, model call or worker startup lives in this adapter.
"""

from __future__ import annotations

import hmac
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Integer, String, UniqueConstraint, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ai_contract import RunEventType
from runtime.api import isolated_discovery as discovery
from runtime.api import workbench, workbench_create
from runtime.api.auth import Principal, current_principal, verify_jwt
from runtime.loop.toolbroker import hmac_json
from runtime.state import db

EXTENSION = "urn:ai-platform:isolated:workbench:1"
ENABLED = object()
ACTION = "a2a_create"
PREFIX = "/v1/isolated-a2a"


class Metadata(DeclarativeBase):
    pass


class Mapping(Metadata):
    __tablename__ = "isolated_a2a_mapping"
    attempt_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scope_id: Mapped[str] = mapped_column(String(64))
    message_id: Mapped[str] = mapped_column(String(128))
    context_id: Mapped[str] = mapped_column(String(36), unique=True)
    run_id: Mapped[str] = mapped_column(String(40), unique=True)
    envelope_hmac: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("scope_id", "message_id"),)


class Withdrawal(Metadata):
    __tablename__ = "isolated_a2a_withdrawal"
    attempt_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scope_id: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[str] = mapped_column(String(40))
    message_id: Mapped[str] = mapped_column(String(128))
    context_id: Mapped[str] = mapped_column(String(36))
    revision: Mapped[int] = mapped_column(Integer)


async def withdrawal_evidence(session, row, mapping):
    evidence = await session.get(Withdrawal, row.attempt_id)
    if (
        evidence is None
        or evidence.scope_id != row.scope_id
        or evidence.fingerprint != row.fingerprint
        or evidence.run_id != row.run_id
        or evidence.message_id != mapping.message_id
        or evidence.context_id != mapping.context_id
        or evidence.revision != row.revision - (1 if row.acknowledged else 0)
    ):
        raise HTTPException(409, "withdrawal evidence unavailable")
    await no_creation_effect(session, row)


async def no_creation_effect(session, row):
    for model in (db.Run, db.RunInput, db.RunCredential):
        if await session.get(model, row.run_id) is not None:
            raise HTTPException(409, "creation evidence conflicts with withdrawal")
    for model in (db.RunEventRow, db.ToolOperation, db.ContinuationInput):
        if await session.scalar(select(model.run_id).where(model.run_id == row.run_id).limit(1)) is not None:
            raise HTTPException(409, "creation records conflict with withdrawal")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Part(Strict):
    text: str = Field(min_length=1, max_length=16384)


class Message(Strict):
    messageId: str = Field(min_length=1, max_length=128)
    role: Literal["ROLE_USER"]
    parts: list[Part] = Field(min_length=1, max_length=1)


class Configuration(Strict):
    returnImmediately: Literal[True]

    @field_validator("returnImmediately", mode="before")
    @classmethod
    def explicit_true(cls, value):
        if value is not True:
            raise ValueError("only explicit true is supported")
        return value


class Envelope(Strict):
    message: Message
    configuration: Configuration


class Prepare(Envelope):
    attempt_id: str = Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
    agent_id: str = Field(min_length=1, max_length=64)


class Evidence(Strict):
    attempt_id: str = Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
    revision: int = Field(ge=1, le=2**53 - 1)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class ExtensionMetadata(Strict):
    receipt: Evidence = Field(alias=EXTENSION)


class Send(Envelope):
    metadata: ExtensionMetadata


async def access(request: Request, response: Response, principal: Principal = Depends(current_principal)):
    response.headers["Cache-Control"] = "no-store"
    if (
        getattr(request.app.state, "isolated_a2a", None) is not ENABLED
        or discovery.settings.runtime_env != "dev"
        or db.engine.url.get_backend_name() != "sqlite"
    ):
        raise HTTPException(503, "isolated A2A disabled")
    if request.headers.get("A2A-Version") != "1.0":
        raise HTTPException(400, "VersionNotSupported: explicit 1.0 required")
    if request.headers.get("A2A-Extensions") != EXTENSION:
        raise HTTPException(400, "ExtensionSupportRequired")
    workbench.user_scope(principal)
    if "act" in await verify_jwt(principal.token, request):
        raise HTTPException(403, "direct user required")
    return principal


router = APIRouter(prefix=PREFIX)


def digest(body: Envelope, scope: str, agent: str) -> str:
    return hmac_json(
        {
            "profile": EXTENSION,
            "scope": scope,
            "agent": agent,
            "message": body.message.model_dump(),
            "configuration": body.configuration.model_dump(),
        }
    )


async def admit(request, principal, agent, version):
    catalog = await discovery.discover(request, Response(), principal)
    if not any(item.agent_id == agent and item.version == version for item in catalog.items):
        raise HTTPException(403, "agent unavailable")


async def owned(session, attempt_id, principal):
    row = await workbench.owned(session, attempt_id, workbench.user_scope(principal))
    mapping = await session.get(Mapping, attempt_id)
    if row.action != ACTION or mapping is None or mapping.scope_id != row.scope_id or mapping.run_id != row.run_id:
        raise HTTPException(404, "receipt not found")
    return row, mapping


def receipt(row, mapping):
    return {
        "profile": EXTENSION,
        "receipt": workbench.view(row),
        "messageId": mapping.message_id,
        "contextId": mapping.context_id,
        "taskId": row.run_id if row.status == "COMMITTED" else None,
    }


async def committed_evidence(session, row, principal):
    run = await session.get(db.Run, row.run_id)
    event = await session.get(db.RunEventRow, (row.run_id, row.evidence_seq)) if row.evidence_seq else None
    if (
        run is None
        or run.tenant_id != principal.tenant_id
        or run.subject != principal.subject
        or run.agent_id != row.create_agent
        or run.agent_version != row.create_version
        or event is None
        or event.seq != 1
        or event.type != RunEventType.RUN_CREATED.value
        or event.payload.get("workbench_attempt_id") != row.attempt_id
        or event.payload.get("workbench_snapshot_hash") != row.create_snapshot
        or event.payload.get("workbench_value_hmac") != row.value_hmac
        or event.payload.get("subject") != principal.subject
        or event.payload.get("agent_id") != row.create_agent
        or event.payload.get("agent_version") != row.create_version
        or event.payload.get("model_alias") != row.create_model
        or row.value_hmac is None
        or row.create_snapshot is None
    ):
        raise HTTPException(409, "terminal evidence unavailable")
    return run


@router.post("/prepare")
async def prepare(body: Prepare, request: Request, principal: Principal = Depends(access)):
    attempt_id = str(UUID(body.attempt_id))
    scope_id = workbench.user_scope(principal)
    bound = digest(body, scope_id, body.agent_id)
    async with db.SessionLocal() as session:
        scope = await workbench.lock_scope(session, scope_id)
        old = await session.get(db.WorkbenchAttempt, attempt_id)
        if old is not None:
            row, mapping = await owned(session, attempt_id, principal)
            if not hmac.compare_digest(mapping.envelope_hmac, bound):
                raise HTTPException(409, "message binding mismatch")
            return receipt(row, mapping)
        if scope.active_attempt is not None:
            raise HTTPException(409, "WORKBENCH_LOCKED")
        duplicate = await session.scalar(select(Mapping).where(Mapping.scope_id == scope_id, Mapping.message_id == body.message.messageId))
        if duplicate is not None:
            raise HTTPException(409, "message already registered; recover original attempt")
        agent, reason = await workbench_create.selected_agent(session, body.agent_id, principal)
        await admit(request, principal, agent.agent_id, agent.version)
        row = db.WorkbenchAttempt(
            attempt_id=attempt_id,
            scope_id=scope_id,
            action=ACTION,
            run_id="run_" + uuid4().hex[:20],
            create_agent=agent.agent_id,
            create_version=agent.version,
            create_model=agent.model_alias,
            create_snapshot=workbench_create.snapshot(agent, reason),
            create_reason=reason,
            request_seq=0,
            status="PREPARED",
            revision=1,
            acknowledged=False,
        )
        row.value_hmac = workbench_create.content_binding(row, body.message.parts[0].text)
        mapping = Mapping(
            attempt_id=attempt_id,
            scope_id=scope_id,
            message_id=body.message.messageId,
            context_id=str(uuid4()),
            run_id=row.run_id,
            envelope_hmac=bound,
        )
        row.fingerprint = hmac_json(
            {
                "envelope": bound,
                "context": mapping.context_id,
                "attempt": attempt_id,
                "run": row.run_id,
                "snapshot": row.create_snapshot,
                "value": row.value_hmac,
            }
        )
        session.add_all([row, mapping])
        scope.active_attempt = attempt_id
        await session.commit()
        return receipt(row, mapping)


@router.get("/attempts/{attempt_id}")
async def recover(attempt_id: UUID, principal: Principal = Depends(access)):
    async with db.SessionLocal() as session:
        row, mapping = await owned(session, str(attempt_id), principal)
        if row.status == "COMMITTED":
            await committed_evidence(session, row, principal)
        elif row.rejection == "OWNER_WITHDRAWN":
            await withdrawal_evidence(session, row, mapping)
        return receipt(row, mapping)


@router.post("/message:send")
async def send(body: Send, request: Request, principal: Principal = Depends(access)):
    async with db.SessionLocal() as session:
        scope = await workbench.lock_scope(session, workbench.user_scope(principal))
        row, mapping = await owned(session, body.metadata.receipt.attempt_id, principal)
        if body.metadata.receipt.fingerprint != row.fingerprint or not hmac.compare_digest(
            mapping.envelope_hmac, digest(body, row.scope_id, row.create_agent)
        ):
            raise HTTPException(409, "message binding mismatch")
        # Replays are rejected even after ACK. Recovery is explicitly GET, never re-execution.
        if row.status != "PREPARED" or scope.active_attempt != row.attempt_id or row.revision != body.metadata.receipt.revision:
            raise HTTPException(409, "already executed or evidence changed; use read-only recovery")
        try:
            async with session.begin_nested():
                await admit(request, principal, row.create_agent, row.create_version)
                await workbench_create.apply(session, row, body.message.parts[0].text, request, principal, independent_a2a=True)
                event = await session.get(db.RunEventRow, (row.run_id, 1))
                if event is None or event.payload.get("workbench_attempt_id") != row.attempt_id:
                    raise RuntimeError("create evidence missing")
                row.evidence_seq = 1
                row.status = "COMMITTED"
        except HTTPException as error:
            # Unavailable evidence is unknown, never a fabricated no-effect terminal receipt.
            if error.status_code >= 500:
                raise
            row.status = "REJECTED_NO_EFFECT"
            row.rejection = "VALIDATION_REJECTED"
        row.revision += 1
        await session.commit()
        result = receipt(row, mapping)
    if result["taskId"]:
        from runtime.api import messages

        await messages._requeue(result["taskId"], "created")
        task = await get_task(result["taskId"], principal)
        task["metadata"] = {EXTENSION: result}
        return {"task": task}
    raise HTTPException(409, "execution rejected without effect; read the original receipt")


async def final_artifact(session, run):
    """Project only an explicitly linked final response; never infer from the last text."""
    if run.state != "completed" or run.last_seq < 3:
        return None
    terminal = await session.get(db.RunEventRow, (run.run_id, run.last_seq))
    done = await session.get(db.RunEventRow, (run.run_id, run.last_seq - 1))
    if (
        terminal is None
        or terminal.type != "run.state_changed"
        or terminal.payload.get("to") != "completed"
        or done is None
        or done.type != "run.done"
        or done.payload.get("stop_reason") not in ("end_turn", "max_tokens")
    ):
        return None
    seq = done.payload.get("final_response_seq")
    if type(seq) is not int or not 1 < seq < done.seq:
        return None
    event = await session.get(db.RunEventRow, (run.run_id, seq))
    if event is None or event.type != "model.response" or "withheld_text_hmac" in event.payload:
        return None
    text = event.payload.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 65536:
        return None
    return {
        "artifactId": f"{run.run_id}:final:{seq}",
        "name": "Final assistant text",
        "parts": [{"text": text}],
        "metadata": {EXTENSION: {"sourceSeq": seq, "stopReason": done.payload["stop_reason"], "projection": "final-assistant-text-v1"}},
    }


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, principal: Principal = Depends(access)):
    async with db.SessionLocal() as session:
        mapping = await session.scalar(
            select(Mapping).where(Mapping.run_id == task_id, Mapping.scope_id == workbench.user_scope(principal))
        )
        if mapping is None:
            raise HTTPException(404, "task not found")
        row, mapping = await owned(session, mapping.attempt_id, principal)
        if row.status != "COMMITTED":
            raise HTTPException(404, "task not found")
        run = await committed_evidence(session, row, principal)
        states = {
            value: "TASK_STATE_" + value.upper().replace("-", "_")
            for value in ("submitted", "working", "auth-required", "input-required", "completed", "failed", "canceled")
        }
        if run.state not in states:
            raise HTTPException(409, "unknown task state")
        result = {"id": run.run_id, "contextId": mapping.context_id, "status": {"state": states[run.state]}}
        artifact = await final_artifact(session, run)
        if artifact is not None:
            result["artifacts"] = [artifact]
        return result


@router.post("/attempts/{attempt_id}/withdraw")
async def withdraw(attempt_id: UUID, body: Evidence, principal: Principal = Depends(access)):
    async with db.SessionLocal() as session:
        scope = await workbench.lock_scope(session, workbench.user_scope(principal))
        row, mapping = await owned(session, str(attempt_id), principal)
        if (
            body.attempt_id != row.attempt_id
            or body.fingerprint != row.fingerprint
            or body.revision != row.revision
            or scope.active_attempt != row.attempt_id
            or row.status != "PREPARED"
            or row.acknowledged
            or row.evidence_seq is not None
            or row.request_seq != 0
        ):
            raise HTTPException(409, "registration changed; recover original receipt")
        await no_creation_effect(session, row)
        row.status = "REJECTED_NO_EFFECT"
        row.rejection = "OWNER_WITHDRAWN"
        row.revision += 1
        session.add(
            Withdrawal(
                attempt_id=row.attempt_id,
                scope_id=row.scope_id,
                fingerprint=row.fingerprint,
                run_id=row.run_id,
                message_id=mapping.message_id,
                context_id=mapping.context_id,
                revision=row.revision,
            )
        )
        session.add(
            db.Audit(
                actor=principal.subject,
                tenant_id=principal.tenant_id,
                auth_method=principal.auth_method,
                action="isolated_a2a.withdraw",
                object=f"attempt:{row.attempt_id}",
                result="ok",
                detail={
                    "fingerprint": row.fingerprint,
                    "revision": row.revision,
                    "previous_revision": body.revision,
                    "scope_id": row.scope_id,
                    "reason": "OWNER_WITHDRAWN",
                },
            )
        )
        await session.commit()
        return receipt(row, mapping)


@router.post("/acknowledge")
async def acknowledge(body: Evidence, principal: Principal = Depends(access)):
    async with db.SessionLocal() as session:
        scope = await workbench.lock_scope(session, workbench.user_scope(principal))
        row, mapping = await owned(session, body.attempt_id, principal)
        if (
            row.fingerprint != body.fingerprint
            or row.revision != body.revision
            or row.acknowledged
            or scope.active_attempt != row.attempt_id
        ):
            raise HTTPException(409, "evidence changed; use read-only recovery")
        if row.status == "COMMITTED":
            await committed_evidence(session, row, principal)
        elif row.status == "REJECTED_NO_EFFECT" and row.rejection == "OWNER_WITHDRAWN":
            await withdrawal_evidence(session, row, mapping)
        elif row.status != "REJECTED_NO_EFFECT" or row.rejection != "VALIDATION_REJECTED":
            raise HTTPException(409, "no terminal receipt")
        row.acknowledged = True
        row.revision += 1
        scope.active_attempt = None
        session.add(
            db.Audit(
                actor=principal.subject,
                tenant_id=principal.tenant_id,
                auth_method=principal.auth_method,
                action="isolated_a2a.acknowledge",
                object=f"attempt:{row.attempt_id}",
                result="ok",
                detail={"fingerprint": row.fingerprint, "revision": row.revision},
            )
        )
        await session.commit()
        return receipt(row, mapping)


@router.get("/.well-known/agent-card.json")
async def card(request: Request, principal: Principal = Depends(access)):
    return {
        "name": "Isolated Runtime E2",
        "description": "Restricted independent Task profile; synthetic environments only",
        "version": "isolated-e2-v1",
        "supportedInterfaces": [
            {"url": str(request.base_url).rstrip("/") + PREFIX, "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"}
        ],
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "extensions": [
                {
                    "uri": EXTENSION,
                    "required": True,
                    "description": (
                        "Explicit prepare, metadata-bound execution, GET recovery and owner ACK. Single text, returnImmediately=true only."
                    ),
                }
            ],
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "securitySchemes": {"directUser": {"httpAuthSecurityScheme": {"scheme": "bearer", "bearerFormat": "JWT"}}},
        "securityRequirements": [{"schemes": {"directUser": {"list": []}}}],
        "skills": [
            {
                "id": "independent-task",
                "name": "Independent task",
                "description": "E1a admission checked per preparation and execution",
                "tags": ["isolated", "explicit-receipt"],
            }
        ],
    }

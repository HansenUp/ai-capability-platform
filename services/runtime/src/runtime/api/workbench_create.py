"""单条纯文本创建的预留范围与调用方持有事务写入。"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Self
from uuid import UUID, uuid4

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.api.auth import Principal
from runtime.api.synthetic_attachments import AttachmentBody
from runtime.loop.toolbroker import hmac_json
from runtime.state.db import AgentVersion, SessionLocal, WorkbenchAttempt


class CreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: UUID
    agent_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, repr=False, exclude=True)
    conversation_id: UUID | None = None
    selected_run_ids: list[str] = Field(default_factory=list, max_length=20)
    synthetic_attachments: list[AttachmentBody] = Field(default_factory=list, max_length=3, repr=False, exclude=True)

    @model_validator(mode="after")
    def history_requires_conversation(self) -> Self:
        if (self.selected_run_ids or self.synthetic_attachments) and self.conversation_id is None:
            raise ValueError("history requires a conversation")
        return self


def snapshot(agent: AgentVersion, reason: str) -> str:
    fields = (
        "agent_id",
        "version",
        "tenant_id",
        "status",
        "model_alias",
        "harness",
        "system_prompt",
        "config",
        "harness_version",
        "model_resolved",
        "skills_digest",
        "skills_lock",
        "tools_schema_version",
        "retrieval_config",
        "eval_run_ref",
        "eval_passed",
        "rollout_percent",
        "rollout_allowlist",
    )
    return hmac_json(
        {"protocol": "workbench-create-snapshot-v1", "reason": reason, "agent": {name: getattr(agent, name) for name in fields}}
    )


async def selected_agent(session: AsyncSession, agent_id: str, principal: Principal) -> tuple[AgentVersion, str]:
    from runtime.api import messages

    selected, reason = await messages._load_agent(agent_id, None, principal.tenant_id, principal.subject)
    locked = (await session.execute(select(AgentVersion).where(AgentVersion.id == selected.id).with_for_update())).scalar_one_or_none()
    if locked is None or not hmac.compare_digest(snapshot(selected, reason), snapshot(locked, reason)):
        raise HTTPException(409, "agent snapshot changed during admission")
    return locked, reason


def content_binding(row: WorkbenchAttempt, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(422, "create execution requires the original nonempty text")
    try:
        value.encode("utf-8")
        return hmac_json(
            {
                "protocol": "workbench-create-v1",
                "scope_id": row.scope_id,
                "attempt_id": row.attempt_id,
                "run_id": row.run_id,
                "agent_id": row.create_agent,
                "agent_version": row.create_version,
                "snapshot": row.create_snapshot,
                "text": value,
            }
        )
    except UnicodeError:
        raise HTTPException(400, "create text must be valid Unicode") from None


async def prepare(body: CreateBody, request: Request, principal: Principal) -> dict[str, Any]:
    from runtime.api import conversations, workbench

    if body.conversation_id is not None:
        conversations.enabled()
    scope_id, attempt_id = workbench.user_scope(principal), str(body.attempt_id)
    async with SessionLocal() as session:
        scope = await workbench.lock_scope(session, scope_id)
        existing = await session.get(WorkbenchAttempt, attempt_id)
        if existing is not None:
            row = await workbench.owned(session, attempt_id, scope_id)
            if row.action != "create" or row.create_agent != body.agent_id or row.value_hmac != content_binding(row, body.text):
                raise HTTPException(409, "create attempt binding mismatch")
            await conversations.check_repeat(session, row, body)
            return workbench.view(row)
        if scope.active_attempt is not None:
            raise HTTPException(409, "WORKBENCH_LOCKED")
        agent, reason = await selected_agent(session, body.agent_id, principal)
        row = WorkbenchAttempt(
            attempt_id=attempt_id,
            scope_id=scope_id,
            action="create",
            run_id="run_" + uuid4().hex[:20],
            create_agent=agent.agent_id,
            create_version=agent.version,
            create_model=agent.model_alias,
            create_snapshot=snapshot(agent, reason),
            create_reason=reason,
            request_seq=0,
            status="PREPARED",
            revision=1,
            acknowledged=False,
        )
        continuation_digest = await conversations.register(session, row, body, principal) if body.conversation_id else None
        row.value_hmac = content_binding(row, body.text)
        descriptor = {
            "scope_id": scope_id,
            "action": "create",
            "run_id": row.run_id,
            "request_seq": 0,
            "value_hmac": row.value_hmac,
            "snapshot": row.create_snapshot,
        }
        if continuation_digest is not None:
            descriptor["continuation_digest"] = continuation_digest
        row.fingerprint = hashlib.sha256(json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        session.add(row)
        scope.active_attempt = attempt_id
        request.state.audit_object = f"attempt:{attempt_id}"
        await session.commit()
        return workbench.view(row)


async def apply(
    session: AsyncSession, row: WorkbenchAttempt, value: str, request: Request, principal: Principal, *, independent_a2a: bool = False
) -> None:
    from runtime.api import messages
    from runtime.loop import runner

    if not row.create_agent or not row.create_version or not row.create_snapshot or row.request_seq != 0:
        raise HTTPException(409, "invalid create attempt binding")
    agent, reason = await selected_agent(session, row.create_agent, principal)
    if agent.version != row.create_version or not hmac.compare_digest(snapshot(agent, reason), row.create_snapshot):
        raise HTTPException(409, "prepared agent selection or snapshot changed")
    from runtime.api import conversations

    if independent_a2a:
        if row.action != "a2a_create":
            raise HTTPException(409, "independent A2A action required")
        conversation_id = None
    else:
        value, conversation_id = await conversations.execution(session, row, value, principal)
    initial, sources, images, attachments = await messages._prepare_input(agent, [{"role": "user", "content": value}], [], principal)
    if sources or images or attachments:
        raise HTTPException(409, "workbench create supports plain text only")
    await runner.persist_run(
        session,
        agent,
        principal.subject,
        conversation_id,
        actor=principal.actor,
        rollout=reason,
        initial_messages=initial,
        durable=True,
        credential=principal.token,
        run_id=row.run_id,
        attempt_id=row.attempt_id,
        value_hmac=row.value_hmac,
        snapshot_hash=row.create_snapshot,
    )
    request.state.audit_object = f"run:{row.run_id}"

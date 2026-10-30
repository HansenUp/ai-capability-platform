"""Opt-in isolated conversations. All history is read from Runtime, never the browser."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import Depends, HTTPException, Query
from sqlalchemy import and_, or_, select

from runtime.api.auth import Principal, current_principal
from runtime.config import settings
from runtime.loop.toolbroker import hmac_json
from runtime.state.db import ContinuationInput, Conversation, Run, RunEventRow, SessionLocal

TERMINAL = {"completed", "failed", "canceled"}


def enabled() -> None:
    if not settings.isolated_conversations or settings.runtime_env != "dev" or not settings.database_url.startswith("sqlite+"):
        raise HTTPException(404, "isolated conversations disabled")


def legacy(row: Any) -> bool:
    descriptor = {
        "scope_id": row.scope_id,
        "action": "create",
        "run_id": row.run_id,
        "request_seq": 0,
        "value_hmac": row.value_hmac,
        "snapshot": row.create_snapshot,
    }
    return row.fingerprint == hashlib.sha256(json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def owner(session: Any, conversation_id: str, principal: Principal, agent: str | None = None) -> Conversation:
    from runtime.api.workbench import user_scope

    row = await session.get(Conversation, conversation_id)
    if row is None or row.scope_id != user_scope(principal) or row.tenant_id != principal.tenant_id or row.subject != principal.subject:
        raise HTTPException(404, "conversation not found")
    if agent is not None and row.agent_id != agent:
        raise HTTPException(409, "conversation agent mismatch")
    return row


def page_binding(scope: str, agent: str, conversation: str | None, limit: int) -> dict[str, Any]:
    return {"protocol": "isolated-conversation-page-v1", "scope": scope, "agent": agent, "conversation": conversation, "limit": limit}


def encode_cursor(binding: dict[str, Any], after: list[str]) -> str:
    payload = binding | {"after": after}
    return base64.urlsafe_b64encode(json.dumps({"payload": payload, "mac": hmac_json(payload)}, sort_keys=True).encode()).decode()


def decode_cursor(cursor: str | None, binding: dict[str, Any]) -> list[str] | None:
    if cursor is None:
        return None
    try:
        if not cursor or len(cursor) > 2048:
            raise ValueError
        envelope = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        payload = envelope["payload"]
        after = payload["after"]
        if payload != binding | {"after": after} or not hmac.compare_digest(envelope["mac"], hmac_json(payload)):
            raise ValueError
        expected = 2 if binding["conversation"] else 1
        if not isinstance(after, list) or len(after) != expected or any(not isinstance(item, str) or not item for item in after):
            raise ValueError
        if expected == 2:
            instant = datetime.fromisoformat(after[0])
            if instant.tzinfo is None or len(after[1]) > 40:
                raise ValueError
        else:
            UUID(after[0])
        return after
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise HTTPException(400, "invalid or mismatched conversation cursor") from None


async def history(session: Any, conversation: Conversation, limit: int, cursor: str | None) -> dict[str, Any]:
    binding = page_binding(conversation.scope_id, conversation.agent_id, conversation.conversation_id, limit)
    after = decode_cursor(cursor, binding)
    query = select(Run).where(
        Run.session_id == conversation.conversation_id,
        Run.tenant_id == conversation.tenant_id,
        Run.subject == conversation.subject,
        Run.agent_id == conversation.agent_id,
    )
    if after:
        instant = datetime.fromisoformat(after[0])
        query = query.where(or_(Run.created_at > instant, and_(Run.created_at == instant, Run.run_id > after[1])))
    rows = (await session.execute(query.order_by(Run.created_at, Run.run_id).limit(limit + 1))).scalars().all()
    more = len(rows) > limit
    rows = rows[:limit]
    return {
        "items": [{"run_id": row.run_id, "state": row.state, "selectable": row.state in TERMINAL} for row in rows],
        "next_cursor": encode_cursor(binding, [rows[-1].created_at.isoformat(), rows[-1].run_id]) if more else None,
        "page_size": limit,
        "snapshot_guaranteed": False,
    }


async def register(session: Any, row: Any, body: Any, principal: Principal) -> str:
    enabled()
    from runtime.api.synthetic_attachments import SCANNER, accepted

    documents = accepted(body.synthetic_attachments)
    conversation_id = str(body.conversation_id)
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        if body.selected_run_ids:
            raise HTTPException(404, "conversation not found")
        conversation = Conversation(
            conversation_id=conversation_id,
            scope_id=row.scope_id,
            tenant_id=principal.tenant_id,
            subject=principal.subject,
            agent_id=body.agent_id,
        )
        session.add(conversation)
        await session.flush()
    else:
        conversation = await owner(session, conversation_id, principal, body.agent_id)
    if len(body.selected_run_ids) != len(set(body.selected_run_ids)):
        raise HTTPException(422, "duplicate history selection")
    # Admission is bounded by the explicit selection, not total conversation size.
    rows = (
        (
            await session.execute(
                select(Run).where(
                    Run.run_id.in_(body.selected_run_ids),
                    Run.session_id == conversation_id,
                    Run.tenant_id == conversation.tenant_id,
                    Run.subject == conversation.subject,
                    Run.agent_id == conversation.agent_id,
                )
            )
        )
        .scalars()
        .all()
    )
    available = {item.run_id: {"state": item.state, "selectable": item.state in TERMINAL} for item in rows}
    selected = []
    for run_id in body.selected_run_ids:
        if run_id not in available or not available[run_id]["selectable"]:
            raise HTTPException(409, "selected history unavailable")
        prior = (
            await session.execute(
                select(ContinuationInput).where(ContinuationInput.run_id == run_id, ContinuationInput.conversation_id == conversation_id)
            )
        ).scalar_one_or_none()
        if prior is None:
            raise HTTPException(409, "authoritative question unavailable")
        # Never recursively copy a prior composed prompt, hidden thinking, or tool output.
        events = (
            (
                await session.execute(
                    select(RunEventRow).where(RunEventRow.run_id == run_id, RunEventRow.type == "model.response").order_by(RunEventRow.seq)
                )
            )
            .scalars()
            .all()
        )
        messages = [{"role": "user", "content": prior.question}]
        messages.extend(
            {"role": "assistant", "content": event.payload["text"]} for event in events if isinstance(event.payload.get("text"), str)
        )
        selected.append({"run_id": run_id, "state": available[run_id]["state"], "messages": messages})
    composed = json.dumps(
        {
            "protocol": "isolated-continuation-v1",
            "conversation_id": conversation_id,
            "question": body.text,
            "selected_history": selected,
            "synthetic_attachments": documents,
            "attachment_scanner": SCANNER,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    digest = hashlib.sha256(composed.encode()).hexdigest()
    session.add(
        ContinuationInput(
            attempt_id=row.attempt_id,
            conversation_id=conversation_id,
            run_id=row.run_id,
            question=body.text,
            selected_run_ids=body.selected_run_ids,
            composed=composed,
            digest=digest,
        )
    )
    return digest


async def check_repeat(session: Any, row: Any, body: Any) -> None:
    if legacy(row):
        if body.conversation_id is not None:
            raise HTTPException(409, "conversation binding mismatch")
        return
    enabled()
    saved = await session.get(ContinuationInput, row.attempt_id)
    if saved is None or saved.conversation_id != str(body.conversation_id) or saved.selected_run_ids != body.selected_run_ids:
        raise HTTPException(409, "conversation binding mismatch")
    from runtime.api.synthetic_attachments import accepted

    if json.loads(saved.composed).get("synthetic_attachments", []) != accepted(body.synthetic_attachments):
        raise HTTPException(409, "attachment binding mismatch")


async def execution(session: Any, row: Any, value: str, principal: Principal) -> tuple[str, str | None]:
    if legacy(row):
        return value, None
    enabled()
    saved = await session.get(ContinuationInput, row.attempt_id)
    if (
        saved is None
        or saved.question != value
        or saved.run_id != row.run_id
        or hashlib.sha256(saved.composed.encode()).hexdigest() != saved.digest
    ):
        raise HTTPException(409, "continuation snapshot unavailable")
    await owner(session, saved.conversation_id, principal, row.create_agent)
    descriptor = {
        "scope_id": row.scope_id,
        "action": "create",
        "run_id": row.run_id,
        "request_seq": 0,
        "value_hmac": row.value_hmac,
        "snapshot": row.create_snapshot,
        "continuation_digest": saved.digest,
    }
    if row.fingerprint != hashlib.sha256(json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()).hexdigest():
        raise HTTPException(409, "continuation snapshot mismatch")
    return saved.composed, saved.conversation_id


async def list_conversations(
    agent_id: str,
    principal: Principal = Depends(current_principal),
    limit: int = Query(default=200, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=2048),
) -> dict[str, Any]:
    from runtime.api.workbench import user_scope

    enabled()
    scope = user_scope(principal)
    binding = page_binding(scope, agent_id, None, limit)
    after = decode_cursor(cursor, binding)
    async with SessionLocal() as session:
        query = select(Conversation).where(
            Conversation.scope_id == scope,
            Conversation.agent_id == agent_id,
            Conversation.tenant_id == principal.tenant_id,
            Conversation.subject == principal.subject,
        )
        if after:
            query = query.where(Conversation.conversation_id > after[0])
        rows = (await session.execute(query.order_by(Conversation.conversation_id).limit(limit + 1))).scalars().all()
        more = len(rows) > limit
        rows = rows[:limit]
        return {
            "scope_id": scope,
            "agent_id": agent_id,
            "items": [row.conversation_id for row in rows],
            "next_cursor": encode_cursor(binding, [rows[-1].conversation_id]) if more else None,
            "page_size": limit,
            "snapshot_guaranteed": False,
        }


async def read_conversation(
    conversation_id: UUID,
    principal: Principal = Depends(current_principal),
    limit: int = Query(default=200, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=2048),
) -> dict[str, Any]:
    enabled()
    async with SessionLocal() as session:
        row = await owner(session, str(conversation_id), principal)
        return {
            "scope_id": row.scope_id,
            "conversation_id": row.conversation_id,
            "agent_id": row.agent_id,
            **await history(session, row, limit, cursor),
        }

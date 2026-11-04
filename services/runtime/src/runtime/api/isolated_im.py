"""Fixture-only DingTalk inbox. Not a vendor webhook or a Run write interface."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Integer, String, UniqueConstraint, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from typing_extensions import TypedDict

from runtime.api.auth import Principal, current_principal, verify_jwt
from runtime.config import settings
from runtime.state import db

PREFIX = "/v1/isolated-im"
ENABLED = object()
router = APIRouter(prefix=PREFIX)

Binding = tuple[str, str, int]
Identity = tuple[Principal, dict[str, Any], str]


class ReceiptView(TypedDict):
    profile: Literal["isolated-dingtalk-v1"]
    receipt_id: str
    event_id: str
    status: Literal["RECEIVED"]
    received_at: str
    run_created: Literal[False]
    message_sent: Literal[False]
    workbench_path: Literal["/chat"]


class Metadata(DeclarativeBase):
    pass


class Receipt(Metadata):
    __tablename__ = "isolated_im_receipt"
    receipt_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    app_id: Mapped[str] = mapped_column(String(64))
    platform_tenant_id: Mapped[str] = mapped_column(String(64))
    event_id: Mapped[str] = mapped_column(String(128))
    sender_id: Mapped[str] = mapped_column(String(64))
    tenant: Mapped[str] = mapped_column(String(128))
    subject: Mapped[str] = mapped_column(String(128))
    revision: Mapped[int] = mapped_column(Integer)
    digest: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[str] = mapped_column(String(40))
    __table_args__ = (UniqueConstraint("app_id", "platform_tenant_id", "event_id"),)


class Event(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    profile: Literal["isolated-dingtalk-v1"]
    platform: Literal["dingtalk"]
    app_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    platform_tenant_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    event_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    sender_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    chat_type: Literal["direct"]
    message_type: Literal["text"]
    text: str = Field(min_length=1, max_length=4000)


async def identity(request: Request, response: Response, principal: Principal = Depends(current_principal)) -> Identity:
    response.headers["Cache-Control"] = "no-store"
    if (
        getattr(request.app.state, "isolated_im", None) is not ENABLED
        or settings.runtime_env != "dev"
        or db.engine.url.get_backend_name() != "sqlite"
    ):
        raise HTTPException(503, "isolated inbox disabled")
    if principal.auth_method not in ("user", "client_credentials") or not principal.token:
        raise HTTPException(403, "signed identity required")
    claims = await verify_jwt(principal.token, request)
    if "act" in claims:
        raise HTTPException(403, "direct identity required")
    return principal, claims, principal.token


def mapping(request: Request, app: str, tenant: str, sender: str) -> Binding:
    value = getattr(request.app.state, "isolated_im_bindings", {}).get(("dingtalk", app, tenant, sender))
    if (
        not isinstance(value, tuple)
        or len(value) != 3
        or not all(isinstance(v, str) and v for v in value[:2])
        or type(value[2]) is not int
        or value[2] < 1
    ):
        raise HTTPException(403, "sender mapping unavailable")
    return value[0], value[1], value[2]


def view(row: Receipt) -> ReceiptView:
    return {
        "profile": "isolated-dingtalk-v1",
        "receipt_id": row.receipt_id,
        "event_id": row.event_id,
        "status": "RECEIVED",
        "received_at": row.received_at,
        "run_created": False,
        "message_sent": False,
        "workbench_path": "/chat",
    }


@router.post("/events")
async def receive(request: Request, auth: Identity = Depends(identity)) -> ReceiptView:
    principal, claims, token = auth
    source_key = (claims.get("iss"), claims.get("client_id"), claims.get("sub"))
    source = getattr(request.app.state, "isolated_im_sources", {}).get(source_key)
    if principal.auth_method != "client_credentials" or not source or len(source) != 3 or source[2] != principal.tenant_id:
        raise HTTPException(403, "synthetic source identity required")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 32768:
            raise HTTPException(413, "event too large")
    try:
        event = Event.model_validate_json(raw)
    except ValidationError:
        raise HTTPException(422, "invalid synthetic direct text event") from None
    if (event.app_id, event.platform_tenant_id) != source[:2]:
        raise HTTPException(403, "source scope mismatch")
    bound = mapping(request, event.app_id, event.platform_tenant_id, event.sender_id)
    if bound[0] != principal.tenant_id:
        raise HTTPException(403, "tenant mismatch")
    key = getattr(request.app.state, "isolated_im_digest_key", None)
    if not isinstance(key, bytes) or len(key) < 32:
        raise HTTPException(503, "synthetic digest key unavailable")
    canonical = json.dumps(event.model_dump(), sort_keys=True, separators=(",", ":")).encode()
    digest = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    async with db.SessionLocal() as session:
        await session.execute(
            insert(Receipt)
            .values(
                receipt_id=str(uuid4()),
                app_id=event.app_id,
                platform_tenant_id=event.platform_tenant_id,
                event_id=event.event_id,
                sender_id=event.sender_id,
                tenant=bound[0],
                subject=bound[1],
                revision=bound[2],
                digest=digest,
                received_at=datetime.now(UTC).isoformat(),
            )
            .on_conflict_do_nothing(index_elements=["app_id", "platform_tenant_id", "event_id"])
        )
        row = await session.scalar(
            select(Receipt).where(
                Receipt.app_id == event.app_id,
                Receipt.platform_tenant_id == event.platform_tenant_id,
                Receipt.event_id == event.event_id,
            )
        )
        if row is None:
            raise HTTPException(503, "receipt unavailable; delivery outcome unknown")
        if not hmac.compare_digest(row.digest, digest) or (row.tenant, row.subject, row.revision) != bound:
            raise HTTPException(409, "event conflict; original receipt preserved")
        await verify_jwt(token, request)
        if (
            mapping(request, event.app_id, event.platform_tenant_id, event.sender_id) != bound
            or getattr(request.app.state, "isolated_im_sources", {}).get(source_key) != source
        ):
            raise HTTPException(403, "source or mapping changed")
        await session.commit()
        return view(row)


@router.get("/events/{event_id}")
async def recover(
    event_id: str,
    request: Request,
    app_id: str = Query(max_length=64),
    platform_tenant_id: str = Query(max_length=64),
    sender_id: str = Query(max_length=64),
    auth: Identity = Depends(identity),
) -> ReceiptView:
    principal, _, token = auth
    if principal.auth_method != "user":
        raise HTTPException(403, "direct user required")
    bound = mapping(request, app_id, platform_tenant_id, sender_id)
    if bound[:2] != (principal.tenant_id, principal.subject):
        raise HTTPException(403, "sender does not match signed user")
    async with db.SessionLocal() as session:
        row = await session.scalar(
            select(Receipt).where(
                Receipt.app_id == app_id,
                Receipt.platform_tenant_id == platform_tenant_id,
                Receipt.event_id == event_id,
                Receipt.sender_id == sender_id,
            )
        )
        if row is None or (row.tenant, row.subject, row.revision) != bound:
            raise HTTPException(404, "matching receipt unavailable; not proof of no delivery")
        await verify_jwt(token, request)
        if mapping(request, app_id, platform_tenant_id, sender_id) != bound:
            raise HTTPException(403, "mapping changed")
        return view(row)

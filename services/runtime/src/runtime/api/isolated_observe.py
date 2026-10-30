"""O1-O3 disposable observation inbox, never authoritative billing or forwarding."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import JSON, Integer, String, UniqueConstraint, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from runtime.api.auth import Principal, current_principal, verify_jwt
from runtime.config import settings
from runtime.state import db

PREFIX = "/v1/isolated-observe"
ENABLED = object()
router = APIRouter(prefix=PREFIX)


class Metadata(DeclarativeBase):
    pass


class Observation(Metadata):
    __tablename__ = "isolated_observation"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant: Mapped[str] = mapped_column(String(128))
    reporter: Mapped[str] = mapped_column(String(64))
    agent: Mapped[str] = mapped_column(String(64))
    event_id: Mapped[str] = mapped_column(String(128))
    digest: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JSON)
    received_at: Mapped[str] = mapped_column(String(40))
    __table_args__ = (UniqueConstraint("tenant", "reporter", "event_id"),)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Usage(Strict):
    model: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:/-]+$")
    input_tokens: int = Field(ge=0, le=10**12)
    output_tokens: int = Field(ge=0, le=10**12)
    cost: float | None = Field(default=None, ge=0, le=10**9)
    currency: Literal["CNY", "USD"] | None = None

    @model_validator(mode="after")
    def currency_pair(self):
        if (self.cost is None) != (self.currency is None):
            raise ValueError("cost and currency must both be present or absent")
        return self


class Status(Strict):
    instances: int = Field(ge=0, le=10000)
    healthy: bool


class Event(Strict):
    event_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    kind: Literal["usage", "status"]
    payload: dict


async def identity(request: Request, response: Response, principal: Principal = Depends(current_principal)):
    response.headers["Cache-Control"] = "no-store"
    if (
        getattr(request.app.state, "isolated_observe", None) is not ENABLED
        or settings.runtime_env != "dev"
        or db.engine.url.get_backend_name() != "sqlite"
    ):
        raise HTTPException(503, "isolated observation disabled")
    if principal.auth_method not in ("user", "client_credentials") or not principal.token:
        raise HTTPException(403, "signed identity required")
    claims = await verify_jwt(principal.token, request)
    if "act" in claims:
        raise HTTPException(403, "delegation not supported")
    return principal, claims


def view(row):
    return {
        "receipt_id": row.id,
        "event_id": row.event_id,
        "agent_id": row.agent,
        "kind": row.kind,
        "payload": row.payload,
        "received_at": row.received_at,
        "trust": "unverified",
        "billing_eligible": False,
        "forwarded": False,
    }


@router.post("/events")
async def receive(request: Request, auth=Depends(identity)):
    principal, claims = auth
    if principal.auth_method != "client_credentials":
        raise HTTPException(403, "reporter service identity required")
    key = (claims.get("iss"), claims.get("client_id"), claims.get("sub"))
    binding = getattr(request.app.state, "isolated_observe_bindings", {}).get(key)
    if not isinstance(binding, tuple) or len(binding) != 2 or binding[0] != principal.tenant_id:
        raise HTTPException(403, "reporter binding unavailable")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 8192:
            raise HTTPException(413, "event too large")
    try:
        event = Event.model_validate_json(raw)
        model = Usage if event.kind == "usage" else Status
        payload = model.model_validate(event.payload).model_dump(mode="json")
    except ValidationError:
        raise HTTPException(422, "invalid structured observation") from None
    reporter = hashlib.sha256(json.dumps(key, separators=(",", ":")).encode()).hexdigest()
    canonical = json.dumps({"agent": binding[1], "kind": event.kind, "payload": payload}, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    async with db.SessionLocal() as session:
        await session.execute(
            insert(Observation)
            .values(
                tenant=binding[0],
                reporter=reporter,
                agent=binding[1],
                event_id=event.event_id,
                digest=digest,
                kind=event.kind,
                payload=payload,
                received_at=datetime.now(UTC).isoformat(),
            )
            .on_conflict_do_nothing(index_elements=["tenant", "reporter", "event_id"])
        )
        row = await session.scalar(
            select(Observation).where(
                Observation.tenant == binding[0], Observation.reporter == reporter, Observation.event_id == event.event_id
            )
        )
        if row.digest != digest:
            raise HTTPException(409, "event identity conflicts; do not overwrite")
        await session.commit()
        return view(row)


@router.get("/events")
async def history(after: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100), auth=Depends(identity)):
    principal, _ = auth
    if principal.auth_method != "user" or "platform-admin" not in principal.roles:
        raise HTTPException(403, "direct tenant administrator required")
    async with db.SessionLocal() as session:
        rows = list(
            (
                await session.scalars(
                    select(Observation)
                    .where(Observation.tenant == principal.tenant_id, Observation.id > after)
                    .order_by(Observation.id)
                    .limit(limit + 1)
                )
            ).all()
        )
        return {
            "trust": "unverified",
            "billing_eligible": False,
            "snapshot_guaranteed": False,
            "items": [view(row) for row in rows[:limit]],
            "next_after": rows[limit - 1].id if len(rows) > limit else None,
        }

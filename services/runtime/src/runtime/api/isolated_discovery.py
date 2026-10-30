"""E1a fixture-only discovery; not A2A and never an execution permission."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from runtime.api import rollout, workbench
from runtime.api.auth import Principal, current_principal, verify_jwt
from runtime.config import settings
from runtime.state import db

router = APIRouter(prefix="/v1/isolated-discovery")
ENABLED = object()


class Rule(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    agent_id: str = Field(min_length=1, max_length=64)
    groups: list[str]
    roles: list[str]
    allowlist: list[str]


class Snapshot(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    tenant: str = Field(min_length=1)
    revision: int = Field(ge=1)
    rules: list[Rule] = Field(max_length=2000)


class Item(BaseModel):
    agent_id: str
    version: str


class Catalog(BaseModel):
    profile: Literal["isolated-e1a-v1"] = "isolated-e1a-v1"
    items: list[Item]
    snapshot_guaranteed: bool = False


async def candidates(tenant):
    async with db.SessionLocal() as session:
        rows = (
            await session.execute(
                select(
                    db.AgentVersion.id,
                    db.AgentVersion.agent_id,
                    db.AgentVersion.version,
                    db.AgentVersion.rollout_percent,
                    db.AgentVersion.rollout_allowlist,
                )
                .where(db.AgentVersion.tenant_id == tenant, db.AgentVersion.status == "published")
                .order_by(db.AgentVersion.id)
                .limit(2001)
            )
        ).all()
        if len(rows) > 2000:
            raise ValueError("candidate bound exceeded")
        return [dict(row._mapping) for row in rows]


@router.get("/agents", response_model=Catalog)
async def discover(request: Request, response: Response, principal: Principal = Depends(current_principal)):
    response.headers["Cache-Control"] = "no-store"
    if (
        getattr(request.app.state, "isolated_discovery", None) is not ENABLED
        or settings.runtime_env != "dev"
        or db.engine.url.get_backend_name() != "sqlite"
    ):
        raise HTTPException(503, "isolated discovery disabled", headers={"Cache-Control": "no-store"})
    workbench.user_scope(principal)  # Reject dev headers, services and delegation; no administrator bypass.
    claims = await verify_jwt(principal.token, request)
    if "act" in claims:
        raise HTTPException(403, "direct user required", headers={"Cache-Control": "no-store"})
    provider = getattr(request.app.state, "isolated_visibility_reader", None)

    async def read_rules():
        value = Snapshot.model_validate(await provider(principal.tenant_id))
        if value.tenant != principal.tenant_id or len({r.agent_id for r in value.rules}) != len(value.rules):
            raise ValueError("invalid visibility scope")
        return value

    try:
        async with asyncio.timeout(2):
            before = await read_rules()
            rows = await candidates(principal.tenant_id)
            visible = {rule.agent_id for rule in before.rules if not rule.groups and not rule.roles and principal.subject in rule.allowlist}
            grouped = {}
            for row in rows:
                if row["agent_id"] in visible:
                    grouped.setdefault(row["agent_id"], []).append(SimpleNamespace(**row))
            items = []
            for agent_id in sorted(grouped):
                chosen, _ = rollout.choose(grouped[agent_id], principal.subject)
                if chosen is not None:
                    items.append(Item(agent_id=agent_id, version=chosen.version))
            if await read_rules() != before or await candidates(principal.tenant_id) != rows:
                raise ValueError("discovery evidence changed")
            await verify_jwt(principal.token, request)  # Expiry is checked again before delivery.
            return Catalog(items=items)
    except Exception:
        raise HTTPException(503, "discovery evidence unavailable", headers={"Cache-Control": "no-store"}) from None

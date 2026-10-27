"""按节点选择已发布模型版本，不修改 Agent 快照或已有 Run。"""

import hashlib
import json
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError

from runtime import llm
from runtime.config import settings
from runtime.state.db import AgentVersion, NodeCommandReceipt, NodeModelRoute, SessionLocal


class SwitchModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    agent_id: str = Field(min_length=1, max_length=64)
    base_version: str = Field(min_length=1, max_length=32)
    expected_version: str = Field(min_length=1, max_length=32)
    target_version: str = Field(min_length=1, max_length=32)
    model_alias: str = Field(min_length=1, max_length=64)


async def switch(node_id: str, command: dict[str, Any]) -> dict[str, Any]:
    body = SwitchModel.model_validate(command.get("args"))
    tenant = command.get("tenant_id")
    command_id = command.get("id")
    if not isinstance(tenant, str) or not tenant or not isinstance(command_id, str) or not command_id or len(command_id) > 128:
        raise ValueError("trusted tenant and command id are required")
    digest = hashlib.sha256(json.dumps({"tenant": tenant, **body.model_dump()}, sort_keys=True).encode()).hexdigest()
    async with SessionLocal() as session:
        receipt = await session.get(NodeCommandReceipt, (node_id, command_id))
        if receipt is not None:
            if receipt.args_hash != digest:
                raise ValueError("command id was reused with different arguments")
            return receipt.result
        versions = list(
            (
                await session.execute(
                    select(AgentVersion).where(
                        AgentVersion.tenant_id == tenant,
                        AgentVersion.agent_id == body.agent_id,
                        AgentVersion.version.in_([body.base_version, body.expected_version, body.target_version]),
                    )
                )
            ).scalars()
        )
        by_version = {version.version: version for version in versions}
        base = by_version.get(body.base_version)
        target = by_version.get(body.target_version)
        if base is None or target is None or base.status != "published" or target.status != "published" or target.rollout_percent != 100:
            raise ValueError("base and fully released target versions must be published in the command tenant")
        if target.model_alias != body.model_alias:
            raise ValueError("target alias does not match the published snapshot")
        for attribute in ("system_prompt", "harness", "harness_version", "config", "skills_lock", "skills_digest"):
            if getattr(base, attribute) != getattr(target, attribute):
                raise ValueError("model switch cannot change non-model snapshot configuration")
        await llm.get_client().resolve_alias(body.model_alias)
        key = (node_id, tenant, body.agent_id)
        route = await session.get(NodeModelRoute, key)
        if route is None:
            if body.expected_version != body.base_version:
                raise ValueError("stale expected version")
            session.add(
                NodeModelRoute(
                    node_id=node_id,
                    tenant_id=tenant,
                    agent_id=body.agent_id,
                    base_version=body.base_version,
                    target_version=body.target_version,
                )
            )
        else:
            expected = route.target_version if route.base_version == body.base_version else body.base_version
            if body.expected_version != expected:
                raise ValueError("stale expected version")
            result = await session.execute(
                update(NodeModelRoute)
                .where(
                    NodeModelRoute.node_id == node_id,
                    NodeModelRoute.tenant_id == tenant,
                    NodeModelRoute.agent_id == body.agent_id,
                    NodeModelRoute.base_version == route.base_version,
                    NodeModelRoute.target_version == route.target_version,
                )
                .values(base_version=body.base_version, target_version=body.target_version)
            )
            if cast(CursorResult[Any], result).rowcount != 1:
                raise ValueError("stale expected version")
        response = {
            "status": "ok",
            "detail": {
                "scope": "new_unpinned_runs",
                "agent_id": body.agent_id,
                "base_version": body.base_version,
                "target_version": body.target_version,
                "model_alias": body.model_alias,
            },
        }
        session.add(NodeCommandReceipt(node_id=node_id, command_id=command_id, args_hash=digest, result=response))
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ValueError("concurrent command; reload state before retry") from exc
        return response


async def target_version(agent: AgentVersion) -> str | None:
    async with SessionLocal() as session:
        route = await session.get(NodeModelRoute, (settings.node_id, agent.tenant_id, agent.agent_id))
        if route is None or route.base_version != agent.version:
            return None
        target = (
            await session.execute(
                select(AgentVersion).where(
                    AgentVersion.agent_id == agent.agent_id,
                    AgentVersion.tenant_id == agent.tenant_id,
                    AgentVersion.version == route.target_version,
                    AgentVersion.status == "published",
                    AgentVersion.rollout_percent == 100,
                )
            )
        ).scalar_one_or_none()
        if target is None:
            raise ValueError("configured node target is no longer fully published")
        return target.version

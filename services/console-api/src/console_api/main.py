"""Agent Console 后端（Console API，默认端口 8081）。

独立服务、独立库表，承担运营侧控制面：
- Agent 生命周期：草稿 → 发布（评测闸门 + 灰度）→ 放量 / 回滚 → 可见性；发布、灰度、回滚都先同步到 Runtime 的
  /admin 接口，Runtime 成功后才改 Console 侧状态（失败返回 502）；
- 节点控制面：Runtime 节点注册、心跳、指令下发（reload_config / reload_skills / graceful_restart / switch_model）；
  S9.8 起节点接口只接受 ai-node-svc 服务令牌（console_api.node_auth），指令持久化在 t_console_node_command，
  带 id 与回执、拉取不清空队列；``POST /skills/push`` 生成 reload_skills 指令；
- Skill 商店（S9）：评测回放编排 ``POST /skills/{n}/versions/{v}/eval``（console_api.skill_eval）；审核页转发
  ``GET /skills?status=&name=``、``GET /skills/{n}/versions/{v}``、``POST .../approve``、``POST .../revoke {reason}``
  到能力网关（用户令牌取 ``X-AI-User-Token``，否则 ``Authorization: Bearer``；Cedar 判定与禁自批在网关）。前端经
  ``/console/*`` 代理访问（代理去掉 /console 前缀）；
- 路由表：Agent 与节点组的绑定权重，供 APISIX 路由插件拉取（ADR-0003）；
- 审计：所有变更操作记录操作者、租户、动作与对象。
Agent、版本、可见性、归属、审计、节点指令、节点注册、心跳与绑定均存 Console 库。
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from console_api import (
    directory,
    eval_gate,
    feedback_metrics,
    gateway_client,
    node_auth,
    owner_metrics,
    routing_auth,
    runtime_client,
    skill_eval,
    trace_metrics,
)
from console_api.auth import Operator, Role, assert_assigned, require
from console_api.db import (
    Agent,
    AgentVersion,
    Audit,
    Node,
    NodeBinding,
    NodeCommand,
    NodeGroup,
    SessionLocal,
    UserAgent,
    Visibility,
    init_db,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时校验生产基线（CONSOLE_ENV=prod 要求 langfuse 评测闸门），再把 Console 库升级到最新迁移版本。"""
    eval_gate.validate_for_production()
    if os.environ.get("CONSOLE_ENV", "dev") == "prod" and node_auth.production_problems():
        raise RuntimeError("CONSOLE_ENV=prod 节点鉴权基线未满足：" + "；".join(node_auth.production_problems()))
    await init_db()
    yield


app = FastAPI(title="AI Capability Platform · Console API", version="0.1.0", lifespan=lifespan)
app.include_router(directory.router)
app.include_router(trace_metrics.router)
app.include_router(feedback_metrics.router)
app.include_router(owner_metrics.router)
HEARTBEAT_TIMEOUT_S = int(os.environ.get("HEARTBEAT_TIMEOUT_S", "90"))  # 超过该秒数未心跳的节点从路由表剔除

NodeToken = Depends(node_auth.require_node)  # 节点接口：ai-node-svc 服务令牌（S9.8）

Subject = Annotated[str, Field(max_length=128)]


def _audit(s: AsyncSession, op: Operator, action: str, obj: str, detail: dict[str, Any] | None = None) -> None:
    """在当前事务中追加一条审计记录（与业务变更一起提交）。"""
    s.add(Audit(actor=op.user, tenant_id=op.tenant, action=action, object=obj, detail=detail or {}))


def _view(v: AgentVersion) -> dict[str, Any]:
    """版本快照的对外视图。"""
    return {
        "agent_id": v.agent_id,
        "version": v.version,
        "tenant": v.tenant_id,
        "status": v.status,
        "system_prompt": v.system_prompt,
        "model_alias": v.model_alias,
        "harness": v.harness,
        "config": v.config,
        "eval_run_ref": v.eval_run_ref,
        "canary": {"percent": v.canary_percent, "allowlist": v.canary_allowlist},
    }


async def _version(s: AsyncSession, op: Operator, agent_id: str, version: str) -> AgentVersion:
    """取本租户内的版本快照，不存在 404。"""
    q = select(AgentVersion).where(AgentVersion.tenant_id == op.tenant, AgentVersion.agent_id == agent_id, AgentVersion.version == version)
    v = (await s.execute(q)).scalars().first()
    if v is None:
        raise HTTPException(404)
    return v


async def _sync(method: str, path: str, op: Operator, json: dict[str, Any] | None = None, *, tenant_scoped: bool = True) -> dict[str, Any]:
    """同步到 Runtime；失败映射为 502，调用方随之放弃本次 Console 侧变更。
    tenant_scoped：发布 / 放量 / 回滚带上 ?tenant_id=，版本属于其他租户时 Runtime 按不存在处理。"""
    params = {"tenant_id": op.tenant} if tenant_scoped else None
    try:
        return await runtime_client.call(method, path, user=op.user, tenant=op.tenant, json=json, params=params)
    except runtime_client.RuntimeSyncError as e:
        raise HTTPException(502, f"runtime sync failed: {e}") from e


async def _push_snapshot(op: Operator, snapshot: dict[str, Any]) -> None:
    """把草稿快照写到 Runtime。Runtime 返回 409（该版本已在 Runtime 发布，常见于上次 Runtime 成功而 Console 写库失败）
    时继续后续的发布调用：Runtime 的发布对已发布版本幂等，且带租户校验，其他租户的同名版本会得到 404。"""
    try:
        await runtime_client.call("POST", "/admin/agents", user=op.user, tenant=op.tenant, json=snapshot)
    except runtime_client.RuntimeSyncError as e:
        if e.status != 409:
            raise HTTPException(502, f"runtime sync failed: {e}") from e


# ---- Agent 生命周期 ----
class AgentDraft(BaseModel):
    """Agent 版本草稿。"""

    agent_id: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=32)
    system_prompt: str = ""
    model_alias: str = Field(default="default-chat", min_length=1, max_length=64)  # 模型别名（ADR-0006），发布后锁定
    harness: str | None = Field(default=None, max_length=32)  # 指定 Harness；空则用节点默认
    config: dict[str, Any] = Field(default_factory=dict)  # tools / skills / redlines / core_tool_limit 等


@app.post("/agents", status_code=201)
async def create_draft(body: AgentDraft, op: Operator = Depends(require(Role.ADMIN))) -> dict[str, Any]:
    """创建或更新草稿版本；新 Agent 的创建者自动获得归属，已存在的 Agent 需归属校验；已发布版本不可变。"""
    async with SessionLocal() as s:
        agent = (await s.execute(select(Agent).where(Agent.tenant_id == op.tenant, Agent.agent_id == body.agent_id))).scalars().first()
        if agent is None:
            s.add(Agent(tenant_id=op.tenant, agent_id=body.agent_id, created_by=op.user))
            s.add(UserAgent(tenant_id=op.tenant, user=op.user, agent_id=body.agent_id))
        else:
            await assert_assigned(s, op, body.agent_id)
        q = select(AgentVersion).where(
            AgentVersion.tenant_id == op.tenant, AgentVersion.agent_id == body.agent_id, AgentVersion.version == body.version
        )
        v = (await s.execute(q)).scalars().first()
        if v is not None and v.status != "draft":
            raise HTTPException(409, "published versions are immutable; create a new version")
        v = v or AgentVersion(tenant_id=op.tenant, agent_id=body.agent_id, version=body.version, status="draft")
        v.system_prompt, v.model_alias, v.harness, v.config = body.system_prompt, body.model_alias, body.harness, body.config
        s.add(v)
        _audit(s, op, "agent.create", f"{body.agent_id}@{body.version}")
        await s.commit()
        return _view(v)


class PublishIn(BaseModel):
    """发布请求：评测结果 + 灰度参数。"""

    eval_run_ref: str | None = Field(default=None, max_length=128)  # Langfuse 评测运行引用
    eval_passed: bool | None = None  # 评测是否通过；不为 True 时发布被闸门拦截
    canary_percent: int = Field(default=100, ge=0, le=100)  # 灰度流量百分比，100 为全量
    canary_allowlist: list[Subject] = Field(default_factory=list, max_length=1000)  # 灰度白名单主体（如 human:alice）


@app.get("/agents/{agent_id}/eval-gate")
async def agent_eval_gate(agent_id: str, op: Operator = Depends(require(Role.ADMIN))) -> dict[str, Any]:
    async with SessionLocal() as session:
        await assert_assigned(session, op, agent_id)
        exists = (await session.execute(select(Agent).where(Agent.agent_id == agent_id, Agent.tenant_id == op.tenant))).scalar_one_or_none()
        if exists is None:
            raise HTTPException(404)
    invalid = eval_gate.configuration_problems()
    if invalid:
        raise HTTPException(409, "invalid evaluation gate configuration: " + "; ".join(invalid))
    return eval_gate.policy_snapshot(agent_id)


@app.get("/agents/{agent_id}/versions/{version}/evaluation")
async def agent_evaluation(
    agent_id: str,
    version: str,
    response: Response,
    eval_run_ref: Annotated[str | None, Query(max_length=128)] = None,
    op: Operator = Depends(require(Role.ADMIN)),
) -> dict[str, Any]:
    """只读重算指定／已保存评测引用；不是历史发布证明或发布授权。"""
    response.headers["Cache-Control"] = "no-store"
    async with SessionLocal() as session:
        await assert_assigned(session, op, agent_id)
        row = await _version(session, op, agent_id, version)
        reference = eval_run_ref if eval_run_ref is not None else row.eval_run_ref
        stored = row.eval_run_ref
    result = await eval_gate.evaluate(agent_id, version, reference, None, op.tenant)
    if str(result["reason"]).startswith("evaluation run is for"):
        result["reason"] = "evaluation run agent/version mismatch"
    if result["status"] == "unavailable":
        result["reason"] = "evaluation source unavailable or incomplete; no partial statistics returned"
    return result | {
        "agent_id": agent_id,
        "version": version,
        "eval_run_ref": reference,
        "stored_eval_run_ref": stored,
        "policy": eval_gate.policy_snapshot(agent_id),
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": "current_policy_read_only_recomputation_not_release_authorization",
    }


@app.post("/agents/{agent_id}/versions/{version}/publish")
async def publish(agent_id: str, version: str, body: PublishIn, op: Operator = Depends(require(Role.ADMIN))) -> dict[str, Any]:
    """发布版本：评测闸门 → 推送快照并发布到 Runtime（带灰度参数）→ Console 侧标记 published。"""
    async with SessionLocal() as s:
        await assert_assigned(s, op, agent_id)
        v = await _version(s, op, agent_id, version)
        # 评测闸门：对外放量（比例大于 0）必须通过；0% + 白名单为预发布，只供评测账号使用，不经闸门
        if body.canary_percent > 0:
            reason = await eval_gate.check(agent_id, version, body.eval_run_ref, body.eval_passed, op.tenant)
            if reason:
                raise HTTPException(409, f"publish gate: {reason}")  # 业务规则拒绝统一用 409
        elif not body.canary_allowlist:
            raise HTTPException(409, "pre-release (canary_percent=0) requires an allowlist of evaluators")
        snapshot = {"agent_id": agent_id, "version": version, "system_prompt": v.system_prompt, "model_alias": v.model_alias}
        await _push_snapshot(op, snapshot | {"harness": v.harness, "config": v.config, "tenant_id": op.tenant})
        publish_body = {
            "eval_run_ref": body.eval_run_ref,
            "eval_passed": True,
            "rollout_percent": body.canary_percent,
            "rollout_allowlist": body.canary_allowlist,
        }
        await _sync("POST", f"/admin/agents/{agent_id}/versions/{version}/publish", op, publish_body)
        v.status, v.eval_run_ref = "published", body.eval_run_ref
        v.canary_percent, v.canary_allowlist = body.canary_percent, list(body.canary_allowlist)
        _audit(
            s,
            op,
            "agent.publish",
            f"{agent_id}@{version}",
            {
                "eval_run_ref": body.eval_run_ref,
                "canary": body.canary_percent,
                "eval_gate": eval_gate.policy_snapshot(agent_id),
                "eval_checked": body.canary_percent > 0,
            },
        )
        await s.commit()
        return _view(v)


class CanaryIn(BaseModel):
    """调整灰度：放量比例与白名单；从预发布（0%）放量时需附评测结果。"""

    percent: int = Field(ge=0, le=100)
    allowlist: list[Subject] = Field(default_factory=list, max_length=1000)
    eval_run_ref: str | None = Field(default=None, max_length=128)
    eval_passed: bool | None = None


@app.put("/agents/{agent_id}/versions/{version}/canary")
async def set_canary(agent_id: str, version: str, body: CanaryIn, op: Operator = Depends(require(Role.ADMIN))) -> dict[str, Any]:
    """灰度放量：调整已发布版本的比例与白名单（放量到 100 即成为稳定版）。"""
    async with SessionLocal() as s:
        await assert_assigned(s, op, agent_id)
        v = await _version(s, op, agent_id, version)
        if v.status != "published":
            raise HTTPException(409, f"version status is {v.status}")
        if v.canary_percent == 0 and body.percent > 0:  # 预发布 → 对外放量：过评测闸门
            reason = await eval_gate.check(agent_id, version, body.eval_run_ref or v.eval_run_ref, body.eval_passed, op.tenant)
            if reason:
                raise HTTPException(409, f"publish gate: {reason}")
            v.eval_run_ref = body.eval_run_ref or v.eval_run_ref
        await _sync(
            "PUT", f"/admin/agents/{agent_id}/versions/{version}/rollout", op, {"percent": body.percent, "allowlist": body.allowlist}
        )
        before = v.canary_percent
        v.canary_percent, v.canary_allowlist = body.percent, list(body.allowlist)
        _audit(
            s,
            op,
            "agent.canary",
            f"{agent_id}@{version}",
            {
                "from": before,
                "to": body.percent,
                "eval_run_ref": v.eval_run_ref,
                "eval_gate": eval_gate.policy_snapshot(agent_id),
                "eval_checked": before == 0 and body.percent > 0,
            },
        )
        await s.commit()
        return _view(v)


@app.post("/agents/{agent_id}/versions/{version}/rollback")
async def rollback(agent_id: str, version: str, op: Operator = Depends(require(Role.ADMIN))) -> dict[str, Any]:
    """一键回滚：目标版本（曾发布过）置为全量稳定版，其余已发布版本置为 retired；先回滚 Runtime 再改 Console。"""
    async with SessionLocal() as s:
        await assert_assigned(s, op, agent_id)
        target = await _version(s, op, agent_id, version)
        if target.status == "draft":
            raise HTTPException(409, "cannot roll back to a draft version")
        if target.canary_percent == 0:
            # 预发布版本（0% + 评测白名单）没有过评测闸门：不能借回滚直接成为全量稳定版，应走放量（过闸门）
            raise HTTPException(409, "cannot roll back to a pre-release version; promote it via canary (evaluation gate)")
        await _sync("POST", f"/admin/agents/{agent_id}/versions/{version}/rollback", op)
        q = select(AgentVersion).where(
            AgentVersion.tenant_id == op.tenant, AgentVersion.agent_id == agent_id, AgentVersion.status == "published"
        )
        retired = []
        for v in (await s.execute(q)).scalars():
            if v.id != target.id:
                v.status = "retired"
                retired.append(v.version)
        target.status, target.canary_percent, target.canary_allowlist = "published", 100, []
        _audit(s, op, "agent.rollback", f"{agent_id}@{version}", {"retired": retired})
        await s.commit()
    return {"agent_id": agent_id, "active_version": version, "retired": retired}


@app.get("/agents")
async def list_agents(op: Operator = Depends(require(Role.ADMIN))) -> list[dict[str, Any]]:
    """本租户全部版本快照。"""
    async with SessionLocal() as s:
        q = select(AgentVersion).where(AgentVersion.tenant_id == op.tenant).order_by(AgentVersion.id)
        if op.role == Role.ADMIN:
            assigned = select(UserAgent.agent_id).where(UserAgent.tenant_id == op.tenant, UserAgent.user == op.user)
            q = q.where(AgentVersion.agent_id.in_(assigned))
        return [_view(v) for v in (await s.execute(q)).scalars()]


class AgentAdminsIn(BaseModel):
    model_config = {"extra": "forbid"}
    users: list[Annotated[str, Field(strict=True, min_length=1, max_length=128, pattern=r"^[^\s\x00-\x1f\x7f]+$")]] = Field(max_length=1000)


@app.get("/agents/{agent_id}/admins")
async def list_agent_admins(agent_id: str, op: Operator = Depends(require(Role.TENANT_ADMIN))) -> dict[str, Any]:
    async with SessionLocal() as session:
        agent = (await session.execute(select(Agent).where(Agent.tenant_id == op.tenant, Agent.agent_id == agent_id))).scalar_one_or_none()
        if agent is None:
            raise HTTPException(404)
        users = (
            (
                await session.execute(
                    select(UserAgent.user)
                    .where(
                        UserAgent.tenant_id == op.tenant,
                        UserAgent.agent_id == agent_id,
                    )
                    .order_by(UserAgent.user)
                )
            )
            .scalars()
            .all()
        )
    return {"agent_id": agent_id, "users": users}


@app.put("/agents/{agent_id}/admins")
async def replace_agent_admins(agent_id: str, body: AgentAdminsIn, op: Operator = Depends(require(Role.TENANT_ADMIN))) -> dict[str, Any]:
    users = sorted(set(body.users))
    async with SessionLocal() as session:
        agent = (
            await session.execute(
                select(Agent)
                .where(
                    Agent.tenant_id == op.tenant,
                    Agent.agent_id == agent_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if agent is None:
            raise HTTPException(404)
        await session.execute(delete(UserAgent).where(UserAgent.tenant_id == op.tenant, UserAgent.agent_id == agent_id))
        session.add_all([UserAgent(tenant_id=op.tenant, agent_id=agent_id, user=user) for user in users])
        _audit(session, op, "agent.assignment", agent_id, {"users": users})
        await session.commit()
    return {"agent_id": agent_id, "users": users}


class VisibilityIn(BaseModel):
    """Agent 可见范围。"""

    groups: list[str] = Field(default_factory=list)  # 组织 / 部门
    roles: list[str] = Field(default_factory=list)  # 角色
    allowlist: list[str] = Field(default_factory=list)  # 指定用户


@app.put("/agents/{agent_id}/visibility")
async def set_visibility(agent_id: str, body: VisibilityIn, op: Operator = Depends(require(Role.ADMIN))) -> dict[str, Any]:
    """设置 Agent 在门户中的可见范围。"""
    async with SessionLocal() as s:
        await assert_assigned(s, op, agent_id)
        q = select(Visibility).where(Visibility.tenant_id == op.tenant, Visibility.agent_id == agent_id)
        vis = (await s.execute(q)).scalars().first() or Visibility(tenant_id=op.tenant, agent_id=agent_id)
        vis.groups, vis.roles, vis.allowlist = body.groups, body.roles, body.allowlist
        s.add(vis)
        _audit(s, op, "permission.change", agent_id, body.model_dump())
        await s.commit()
    return body.model_dump()


# ---- 节点控制面 ----
class NodeRegister(BaseModel):
    """Runtime 节点注册信息。"""

    node_id: str = Field(min_length=1, max_length=128)
    topology: str = Field(default="central", pattern="^(central|hybrid|standalone)$")
    labels: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(default_factory=lambda: ["general"], max_length=50)
    version: str = Field(default="0", max_length=64)
    ready: bool = True
    boot_id: str | None = Field(default=None, max_length=64)


async def _upsert_node_state(
    session: AsyncSession,
    model: type[Node] | type[NodeBinding] | type[NodeGroup],
    values: dict[str, Any],
    updates: dict[str, Any],
    keys: list[str],
) -> None:
    if session.get_bind().dialect.name == "mysql":
        statement = mysql_insert(model).values(**values)
        await session.execute(statement.on_duplicate_key_update(**updates))
    else:
        statement_sqlite = sqlite_insert(model).values(**values)
        await session.execute(statement_sqlite.on_conflict_do_update(index_elements=keys, set_=updates))


@app.post("/nodes/register")
async def node_register(body: NodeRegister, _node: dict[str, Any] = NodeToken) -> dict[str, Any]:
    """节点启动时注册，返回心跳间隔（需节点服务令牌）。"""
    async with SessionLocal() as session:
        if await session.get(Node, body.node_id) is None:
            for label in sorted(set(body.labels)):
                await _upsert_node_state(
                    session, NodeGroup, {"name": label, "description": "", "created_at": datetime.now(UTC)}, {"name": label}, ["name"]
                )
        updates = {"topology": body.topology, "version": body.version, "last_seen": datetime.now(UTC), "ready": body.ready}
        if body.boot_id is not None:
            updates["boot_id"] = body.boot_id
        await _upsert_node_state(session, Node, body.model_dump() | updates | {"load": 0, "qps": 0}, updates, ["node_id"])
        await session.commit()
    return {"ok": True, "heartbeat_interval_s": 30}


class Heartbeat(BaseModel):
    """节点心跳负载。"""

    load: float = Field(default=0, ge=0, le=1, allow_inf_nan=False)
    qps: float = Field(default=0, ge=0, allow_inf_nan=False)
    version: str = Field(default="0", max_length=64)
    ready: bool = True


@app.post("/nodes/{node_id}/heartbeat")
async def heartbeat(node_id: str, body: Heartbeat, _node: dict[str, Any] = NodeToken) -> dict[str, Any]:
    """更新节点最近心跳与负载；未注册返回 404（节点据此重新注册）。"""
    async with SessionLocal() as session:
        node = await session.get(Node, node_id)
        if node is None:
            raise HTTPException(404, "register first")
        node.last_seen, node.load, node.qps, node.version = datetime.now(UTC), body.load, body.qps, body.version
        node.ready = body.ready
        await session.commit()
    return {"ok": True}


def _node_view(node: Node) -> dict[str, Any]:
    return {
        "node_id": node.node_id,
        "topology": node.topology,
        "labels": node.labels,
        "version": node.version,
        "last_seen": node.last_seen.isoformat(),
        "load": node.load,
        "qps": node.qps,
        "ready": node.ready,
        "boot_id": node.boot_id,
        "healthy": node.ready and (datetime.now(UTC) - node.last_seen).total_seconds() <= HEARTBEAT_TIMEOUT_S,
    }


@app.get("/nodes")
async def list_nodes(op: Operator = Depends(require(Role.TENANT_ADMIN))) -> list[dict[str, Any]]:
    async with SessionLocal() as session:
        return [_node_view(node) for node in (await session.execute(select(Node).order_by(Node.node_id))).scalars()]


@app.get("/node-groups")
async def list_node_groups(op: Operator = Depends(require(Role.TENANT_ADMIN))) -> dict[str, list[str]]:
    async with SessionLocal() as session:
        groups = {name: [] for name in (await session.execute(select(NodeGroup.name).order_by(NodeGroup.name))).scalars()}
        for node in (await session.execute(select(Node).order_by(Node.node_id))).scalars():
            for label in node.labels:
                groups.setdefault(label, []).append(node.node_id)
    return groups


class GroupDescriptionIn(BaseModel):
    description: str = Field(default="", max_length=500)


class GroupCreateIn(GroupDescriptionIn):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


async def _lock_node_groups(session: AsyncSession, labels: list[str]) -> None:
    for label in sorted(set(labels)):
        result = await session.execute(update(NodeGroup).where(NodeGroup.name == label).values(name=label))
        if result.rowcount != 1:
            raise HTTPException(409, f"node group not found: {label}")


@app.get("/node-groups/details")
async def node_group_details(op: Operator = Depends(require(Role.TENANT_ADMIN))) -> list[dict[str, Any]]:
    async with SessionLocal() as session:
        nodes = list((await session.execute(select(Node).order_by(Node.node_id))).scalars())
        return [
            {
                "name": group.name,
                "description": group.description,
                "created_at": group.created_at.isoformat(),
                "nodes": [node.node_id for node in nodes if group.name in node.labels],
            }
            for group in (await session.execute(select(NodeGroup).order_by(NodeGroup.name))).scalars()
        ]


@app.post("/node-groups", status_code=201)
async def create_node_group(body: GroupCreateIn, op: Operator = Depends(require(Role.PLATFORM_ADMIN))) -> dict[str, str]:
    async with SessionLocal() as session:
        session.add(NodeGroup(name=body.name, description=body.description))
        _audit(session, op, "node_group.create", body.name, body.model_dump())
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(409, "node group already exists") from exc
    return body.model_dump()


@app.put("/node-groups/{name}")
async def edit_node_group(name: str, body: GroupDescriptionIn, op: Operator = Depends(require(Role.PLATFORM_ADMIN))) -> dict[str, str]:
    async with SessionLocal() as session:
        await _lock_node_groups(session, [name])
        group = await session.get(NodeGroup, name)
        group.description = body.description
        _audit(session, op, "node_group.update", name, body.model_dump())
        await session.commit()
    return {"name": name, "description": body.description}


@app.delete("/node-groups/{name}")
async def delete_node_group(name: str, op: Operator = Depends(require(Role.PLATFORM_ADMIN))) -> dict[str, bool]:
    async with SessionLocal() as session:
        await _lock_node_groups(session, [name])
        if name == "general":
            raise HTTPException(409, "default group cannot be deleted")
        if any(name in node.labels for node in (await session.execute(select(Node))).scalars()):
            raise HTTPException(409, "group still has registered nodes")
        if any(name in binding.weights for binding in (await session.execute(select(NodeBinding))).scalars()):
            raise HTTPException(409, "group is referenced by an Agent binding")
        await session.delete(await session.get(NodeGroup, name))
        _audit(session, op, "node_group.delete", name)
        await session.commit()
    return {"ok": True}


class NodeGroupsIn(BaseModel):
    labels: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(min_length=1, max_length=50)


@app.put("/nodes/{node_id}/groups")
async def set_node_groups(node_id: str, body: NodeGroupsIn, op: Operator = Depends(require(Role.PLATFORM_ADMIN))) -> dict[str, Any]:
    async with SessionLocal() as session:
        node = await session.get(Node, node_id)
        if node is None:
            raise HTTPException(404, "node not found")
        await _lock_node_groups(session, body.labels)
        node.labels = sorted(set(body.labels))
        _audit(session, op, "node.groups", node_id, {"labels": node.labels})
        await session.commit()
        return _node_view(node)


def _command_view(cmd: NodeCommand) -> dict[str, Any]:
    return {
        "id": cmd.command_id,
        "node_id": cmd.node_id,
        "tenant_id": cmd.tenant_id,
        "authorization_role": cmd.authorization_role,
        "type": cmd.type,
        "args": cmd.args,
        "status": cmd.status,
        "result": cmd.result,
        "created_at": cmd.created_at.isoformat() if cmd.created_at else None,
        "acked_at": cmd.acked_at.isoformat() if cmd.acked_at else None,
    }


@app.get("/nodes/{node_id}/command-history")
async def command_history(
    node_id: str,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before_id: Annotated[int | None, Query(gt=0, le=2**63 - 1)] = None,
    status: Literal["pending", "ok", "error", "unsupported"] | None = None,
    type: Annotated[str | None, Query(min_length=1, max_length=32)] = None,
    op: Operator = Depends(require(Role.TENANT_ADMIN)),
) -> dict[str, Any]:
    query = select(NodeCommand).where(NodeCommand.node_id == node_id, NodeCommand.tenant_id == op.tenant)
    if before_id is not None:
        query = query.where(NodeCommand.id < before_id)
    if status is not None:
        query = query.where(NodeCommand.status == status)
    if type is not None:
        query = query.where(NodeCommand.type == type)
    async with SessionLocal() as session:
        rows = list((await session.execute(query.order_by(NodeCommand.id.desc()).limit(limit + 1))).scalars())
    response.headers["Cache-Control"] = "no-store"
    return {
        "tenant_id": op.tenant,
        "node_id": node_id,
        "items": [
            {
                "id": row.command_id,
                "node_id": row.node_id,
                "tenant_id": row.tenant_id,
                "type": row.type,
                "status": row.status,
                "created_by": row.created_by,
                "created_at": row.created_at.isoformat(),
                "acked_at": row.acked_at.isoformat() if row.acked_at else None,
            }
            for row in rows[:limit]
        ],
        "next_before_id": rows[limit - 1].id if len(rows) > limit else None,
        "order": "newest_first",
        "snapshot_guaranteed": False,
    }


@app.get("/nodes/{node_id}/commands")
async def pull_commands(node_id: str, _node: dict[str, Any] = NodeToken) -> list[dict[str, Any]]:
    """节点拉取待执行指令（按下发顺序）；拉取不清空，回执后才不再下发。"""
    async with SessionLocal() as s:
        q = select(NodeCommand).where(NodeCommand.node_id == node_id, NodeCommand.status == "pending").order_by(NodeCommand.id)
        return [_command_view(c) for c in (await s.execute(q)).scalars()]


@app.get("/nodes/{node_id}/commands/{command_id}/receipt")
async def node_command_receipt(node_id: str, command_id: str, _node: dict[str, Any] = NodeToken) -> dict[str, Any]:
    async with SessionLocal() as session:
        command = (
            await session.execute(select(NodeCommand).where(NodeCommand.node_id == node_id, NodeCommand.command_id == command_id))
        ).scalar_one_or_none()
        if command is None:
            raise HTTPException(404, "command not found")
        return _command_view(command)


class AckIn(BaseModel):
    """节点对一条指令的回执。"""

    status: str = Field(pattern="^(ok|error|unsupported)$")
    detail: dict[str, Any] = Field(default_factory=dict)


@app.post("/nodes/{node_id}/commands/{command_id}/ack")
async def ack_command(node_id: str, command_id: str, body: AckIn, _node: dict[str, Any] = NodeToken) -> dict[str, Any]:
    """节点回执：pending → ok / error / unsupported；重复回执幂等（返回首次结果）。"""
    async with SessionLocal() as s:
        await s.execute(
            update(NodeCommand)
            .where(NodeCommand.node_id == node_id, NodeCommand.command_id == command_id, NodeCommand.status == "pending")
            .values(status=body.status, result=body.detail, acked_at=datetime.now(UTC))
        )
        await s.commit()
        q = select(NodeCommand).where(NodeCommand.node_id == node_id, NodeCommand.command_id == command_id)
        cmd = (await s.execute(q)).scalars().first()
        if cmd is None:
            raise HTTPException(404, "command not found")
        return _command_view(cmd)


class CommandIn(BaseModel):
    """下发给节点的指令。"""

    type: str = Field(max_length=32)  # reload_config | reload_skills | graceful_restart | switch_model
    args: dict[str, Any] = Field(default_factory=dict)


async def _enqueue(s: AsyncSession, op: Operator, node_id: str, body: CommandIn) -> NodeCommand:
    if body.type == "graceful_restart" and op.role < Role.PLATFORM_ADMIN:
        raise HTTPException(403, "shared node restart requires PLATFORM_ADMIN")
    cmd = NodeCommand(
        command_id=str(uuid.uuid4()),
        node_id=node_id,
        tenant_id=op.tenant,
        type=body.type,
        args=body.args,
        status="pending",
        created_by=op.user,
        authorization_role=op.role.name,
    )
    s.add(cmd)
    _audit(s, op, "node.command", node_id, {"id": cmd.command_id} | body.model_dump())
    return cmd


@app.post("/nodes/{node_id}/commands", status_code=202)
async def push_command(node_id: str, body: CommandIn, op: Operator = Depends(require(Role.TENANT_ADMIN))) -> dict[str, Any]:
    """租户管理员向节点下发指令，进入队列等待节点拉取与回执。"""
    async with SessionLocal() as s:
        cmd = await _enqueue(s, op, node_id, body)
        await s.commit()
        q = select(func.count()).select_from(NodeCommand).where(NodeCommand.node_id == node_id, NodeCommand.status == "pending")
        queued = int((await s.execute(q)).scalar_one())
    return {"id": cmd.command_id, "queued": queued}


@app.get("/nodes/{node_id}/commands/{command_id}")
async def command_status(node_id: str, command_id: str, op: Operator = Depends(require(Role.TENANT_ADMIN))) -> dict[str, Any]:
    """查询一条指令的执行与回执状态（本租户下发的）。"""
    async with SessionLocal() as s:
        q = select(NodeCommand).where(
            NodeCommand.node_id == node_id, NodeCommand.command_id == command_id, NodeCommand.tenant_id == op.tenant
        )
        cmd = (await s.execute(q)).scalars().first()
    if cmd is None:
        raise HTTPException(404)
    return _command_view(cmd)


class BindingIn(BaseModel):
    """Agent 与节点组的绑定。"""

    node_group: str = Field(min_length=1, max_length=64)
    weight: int = Field(default=100, ge=1, le=100)


@app.put("/agents/{agent_id}/bindings")
async def bind(agent_id: str, body: list[BindingIn], op: Operator = Depends(require(Role.TENANT_ADMIN))) -> dict[str, Any]:
    """覆盖设置 Agent 的节点组绑定。"""
    weights = {binding.node_group: binding.weight for binding in body}
    if len(weights) != len(body):
        raise HTTPException(422, "duplicate node groups")
    async with SessionLocal() as session:
        await _lock_node_groups(session, list(weights))
        await _upsert_node_state(
            session,
            NodeBinding,
            {"tenant_id": op.tenant, "agent_id": agent_id, "weights": weights},
            {"weights": weights},
            ["tenant_id", "agent_id"],
        )
        _audit(session, op, "agent.bindings", agent_id, weights)
        await session.commit()
    return weights


@app.get("/internal/routing")
async def service_routing_table(op: Operator = Depends(routing_auth.routing_operator)) -> dict[str, Any]:
    return await routing_table(op)


@app.get("/routing")
async def routing_table(op: Operator = Depends(require(Role.TENANT_ADMIN))) -> dict[str, Any]:
    """路由表：供 APISIX 路由插件周期拉取并缓存；心跳超时的节点自动剔除。"""
    now = time.time()
    groups: dict[str, list[str]] = {}
    async with SessionLocal() as session:
        cutoff = datetime.now(UTC) - timedelta(seconds=HEARTBEAT_TIMEOUT_S)
        for node in (
            await session.execute(select(Node).where(Node.last_seen >= cutoff, Node.ready.is_(True)).order_by(Node.node_id))
        ).scalars():
            for label in node.labels:
                groups.setdefault(label, []).append(node.node_id)
        bindings = {
            binding.agent_id: binding.weights
            for binding in (await session.execute(select(NodeBinding).where(NodeBinding.tenant_id == op.tenant))).scalars()
        }
    return {"tenant_id": op.tenant, "groups": groups, "bindings": bindings, "default_group": "general", "generated_at": now}


# ---- Skill 商店（S9）----
class SkillRef(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SkillPushIn(BaseModel):
    """推送已上架 Skill 到节点：显式节点列表，或按节点组标签选健康节点（都不给则全部健康节点）。"""

    skills: list[SkillRef] = Field(min_length=1, max_length=50)
    nodes: list[str] = Field(default_factory=list, max_length=500)
    label: str | None = None


@app.post("/skills/push", status_code=202)
async def push_skills(body: SkillPushIn, op: Operator = Depends(require(Role.TENANT_ADMIN))) -> dict[str, Any]:
    """为目标节点生成 reload_skills 指令（节点拉取后下载 → 验签 → 原子物化 → 回执）。"""
    async with SessionLocal() as session:
        cutoff = datetime.now(UTC) - timedelta(seconds=HEARTBEAT_TIMEOUT_S)
        nodes = (await session.execute(select(Node).where(Node.last_seen >= cutoff, Node.ready.is_(True)))).scalars()
        targets = list(dict.fromkeys(body.nodes)) or [node.node_id for node in nodes if body.label is None or body.label in node.labels]
    if not targets:
        raise HTTPException(409, "no target nodes (none registered / healthy for the given label)")
    cmd_in = CommandIn(type="reload_skills", args={"skills": [r.model_dump() for r in body.skills]})
    async with SessionLocal() as s:
        cmds = [await _enqueue(s, op, node_id, cmd_in) for node_id in sorted(targets)]
        _audit(s, op, "skill.push", ",".join(f"{r.name}@{r.version}" for r in body.skills)[:128], {"nodes": sorted(targets)})
        await s.commit()
    return {"commands": [{"node_id": c.node_id, "id": c.command_id} for c in cmds]}


class SkillEvalIn(BaseModel):
    """评测回放：cases 为空时取包内 evals/cases.jsonl。"""

    cases: list[dict[str, Any]] | None = Field(default=None, max_length=500)


@app.post("/skills/{name}/versions/{version}/eval")
async def eval_skill(name: str, version: str, body: SkillEvalIn, op: Operator = Depends(require(Role.TENANT_ADMIN))) -> dict[str, Any]:
    """编排一次 Skill 评测回放（见 console_api.skill_eval），结果经网关回写。"""
    try:
        summary = await skill_eval.run(name, version, tenant=op.tenant, cases=body.cases)
    except skill_eval.SkillEvalError as e:
        raise HTTPException(e.status, str(e)) from e
    async with SessionLocal() as s:
        detail = {k: summary[k] for k in ("run_ref", "pass_rate", "total", "threshold", "gate_passed")}
        _audit(s, op, "skill.eval", f"{name}@{version}", detail)
        await s.commit()
    return summary


def _user_token(authorization: str | None, x_ai_user_token: str | None) -> str | None:
    if x_ai_user_token:
        return x_ai_user_token
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip() or None
    return None


async def _gateway_proxy(
    method: str,
    path: str,
    op: Operator,
    token: str | None,
    *,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    audit: tuple[str, str] | None = None,
    admin: bool = False,
) -> Any:
    """转发到能力网关（用户令牌放 X-AI-User-Token / 开发态 Bearer），网关错误状态码与 detail 原样返回。"""
    try:
        r = await gateway_client.request(method, path, user_token=token, json=json, params=params, admin=admin)
    except gateway_client.GatewayError as e:
        raise HTTPException(502, str(e)) from e
    try:
        data = r.json() if r.content else {}
    except ValueError:
        data = {"detail": r.text[:300]}
    if audit is not None:
        async with SessionLocal() as s:
            _audit(s, op, audit[0], audit[1], {"status": r.status_code} | (json or {}))
            await s.commit()
    if r.status_code >= 400:
        detail = data.get("detail") if isinstance(data, dict) else None
        raise HTTPException(r.status_code, str(detail or f"gateway returned HTTP {r.status_code}"))
    return data


@app.get("/skills")
async def list_skills(
    status: str | None = None,
    name: str | None = None,
    op: Operator = Depends(require(Role.USER)),
    authorization: str | None = Header(default=None),
    x_ai_user_token: str | None = Header(default=None),
) -> Any:
    """Skill 版本列表（审核页；转发网关 ``GET /skills?status=&name=``）。"""
    params = {k: v for k, v in (("status", status), ("name", name)) if v}
    return await _gateway_proxy("GET", "/skills", op, _user_token(authorization, x_ai_user_token), params=params)


@app.get("/skills/{name}/versions/{version}")
async def get_skill_version(
    name: str,
    version: str,
    op: Operator = Depends(require(Role.USER)),
    authorization: str | None = Header(default=None),
    x_ai_user_token: str | None = Header(default=None),
) -> Any:
    """Skill 版本详情（清单、扫描报告、评测结果；转发网关）。"""
    return await _gateway_proxy("GET", f"/skills/{name}/versions/{version}", op, _user_token(authorization, x_ai_user_token))


@app.post("/skills/{name}/versions/{version}/approve")
async def approve_skill(
    name: str,
    version: str,
    op: Operator = Depends(require(Role.USER)),
    authorization: str | None = Header(default=None),
    x_ai_user_token: str | None = Header(default=None),
) -> Any:
    """审批代理：携带审批人用户令牌转发到能力网关（Cedar approve_skill + 禁自批在网关判定），网关状态码原样返回。"""
    token = _user_token(authorization, x_ai_user_token)
    if not token:
        raise HTTPException(401, "approver user token required", headers={"WWW-Authenticate": "Bearer"})
    path = f"/skills/{name}/versions/{version}/approve"
    return await _gateway_proxy("POST", path, op, token, audit=("skill.approve", f"{name}@{version}"))


class RevokeIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


@app.post("/skills/{name}/versions/{version}/revoke")
async def revoke_skill(
    name: str,
    version: str,
    body: RevokeIn,
    op: Operator = Depends(require(Role.TENANT_ADMIN)),
    authorization: str | None = Header(default=None),
    x_ai_user_token: str | None = Header(default=None),
) -> Any:
    """吊销代理：转发网关 ``POST /skills/{n}/versions/{v}/revoke``（网关要求管理令牌：Console 以 CONSOLE_GATEWAY_ADMIN_TOKEN
    代为携带，操作者须为租户管理员）；节点随心跳拉取吊销列表生效。"""
    token = _user_token(authorization, x_ai_user_token)
    if not token:
        raise HTTPException(401, "user token required", headers={"WWW-Authenticate": "Bearer"})
    path = f"/skills/{name}/versions/{version}/revoke"
    info = await _gateway_proxy("GET", f"/skills/{name}/versions/{version}", op, token)
    if not isinstance(info, dict) or info.get("tenant") != op.tenant:
        raise HTTPException(404, "unknown skill version")
    audit = ("skill.revoke", f"{name}@{version}")
    return await _gateway_proxy("POST", path, op, token, json=body.model_dump(), audit=audit, admin=True)


@app.get("/audit")
async def audit(limit: int = 100, op: Operator = Depends(require(Role.ADMIN))) -> list[dict[str, Any]]:
    """查询本租户最近的审计记录（按时间正序，最新一条在最后）。"""
    async with SessionLocal() as s:
        q = select(Audit).where(Audit.tenant_id == op.tenant).order_by(Audit.id.desc()).limit(max(1, min(limit, 1000)))
        rows = list((await s.execute(q)).scalars())
    return [
        {"ts": a.ts.isoformat(), "actor": a.actor, "tenant": a.tenant_id, "action": a.action, "object": a.object, "detail": a.detail}
        for a in reversed(rows)
    ]


@app.get("/audit/search")
async def search_audit(
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    before_id: Annotated[int | None, Query(gt=0, le=2**63 - 1)] = None,
    actor: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    action: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    object: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    since: datetime | None = None,
    until: datetime | None = None,
    op: Operator = Depends(require(Role.ADMIN)),
) -> dict[str, Any]:
    if any(stamp is not None and stamp.tzinfo is None for stamp in (since, until)):
        raise HTTPException(422, "audit timestamps require an explicit timezone")
    if since is not None and until is not None and since > until:
        raise HTTPException(422, "since must not be after until")
    query = select(Audit).where(Audit.tenant_id == op.tenant)
    if before_id is not None:
        query = query.where(Audit.id < before_id)
    for column, value in ((Audit.actor, actor), (Audit.action, action), (Audit.object, object)):
        if value is not None:
            query = query.where(column == value)
    if since is not None:
        query = query.where(Audit.ts >= since)
    if until is not None:
        query = query.where(Audit.ts <= until)
    async with SessionLocal() as session:
        rows = list((await session.execute(query.order_by(Audit.id.desc()).limit(limit + 1))).scalars())
    return {
        "items": [
            {
                "id": row.id,
                "ts": row.ts.isoformat(),
                "actor": row.actor,
                "tenant": row.tenant_id,
                "action": row.action,
                "object": row.object,
                "detail": row.detail,
            }
            for row in rows[:limit]
        ],
        "next_before_id": rows[limit - 1].id if len(rows) > limit else None,
        "order": "newest_first",
    }


@app.get("/metrics/operations")
async def operation_metrics(op: Operator = Depends(require(Role.TENANT_ADMIN))) -> dict[str, Any]:
    async with SessionLocal() as session:
        agents = await session.scalar(select(func.count(Agent.id)).where(Agent.tenant_id == op.tenant))
        versions = dict(
            (
                await session.execute(
                    select(AgentVersion.status, func.count(AgentVersion.id))
                    .where(
                        AgentVersion.tenant_id == op.tenant,
                    )
                    .group_by(AgentVersion.status)
                )
            ).all()
        )
        assignments = await session.scalar(select(func.count(UserAgent.id)).where(UserAgent.tenant_id == op.tenant))
        principals = await session.scalar(select(func.count(func.distinct(UserAgent.user))).where(UserAgent.tenant_id == op.tenant))
        bindings = await session.scalar(select(func.count(NodeBinding.id)).where(NodeBinding.tenant_id == op.tenant))
        commands = dict(
            (
                await session.execute(
                    select(NodeCommand.status, func.count(NodeCommand.id))
                    .where(
                        NodeCommand.tenant_id == op.tenant,
                    )
                    .group_by(NodeCommand.status)
                )
            ).all()
        )
        audits = await session.scalar(select(func.count(Audit.id)).where(Audit.tenant_id == op.tenant))
        prereleases = await session.scalar(
            select(func.count(AgentVersion.id)).where(
                AgentVersion.tenant_id == op.tenant,
                AgentVersion.status == "published",
                AgentVersion.canary_percent == 0,
            )
        )
    return {
        "tenant_id": op.tenant,
        "scope": "console_control_plane",
        "generated_at": datetime.now(UTC).isoformat(),
        "agents": agents,
        "versions": versions,
        "pre_release_versions": prereleases,
        "assignment_relations": assignments,
        "assigned_principals": principals,
        "agent_binding_records": bindings,
        "commands": commands,
        "audit_events": audits,
        "runtime_execution": {"available": False, "reason": "not_collected_by_console"},
    }


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """健康检查。"""
    return {"status": "ok"}

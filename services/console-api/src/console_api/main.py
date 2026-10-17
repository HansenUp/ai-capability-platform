"""Agent Console 后端（Console API，默认端口 8081）。

独立服务、独立库表，承担运营侧控制面：
- Agent 生命周期：草稿 → 发布（评测闸门 + 灰度）→ 放量 / 回滚 → 可见性；发布、灰度、回滚都先同步到 Runtime 的
  /admin 接口，Runtime 成功后才改 Console 侧状态（失败返回 502）；
- 节点控制面：Runtime 节点注册、心跳、指令下发（reload_config / reload_skills / graceful_restart / switch_model）；
- 路由表：Agent 与节点组的绑定权重，供 APISIX 路由插件拉取（ADR-0003）；
- 审计：所有变更操作记录操作者、租户、动作与对象。
Agent、版本、可见性、归属与审计存 Console 库（S3.3，console_api.db）；节点、绑定与指令队列是易失状态，在进程内存。
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from console_api import runtime_client
from console_api.auth import Operator, Role, assert_assigned, require
from console_api.db import Agent, AgentVersion, Audit, SessionLocal, UserAgent, Visibility, init_db


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时把 Console 库升级到最新迁移版本。"""
    await init_db()
    yield


app = FastAPI(title="AI Capability Platform · Console API", version="0.1.0", lifespan=lifespan)
HEARTBEAT_TIMEOUT_S = int(os.environ.get("HEARTBEAT_TIMEOUT_S", "90"))  # 超过该秒数未心跳的节点从路由表剔除

NODES: dict[str, dict[str, Any]] = {}  # node_id -> 节点信息与最近心跳
BINDINGS: dict[str, dict[str, int]] = {}  # agent_id -> {节点组: 权重}
COMMANDS: dict[str, list[dict[str, Any]]] = {}  # node_id -> 待节点拉取的指令队列

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


@app.post("/agents/{agent_id}/versions/{version}/publish")
async def publish(agent_id: str, version: str, body: PublishIn, op: Operator = Depends(require(Role.ADMIN))) -> dict[str, Any]:
    """发布版本：评测闸门 → 推送快照并发布到 Runtime（带灰度参数）→ Console 侧标记 published。"""
    async with SessionLocal() as s:
        await assert_assigned(s, op, agent_id)
        v = await _version(s, op, agent_id, version)
        if body.eval_passed is not True:
            raise HTTPException(409, "publish gate: evaluation must pass (eval_passed=true with eval_run_ref)")  # 业务规则拒绝统一用 409
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
        _audit(s, op, "agent.publish", f"{agent_id}@{version}", {"eval_run_ref": body.eval_run_ref, "canary": body.canary_percent})
        await s.commit()
        return _view(v)


class CanaryIn(BaseModel):
    """调整灰度：放量比例与白名单。"""

    percent: int = Field(ge=0, le=100)
    allowlist: list[Subject] = Field(default_factory=list, max_length=1000)


@app.put("/agents/{agent_id}/versions/{version}/canary")
async def set_canary(agent_id: str, version: str, body: CanaryIn, op: Operator = Depends(require(Role.ADMIN))) -> dict[str, Any]:
    """灰度放量：调整已发布版本的比例与白名单（放量到 100 即成为稳定版）。"""
    async with SessionLocal() as s:
        await assert_assigned(s, op, agent_id)
        v = await _version(s, op, agent_id, version)
        if v.status != "published":
            raise HTTPException(409, f"version status is {v.status}")
        await _sync("PUT", f"/admin/agents/{agent_id}/versions/{version}/rollout", op, body.model_dump())
        before = v.canary_percent
        v.canary_percent, v.canary_allowlist = body.percent, list(body.allowlist)
        _audit(s, op, "agent.canary", f"{agent_id}@{version}", {"from": before, "to": body.percent})
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
        return [_view(v) for v in (await s.execute(q)).scalars()]


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

    node_id: str
    topology: str = "central"  # central / hybrid / standalone
    labels: list[str] = Field(default_factory=lambda: ["general"])  # 节点组标签，用于路由
    version: str = "0"  # 节点软件版本


@app.post("/nodes/register")
async def node_register(body: NodeRegister) -> dict[str, Any]:
    """节点启动时注册，返回心跳间隔。"""
    NODES[body.node_id] = body.model_dump() | {"last_seen": time.time(), "load": 0}
    COMMANDS.setdefault(body.node_id, [])
    return {"ok": True, "heartbeat_interval_s": 30}


class Heartbeat(BaseModel):
    """节点心跳负载。"""

    load: float = 0  # 负载（0–1）
    qps: float = 0
    version: str = "0"


@app.post("/nodes/{node_id}/heartbeat")
async def heartbeat(node_id: str, body: Heartbeat) -> dict[str, Any]:
    """更新节点最近心跳与负载；未注册返回 404。"""
    if node_id not in NODES:
        raise HTTPException(404, "register first")
    NODES[node_id].update(last_seen=time.time(), load=body.load, qps=body.qps, version=body.version)
    return {"ok": True}


@app.get("/nodes/{node_id}/commands")
async def pull_commands(node_id: str) -> list[dict[str, Any]]:
    """节点拉取待执行指令（拉取即清空队列）。"""
    cmds, COMMANDS[node_id] = COMMANDS.get(node_id, []), []
    return cmds


class CommandIn(BaseModel):
    """下发给节点的指令。"""

    type: str  # reload_config | reload_skills | graceful_restart | switch_model
    args: dict[str, Any] = Field(default_factory=dict)


@app.post("/nodes/{node_id}/commands", status_code=202)
async def push_command(node_id: str, body: CommandIn, op: Operator = Depends(require(Role.TENANT_ADMIN))) -> dict[str, Any]:
    """租户管理员向节点下发指令，进入队列等待节点拉取。"""
    COMMANDS.setdefault(node_id, []).append(body.model_dump())
    async with SessionLocal() as s:
        _audit(s, op, "node.command", node_id, body.model_dump())
        await s.commit()
    return {"queued": len(COMMANDS[node_id])}


class BindingIn(BaseModel):
    """Agent 与节点组的绑定。"""

    node_group: str
    weight: int = 100  # 权重，多个节点组时按权重分流


@app.put("/agents/{agent_id}/bindings")
async def bind(agent_id: str, body: list[BindingIn], op: Operator = Depends(require(Role.TENANT_ADMIN))) -> dict[str, Any]:
    """覆盖设置 Agent 的节点组绑定。"""
    BINDINGS[agent_id] = {b.node_group: b.weight for b in body}
    return BINDINGS[agent_id]


@app.get("/routing")
async def routing_table() -> dict[str, Any]:
    """路由表：供 APISIX 路由插件周期拉取并缓存；心跳超时的节点自动剔除。"""
    now = time.time()
    healthy = [n for n in NODES.values() if now - n["last_seen"] <= HEARTBEAT_TIMEOUT_S]
    groups: dict[str, list[str]] = {}
    for n in healthy:
        for label in n["labels"]:
            groups.setdefault(label, []).append(n["node_id"])
    return {"groups": groups, "bindings": BINDINGS, "default_group": "general", "generated_at": now}


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


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """健康检查。"""
    return {"status": "ok"}

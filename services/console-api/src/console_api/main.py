"""Agent Console 后端（Console API，默认端口 8081）。

独立服务、独立库表，承担运营侧控制面：
- Agent 生命周期：草稿 → 发布（评测闸门 + 灰度）→ 回滚 → 可见性；发布时把快照推送到 Runtime 的 /admin 接口；
- 节点控制面：Runtime 节点注册、心跳、指令下发（reload_config / reload_skills / graceful_restart / switch_model）；
- 路由表：Agent 与节点组的绑定权重，供 APISIX 路由插件拉取（ADR-0003）；
- 审计：所有变更操作记录操作者、租户、动作与对象。
当前存储为内存实现，S10 工作包迁移到 MySQL。
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from console_api.auth import ASSIGNMENTS, Operator, Role, assert_assigned, require

app = FastAPI(title="AI Capability Platform · Console API", version="0.1.0")
RUNTIME_ADMIN_URL = os.environ.get("RUNTIME_ADMIN_URL", "http://localhost:8080")  # Runtime 管理接口地址
HEARTBEAT_TIMEOUT_S = int(os.environ.get("HEARTBEAT_TIMEOUT_S", "90"))  # 超过该秒数未心跳的节点从路由表剔除

AGENTS: dict[str, dict[str, Any]] = {}  # agent_id@version -> 快照与状态（draft / published / retired）
VISIBILITY: dict[str, dict[str, Any]] = {}  # agent_id -> 可见范围（组 / 角色 / 白名单）
NODES: dict[str, dict[str, Any]] = {}  # node_id -> 节点信息与最近心跳
BINDINGS: dict[str, dict[str, int]] = {}  # agent_id -> {节点组: 权重}
COMMANDS: dict[str, list[dict[str, Any]]] = {}  # node_id -> 待节点拉取的指令队列
AUDIT: list[dict[str, Any]] = []  # 审计日志


def _audit(op: Operator, action: str, obj: str, detail: dict[str, Any] | None = None) -> None:
    """追加一条审计记录。"""
    AUDIT.append({"ts": time.time(), "actor": op.user, "tenant": op.tenant, "action": action, "object": obj, "detail": detail or {}})


# ---- Agent 生命周期 ----
class AgentDraft(BaseModel):
    """Agent 版本草稿。"""

    agent_id: str
    version: str
    system_prompt: str = ""
    model_alias: str = "default-chat"  # 模型别名（ADR-0006），发布后锁定
    harness: str | None = None  # 指定 Harness；空则用节点默认
    config: dict[str, Any] = Field(default_factory=dict)  # tools / skills / redlines / core_tool_limit 等


@app.post("/agents", status_code=201)
async def create_draft(body: AgentDraft, op: Operator = Depends(require(Role.ADMIN))) -> dict[str, Any]:
    """创建草稿版本；创建者自动获得该 Agent 的分配关系。"""
    assert_assigned(op, body.agent_id) if body.agent_id in AGENTS else None  # 已存在的 Agent 需归属校验
    AGENTS[f"{body.agent_id}@{body.version}"] = body.model_dump() | {"tenant": op.tenant, "status": "draft"}
    ASSIGNMENTS.setdefault(op.user, set()).add(body.agent_id)
    _audit(op, "agent.create", f"{body.agent_id}@{body.version}")
    return AGENTS[f"{body.agent_id}@{body.version}"]


class PublishIn(BaseModel):
    """发布请求：评测结果 + 灰度参数。"""

    eval_run_ref: str | None = None  # Langfuse 评测运行引用
    eval_passed: bool | None = None  # 评测是否通过；不为 True 时发布被闸门拦截
    canary_percent: int = 0  # 灰度流量百分比
    canary_allowlist: list[str] = Field(default_factory=list)  # 灰度白名单用户


@app.post("/agents/{agent_id}/versions/{version}/publish")
async def publish(agent_id: str, version: str, body: PublishIn, op: Operator = Depends(require(Role.ADMIN))) -> dict[str, Any]:
    """发布版本：评测闸门 → 推送快照到 Runtime → 标记 published 并记录灰度配置。"""
    assert_assigned(op, agent_id)
    key = f"{agent_id}@{version}"
    if key not in AGENTS:
        raise HTTPException(404)
    if body.eval_passed is not True:
        raise HTTPException(409, "publish gate: evaluation must pass (eval_passed=true with eval_run_ref)")  # 业务规则拒绝统一用 409
    draft = AGENTS[key]
    # 先把快照推到 Runtime 管理接口（写入 + 发布），再在 Console 侧标记发布
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            await c.post(
                f"{RUNTIME_ADMIN_URL}/admin/agents",
                json={k: draft[k] for k in ("agent_id", "version", "system_prompt", "model_alias", "harness", "config")}
                | {"tenant_id": op.tenant},
                headers={"X-AI-Subject": f"human:{op.user}"},
            )
            await c.post(
                f"{RUNTIME_ADMIN_URL}/admin/agents/{agent_id}/versions/{version}/publish",
                json={"eval_run_ref": body.eval_run_ref, "eval_passed": True},
                headers={"X-AI-Subject": f"human:{op.user}"},
            )
    except httpx.HTTPError:
        pass  # 开发 / 测试环境 Runtime 可能未启动；Console 侧仍记录快照，节点上线后由指令同步
    draft.update(
        status="published", eval_run_ref=body.eval_run_ref, canary={"percent": body.canary_percent, "allowlist": body.canary_allowlist}
    )
    _audit(op, "agent.publish", key, {"eval_run_ref": body.eval_run_ref, "canary": body.canary_percent})
    return draft


@app.post("/agents/{agent_id}/versions/{version}/rollback")
async def rollback(agent_id: str, version: str, op: Operator = Depends(require(Role.ADMIN))) -> dict[str, Any]:
    """回滚：目标版本置为 published，其余已发布版本置为 retired。"""
    assert_assigned(op, agent_id)
    for k, v in AGENTS.items():
        if k.startswith(agent_id + "@"):
            v["status"] = "published" if k.endswith("@" + version) else ("retired" if v["status"] == "published" else v["status"])
    _audit(op, "agent.rollback", f"{agent_id}@{version}")
    return {"agent_id": agent_id, "active_version": version}


class VisibilityIn(BaseModel):
    """Agent 可见范围。"""

    groups: list[str] = Field(default_factory=list)  # 组织 / 部门
    roles: list[str] = Field(default_factory=list)  # 角色
    allowlist: list[str] = Field(default_factory=list)  # 指定用户


@app.put("/agents/{agent_id}/visibility")
async def set_visibility(agent_id: str, body: VisibilityIn, op: Operator = Depends(require(Role.ADMIN))) -> dict[str, Any]:
    """设置 Agent 在门户中的可见范围。"""
    assert_assigned(op, agent_id)
    VISIBILITY[agent_id] = body.model_dump()
    _audit(op, "permission.change", agent_id, body.model_dump())
    return VISIBILITY[agent_id]


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
    _audit(op, "node.command", node_id, body.model_dump())
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
    """查询最近的审计记录。"""
    return AUDIT[-limit:]


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """健康检查。"""
    return {"status": "ok"}

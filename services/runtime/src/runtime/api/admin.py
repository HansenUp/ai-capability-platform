"""Engine management API: agent version snapshots, node status. Called by the Console backend
with an operator identity; every write is audited.
（引擎管理 API：Agent 版本快照的创建/发布/查询、节点状态查询。由 Console 后端以运维身份调用；
所有写操作都会记入 Audit 审计表。）
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from ai_contract import RiskLevel, capability_tier
from runtime.api.auth import Principal, current_principal
from runtime.api.views import ErrorDetail
from runtime.assemble.assembler import LOCAL_TOOLS, load_skills, skills_digest
from runtime.config import settings
from runtime.harness import registry
from runtime.state.db import AgentVersion, Audit, SessionLocal

router = APIRouter(prefix="/admin")


class AgentVersionIn(BaseModel):
    """创建/更新一个 Agent 版本快照的请求体。"""

    # 字符串长度上限与 t_agent_version 的列宽一致：MySQL 严格模式下超长会在 INSERT 时报错（SQLite 不会），
    # 在请求体上约束后统一返回 422，而不是 500。
    agent_id: str = Field(min_length=1, max_length=64)  # Agent 标识
    version: str = Field(min_length=1, max_length=32)  # 版本号
    tenant_id: str = Field(default="default", min_length=1, max_length=64)  # 租户标识
    model_alias: str = Field(default="default-chat", min_length=1, max_length=64)  # 绑定的模型别名
    harness: str | None = Field(default=None, max_length=32)  # 指定 harness，缺省时使用节点默认 harness
    system_prompt: str = ""  # 系统提示词
    config: dict[str, Any] = Field(default_factory=dict)  # tools, skills, budgets, redlines（工具/技能/预算/红线等配置）


@router.post("/agents", status_code=201, responses={409: {"model": ErrorDetail}})
async def upsert_agent_version(body: AgentVersionIn, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """创建或更新一个草稿态 Agent 版本；已发布（published）版本不可变，需要新建版本。"""
    async with SessionLocal() as s:
        existing = (
            (await s.execute(select(AgentVersion).where(AgentVersion.agent_id == body.agent_id, AgentVersion.version == body.version)))
            .scalars()
            .first()
        )
        if existing and existing.status == "published":
            raise HTTPException(409, "published versions are immutable; create a new version")
        av = existing or AgentVersion(agent_id=body.agent_id, version=body.version)
        av.tenant_id, av.model_alias, av.harness, av.system_prompt, av.config = (
            body.tenant_id,
            body.model_alias,
            body.harness,
            body.system_prompt,
            body.config,
        )
        s.add(av)
        s.add(Audit(actor=principal.subject, action="agent.create", object=f"{body.agent_id}@{body.version}", result="ok"))
        await s.commit()
    return {"agent_id": body.agent_id, "version": body.version, "status": "draft"}


class PublishIn(BaseModel):
    """发布一个 Agent 版本的请求体，携带评测网关的评测结果引用。"""

    eval_run_ref: str | None = None  # 评测运行引用
    eval_passed: bool | None = None  # 评测是否通过


@router.post("/agents/{agent_id}/versions/{version}/publish", responses={404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}})
async def publish(agent_id: str, version: str, body: PublishIn, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """将草稿版本置为已发布状态，并冻结可复现快照（技能摘要、harness 版本）。

    两道闸门依次生效，任一不满足都按业务规则拒绝返回 409（422 只保留给请求参数校验）：
    1) 评测闸门——eval_passed=False 直接阻断；
    2) 能力等级闸门——声明了高风险写工具的 Agent，其所选 harness 的 capability_tier 必须达到 L2，
       否则平台既无法在执行前强制拦截，也无法在审批挂起后精确续跑，高风险写就失去了治理保障。
    """
    async with SessionLocal() as s:
        av = (
            (await s.execute(select(AgentVersion).where(AgentVersion.agent_id == agent_id, AgentVersion.version == version)))
            .scalars()
            .first()
        )
        if av is None:
            raise HTTPException(404)
        if body.eval_passed is False:
            raise HTTPException(409, "evaluation gate failed; publish blocked")
        caps = registry.get(av.harness).capabilities()
        tier = capability_tier(caps)
        cfg = av.config or {}
        # 高风险写工具从本地工具注册表查风险等级；生产环境应改为向能力网关查询。
        high_risk = [n for n in cfg.get("tools", []) if n in LOCAL_TOOLS and LOCAL_TOOLS[n].risk_level == RiskLevel.WRITE_HIGH]
        if high_risk and tier != "L2":
            raise HTTPException(
                409,
                f"所选 Harness 不满足高风险写所需的能力等级（harness={caps.name}，当前 {tier}，需要 L2）；"
                f"涉及的高风险写工具：{', '.join(high_risk)}",
            )
        av.status, av.eval_run_ref, av.eval_passed = "published", body.eval_run_ref, body.eval_passed
        # 冻结发布快照：技能内容摘要与 harness 版本，供事后核对"同一版本是否仍是同一套行为"。
        av.skills_digest = skills_digest(load_skills(cfg.get("skills", [])))
        av.harness_version = caps.version
        s.add(
            Audit(
                actor=principal.subject,
                action="agent.publish",
                object=f"{agent_id}@{version}",
                result="ok",
                detail={
                    "eval_run_ref": body.eval_run_ref,
                    "skills_digest": av.skills_digest,
                    "harness_version": av.harness_version,
                    "capability_tier": tier,
                },
            )
        )
        await s.commit()
    return {"agent_id": agent_id, "version": version, "status": "published"}


@router.get("/agents")
async def list_agents() -> list[dict[str, Any]]:
    """列出所有已登记的 Agent 版本快照概要信息。"""
    async with SessionLocal() as s:
        rows = (await s.execute(select(AgentVersion).order_by(AgentVersion.id))).scalars()
        return [
            {"agent_id": r.agent_id, "version": r.version, "status": r.status, "model_alias": r.model_alias, "harness": r.harness}
            for r in rows
        ]


@router.get("/node")
async def node_status() -> dict[str, Any]:
    """返回当前节点的基本状态：节点标识、拓扑、默认 harness、已加载的 harness 列表。"""
    return {
        "node_id": settings.node_id,
        "topology": settings.topology,
        "default_harness": settings.harness,
        "harnesses_loaded": registry.names(),
    }

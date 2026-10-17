"""Engine management API: agent version snapshots, node status. Called by the Console backend
with an operator identity; every write is audited.
（引擎管理 API：Agent 版本快照的创建/发布/查询、节点状态查询。由 Console 后端以运维身份调用；
所有写操作都会记入 Audit 审计表。）
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_contract import RiskLevel, capability_tier
from runtime import llm
from runtime.api.auth import Principal, require_admin
from runtime.api.views import ErrorDetail
from runtime.assemble.assembler import LOCAL_TOOLS, load_skills, skills_digest
from runtime.config import settings
from runtime.guard.hooks import validate_hooks_config
from runtime.harness import registry
from runtime.observe import audit
from runtime.state.db import AgentVersion, SessionLocal

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])  # 全部管理接口要求鉴权与管理员角色


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
async def upsert_agent_version(body: AgentVersionIn, principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    """创建或更新一个草稿态 Agent 版本。只能覆盖同一租户的草稿：已发布 / 已退役的版本不可变，需要新建版本；
    同名版本属于其他租户时 409（Runtime 的版本键是全局的 (agent_id, version)，不能被别的租户改写归属）。"""
    async with SessionLocal() as s:
        existing = (
            (await s.execute(select(AgentVersion).where(AgentVersion.agent_id == body.agent_id, AgentVersion.version == body.version)))
            .scalars()
            .first()
        )
        if existing and existing.tenant_id != body.tenant_id:
            raise HTTPException(409, "agent version belongs to another tenant")
        if existing and existing.status != "draft":
            raise HTTPException(409, f"{existing.status} versions are immutable; create a new version")
        av = existing or AgentVersion(agent_id=body.agent_id, version=body.version)
        av.tenant_id, av.model_alias, av.harness, av.system_prompt, av.config = (
            body.tenant_id,
            body.model_alias,
            body.harness,
            body.system_prompt,
            body.config,
        )
        s.add(av)
        row = audit.entry(principal, "agent.create", f"agent:{body.agent_id}@{body.version}", "ok", tenant_id=body.tenant_id)
        s.add(row)
        await s.commit()
    audit.mirror(row)
    return {"agent_id": body.agent_id, "version": body.version, "status": "draft"}


class PublishIn(BaseModel):
    """发布一个 Agent 版本的请求体，携带评测网关的评测结果引用。"""

    eval_run_ref: str | None = None  # 评测运行引用
    eval_passed: bool | None = None  # 评测是否通过
    # 灰度（S3.2）：100 为全量发布；小于 100 时作为灰度版，只有白名单与哈希桶落入比例内的主体使用它
    rollout_percent: int = Field(default=100, ge=0, le=100)
    rollout_allowlist: list[Annotated[str, Field(max_length=128)]] = Field(default_factory=list, max_length=1000)


# 发布 / 放量 / 回滚的可选租户校验（Console 调用时带上调用方租户，版本属于其他租户时按不存在处理）
TenantQuery = Annotated[str | None, Query(max_length=64)]


class RolloutIn(BaseModel):
    """调整已发布版本的灰度：放量比例与白名单。"""

    percent: int = Field(ge=0, le=100)
    allowlist: list[Annotated[str, Field(max_length=128)]] = Field(default_factory=list, max_length=1000)


@router.post("/agents/{agent_id}/versions/{version}/publish", responses={404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}})
async def publish(
    agent_id: str, version: str, body: PublishIn, tenant_id: TenantQuery = None, principal: Principal = Depends(require_admin)
) -> dict[str, Any]:
    """将草稿版本置为已发布状态，并冻结可复现快照（技能摘要、harness 版本）。

    以下闸门依次生效，任一不满足都按业务规则拒绝返回 409（422 只保留给请求参数校验）：
    1) 评测闸门——eval_passed=False 直接阻断；
    2) Hooks 配置闸门（S5，D73）——config.hooks 只能引用平台内置 hook，点与参数须合规；
    3) 能力等级闸门——声明了高风险写工具的 Agent，其所选 harness 的 capability_tier 必须达到 L2，
       否则平台既无法在执行前强制拦截，也无法在审批挂起后精确续跑，高风险写就失去了治理保障；
    4) harness 持有循环的高风险写白名单（S5 §4-1，D79）——loop_owner=harness 的适配器即便达到 L2，
       声明高风险写工具的 Agent 也须在 HARNESS_LOOP_HIGH_RISK_ALLOWLIST 中（默认空 = 拒绝）；
    5) harness 版本（S6 评审）——loop_owner=harness 的适配器（goose / dsh 等）取不到二进制版本时拒绝：快照冻结的
       harness_version 是开会话时核对实际版本的依据（HARNESS_VERSION_MISMATCH），不能冻结空值。

    已发布版本重复发布是幂等的（Console 重试用）；已退役版本不能直接重新发布，应走回滚。
    """
    async with SessionLocal() as s:
        av = await _get_version(s, agent_id, version, tenant_id)
        if av.status == "retired":
            raise HTTPException(409, "retired versions cannot be republished; roll back to them instead")
        if body.rollout_percent < 100:
            await _ensure_newest(s, av)
        if body.eval_passed is False:
            raise HTTPException(409, "evaluation gate failed; publish blocked")
        cfg = av.config or {}
        hook_problems = validate_hooks_config(cfg.get("hooks"))
        if hook_problems:
            raise HTTPException(409, "hooks 配置不合规：" + "; ".join(hook_problems))
        await _check_model_alias(av)
        caps = registry.get(av.harness).capabilities()
        if caps.loop_owner == "harness" and not caps.version.strip():
            raise HTTPException(409, f"无法确定所选 Harness 的版本（harness={caps.name}），不能冻结版本快照；请检查其二进制安装")
        tier = capability_tier(caps)
        # 高风险写工具从本地工具注册表查风险等级；生产环境应改为向能力网关查询。
        high_risk = [n for n in cfg.get("tools", []) if n in LOCAL_TOOLS and LOCAL_TOOLS[n].risk_level == RiskLevel.WRITE_HIGH]
        if high_risk and tier != "L2":
            raise HTTPException(
                409,
                f"所选 Harness 不满足高风险写所需的能力等级（harness={caps.name}，当前 {tier}，需要 L2）；"
                f"涉及的高风险写工具：{', '.join(high_risk)}",
            )
        if high_risk and caps.loop_owner == "harness" and agent_id not in settings.harness_loop_high_risk_allowlist:
            raise HTTPException(
                409,
                f"所选 Harness 由自身持有模型-工具循环（harness={caps.name}，loop_owner=harness），高风险写须将 Agent "
                f"'{agent_id}' 加入 HARNESS_LOOP_HIGH_RISK_ALLOWLIST；涉及的高风险写工具：{', '.join(high_risk)}",
            )
        av.status, av.eval_run_ref, av.eval_passed = "published", body.eval_run_ref, body.eval_passed
        av.rollout_percent, av.rollout_allowlist = body.rollout_percent, list(body.rollout_allowlist)
        # 冻结发布快照：技能内容摘要与 harness 版本，供事后核对"同一版本是否仍是同一套行为"。
        av.skills_digest = skills_digest(load_skills(cfg.get("skills", [])))
        av.harness_version = caps.version
        row = audit.entry(
            principal,
            "agent.publish",
            f"agent:{agent_id}@{version}",
            "ok",
            tenant_id=av.tenant_id,
            detail={
                "eval_run_ref": body.eval_run_ref,
                "skills_digest": av.skills_digest,
                "harness_version": av.harness_version,
                "capability_tier": tier,
                "rollout_percent": body.rollout_percent,
                "rollout_allowlist_size": len(body.rollout_allowlist),
            },
        )
        s.add(row)
        await s.commit()
    audit.mirror(row)
    return {"agent_id": agent_id, "version": version, "status": "published", "rollout_percent": body.rollout_percent}


MODEL_HARNESSES = frozenset({"litellm", "claude-sdk", "goose", "dsh"})  # 经模型网关访问真实模型的 harness


async def _check_model_alias(av: AgentVersion) -> None:
    """受控模型出口（S4.8）：真实模型 harness 的版本只能绑定模型网关上已登记的别名——模型访问只能经网关，
    不能用未登记的别名绕过网关的模型清单与配额。网关不可达时同样拒绝发布（fail closed）。"""
    if not settings.publish_check_model_alias or (av.harness or settings.harness) not in MODEL_HARNESSES:
        return
    try:
        await llm.get_client().resolve_alias(av.model_alias)
    except llm.UnknownModelAlias:
        raise HTTPException(409, f"model alias '{av.model_alias}' is not registered on the model gateway") from None
    except llm.LLMError:
        raise HTTPException(409, "model gateway unavailable; cannot verify model alias") from None


async def _get_version(s: AsyncSession, agent_id: str, version: str, tenant_id: str | None = None) -> AgentVersion:
    """取版本；带 tenant_id（Console 调用时总会带）且与版本所属租户不符时同样 404。"""
    av = (await s.execute(select(AgentVersion).where(AgentVersion.agent_id == agent_id, AgentVersion.version == version))).scalars().first()
    if av is None or (tenant_id is not None and av.tenant_id != tenant_id):
        raise HTTPException(404)
    return av


async def _ensure_newest(s: AsyncSession, av: AgentVersion) -> None:
    """只有同租户最新的已发布版本可以处于灰度（比例小于 100）：给较旧的版本降比例会让更新的版本变成唯一可用版本。"""
    q = select(AgentVersion.id).where(
        AgentVersion.tenant_id == av.tenant_id,
        AgentVersion.agent_id == av.agent_id,
        AgentVersion.status == "published",
        AgentVersion.id > av.id,
    )
    if (await s.execute(q)).first() is not None:
        raise HTTPException(409, "only the newest published version can be a canary (percent < 100)")


@router.put("/agents/{agent_id}/versions/{version}/rollout", responses={404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}})
async def set_rollout(
    agent_id: str, version: str, body: RolloutIn, tenant_id: TenantQuery = None, principal: Principal = Depends(require_admin)
) -> dict[str, Any]:
    """调整已发布版本的放量比例与白名单（放量到 100 即转为稳定版）；只有同租户最新的已发布版本能降到 100 以下。"""
    async with SessionLocal() as s:
        av = await _get_version(s, agent_id, version, tenant_id)
        if av.status != "published":
            raise HTTPException(409, f"version status is {av.status}; only published versions can be rolled out")
        if body.percent < 100:
            await _ensure_newest(s, av)
        before = av.rollout_percent
        av.rollout_percent, av.rollout_allowlist = body.percent, list(body.allowlist)
        row = audit.entry(
            principal,
            "agent.rollout",
            f"agent:{agent_id}@{version}",
            "ok",
            tenant_id=av.tenant_id,
            detail={"from_percent": before, "to_percent": body.percent, "allowlist_size": len(body.allowlist)},
        )
        s.add(row)
        await s.commit()
    audit.mirror(row)
    return {"agent_id": agent_id, "version": version, "rollout_percent": body.percent}


@router.post("/agents/{agent_id}/versions/{version}/rollback", responses={404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}})
async def rollback(
    agent_id: str, version: str, tenant_id: TenantQuery = None, principal: Principal = Depends(require_admin)
) -> dict[str, Any]:
    """一键回滚：目标版本（曾发布过）置为全量稳定版，同租户该 Agent 其他已发布版本一律 retired，灰度随之结束。"""
    async with SessionLocal() as s:
        target = await _get_version(s, agent_id, version, tenant_id)
        if target.status == "draft":
            raise HTTPException(409, "cannot roll back to a draft version")
        if target.rollout_percent == 0:
            raise HTTPException(409, "cannot roll back to a pre-release (0%) version; raise its rollout instead")
        q = select(AgentVersion).where(
            AgentVersion.tenant_id == target.tenant_id, AgentVersion.agent_id == agent_id, AgentVersion.status == "published"
        )
        others = (await s.execute(q)).scalars().all()
        retired = [v.version for v in others if v.id != target.id]
        for v in others:
            if v.id != target.id:
                v.status = "retired"
        target.status, target.rollout_percent, target.rollout_allowlist = "published", 100, []
        row = audit.entry(
            principal, "agent.rollback", f"agent:{agent_id}@{version}", "ok", tenant_id=target.tenant_id, detail={"retired": retired}
        )
        s.add(row)
        await s.commit()
    audit.mirror(row)
    return {"agent_id": agent_id, "active_version": version, "retired": retired}


@router.get("/agents")
async def list_agents() -> list[dict[str, Any]]:
    """列出所有已登记的 Agent 版本快照概要信息。"""
    async with SessionLocal() as s:
        rows = (await s.execute(select(AgentVersion).order_by(AgentVersion.id))).scalars()
        return [
            {
                "agent_id": r.agent_id,
                "version": r.version,
                "status": r.status,
                "model_alias": r.model_alias,
                "harness": r.harness,
                "rollout_percent": r.rollout_percent,
            }
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

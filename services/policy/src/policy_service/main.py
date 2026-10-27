"""授权判定服务（Policy Service，默认端口 8083）。

平台"策略即代码"的判定入口，属于纵向的"身份 · 权限 · 审计"能力：
- `POLICY_ENGINE=cedar`（默认）：转发到 Cedar 引擎 sidecar（`services/policy/cedar-engine`，`CEDAR_URL`，
  默认 http://127.0.0.1:8183）评估 `cedar/policies.cedar`；引擎不可达 / 超时 / 应答异常一律 deny（`policy_unavailable`）。
- `POLICY_ENGINE=baseline`：`baseline.authorize` 用 Python 镜像同一套策略，只在显式设置时启用；
  生产（`POLICY_ENV` 或 `PLATFORM_ENV` 任一为 prod）时拒绝以 baseline 启动（D127）。
- 服务间鉴权：配置 `POLICY_SERVICE_TOKEN` 时 `/authz/check-batch`、`/authz/check`、`/authz/decisions` 须带相同的
  `X-Policy-Service-Token`（常量时间比较），否则 401；开发态可不配，生产必配（否则拒绝启动）。能力网关与 Runtime 闸口同步携带。
两者都必须通过 `cedar/tests.json` 的用例。Cedar 原生只有 allow / deny，require_approval 由两次查询合成
（approved=false 为 deny 且 approved=true 为 allow）。每次判定都会生成 decision_id 并记录，
Runtime 的工具闸口会把该 id 写入 Run 事件日志（tool.decision），实现判定可追溯。
"""

from __future__ import annotations

import hmac
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, StrictBool, ValidationError, model_validator

from policy_service.baseline import RawDecision, authorize, synthesize

Decision = Literal["allow", "deny", "require_approval"]
EngineName = Literal["cedar", "baseline"]
MAX_BATCH = 100  # /authz/check-batch 单次最多条数（每条最多两次 Cedar 查询，须 ≤ cedar-engine 的 CEDAR_MAX_BATCH）
BASELINE_VERSION = "baseline"  # baseline 引擎的 policy_version
UNAVAILABLE_VERSION = "unavailable"  # Cedar 不可达时的 policy_version


@dataclass(frozen=True)
class Settings:
    """策略服务配置（启动时从环境变量读取一次）。"""

    engine: EngineName
    cedar_url: str
    cedar_timeout_s: float
    policy_env: str
    service_token: str = ""  # 服务间共享令牌（X-Policy-Service-Token）；空表示不校验（仅开发态）


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """读取并校验配置；baseline 只能显式开启，且生产环境禁止；生产必须配置服务间令牌（抛出异常即拒绝启动）。
    生产判定：POLICY_ENV 或平台统一的 PLATFORM_ENV 任一为 prod。"""
    env = os.environ if env is None else env
    engine = (env.get("POLICY_ENGINE") or "cedar").strip().lower()
    if engine not in ("cedar", "baseline"):
        raise RuntimeError(f"POLICY_ENGINE must be 'cedar' or 'baseline', got {engine!r}")
    envs = {(env.get(k) or "").strip().lower() for k in ("POLICY_ENV", "PLATFORM_ENV")}
    policy_env = "prod" if "prod" in envs else ((env.get("POLICY_ENV") or "dev").strip().lower() or "dev")
    if engine == "baseline" and policy_env == "prod":
        raise RuntimeError(
            "POLICY_ENGINE=baseline is not allowed in prod (POLICY_ENV / PLATFORM_ENV=prod); run the Cedar engine (CEDAR_URL)"
        )
    service_token = (env.get("POLICY_SERVICE_TOKEN") or "").strip()
    if policy_env == "prod" and not service_token:
        raise RuntimeError("POLICY_SERVICE_TOKEN is required in prod (POLICY_ENV / PLATFORM_ENV=prod)")
    timeout = float(env.get("CEDAR_TIMEOUT_S") or "1.0")
    if timeout <= 0:
        raise RuntimeError("CEDAR_TIMEOUT_S must be > 0")
    cedar_url = (env.get("CEDAR_URL") or "http://127.0.0.1:8183").rstrip("/")
    return Settings(
        engine=engine,  # type: ignore[arg-type]
        cedar_url=cedar_url,
        cedar_timeout_s=timeout,
        policy_env=policy_env,
        service_token=service_token,
    )


settings = load_settings()
app = FastAPI(title="AI Capability Platform · Policy Service", version="0.2.0")
DECISION_LOG: list[dict[str, Any]] = []  # 判定记录（内存实现；生产写入审计库）
TOKEN_HEADER = "X-Policy-Service-Token"  # noqa: S105 —— 头名而非密钥


def require_service(x_policy_service_token: Annotated[str | None, Header()] = None) -> None:
    """服务间鉴权：配置了 POLICY_SERVICE_TOKEN 时调用方须带相同的 X-Policy-Service-Token（常量时间比较），否则 401。"""
    expected = settings.service_token
    if not expected:
        return
    if not x_policy_service_token or not hmac.compare_digest(x_policy_service_token.encode(), expected.encode()):
        raise HTTPException(401, "policy service token required", headers={"WWW-Authenticate": TOKEN_HEADER})


ServiceAuth = [Depends(require_service)]


# ---- 契约 1：批量判定 ----
class Principal(BaseModel):
    """调用主体：sub、tenant、roles 取自验签令牌。"""

    sub: str = Field(min_length=1, max_length=256)
    tenant: str = Field(min_length=1, max_length=128)
    roles: list[str] = Field(default_factory=list, max_length=64)


class Resource(BaseModel):
    """目标资源：能力（属性取自能力网关目录），或 approve_skill 的 Skill 版本（submitted_by 为提交人 sub）。"""

    id: str = Field(min_length=1, max_length=256)
    tenant: str = Field(min_length=1, max_length=128)
    system: str = Field(default="", max_length=128)
    risk_level: str = Field(min_length=1, max_length=64)
    data_class: str = Field(default="", max_length=64)
    submitted_by: str = Field(default="", max_length=256)


class AgentRef(BaseModel):
    """代理执行的 Agent（act）。"""

    id: str = Field(min_length=1, max_length=256)
    version: str | None = Field(default=None, max_length=64)
    tenant: str = Field(min_length=1, max_length=128)


class AuthzContext(BaseModel):
    """判定上下文：审批状态、数据污点、代理 Agent。"""

    approved: StrictBool = False
    taint: list[str] = Field(default_factory=list, max_length=64)
    agent: AgentRef | None = None


SKILL_ACTIONS = frozenset({"approve_skill"})  # 资源为 Skill 实体的动作


class AuthzRequest(BaseModel):
    """一条判定请求。"""

    principal: Principal
    action: str = Field(default="invoke", min_length=1, max_length=64)
    resource: Resource
    context: AuthzContext = Field(default_factory=AuthzContext)

    @model_validator(mode="after")
    def _skill_needs_submitter(self) -> AuthzRequest:
        """approve_skill 必须带提交人（缺失时无法判定自批，按非法请求拒绝）。"""
        if self.action in SKILL_ACTIONS and not self.resource.submitted_by:
            raise ValueError("resource.submitted_by is required for approve_skill")
        return self


class BatchRequest(BaseModel):
    """批量判定请求。"""

    requests: list[AuthzRequest] = Field(max_length=MAX_BATCH)


class BatchResult(BaseModel):
    """单条判定结果。"""

    decision: Decision
    policy_ids: list[str]
    reason: str
    decision_id: str


class BatchResponse(BaseModel):
    """批量判定结果。"""

    results: list[BatchResult]
    engine: EngineName
    policy_version: str


class PolicyUnavailableError(Exception):
    """Cedar 引擎不可达或应答异常。"""


def _context_dict(req: AuthzRequest, approved: bool) -> dict[str, Any]:
    ctx: dict[str, Any] = {"approved": approved, "taint": sorted(set(req.context.taint))}
    if req.context.agent is not None:
        ctx["agent"] = req.context.agent.model_dump(exclude_none=True)
    return ctx


def _cedar_query(req: AuthzRequest, approved: bool) -> dict[str, Any]:
    """实体映射：principal=User::"<sub>"（tenant、roles）、resource=Capability::"<id>"（approve_skill 为
    Skill::"<id>"，submitter=User::"<submitted_by>"）、context={approved,taint,agent?}。"""
    p, r = req.principal, req.resource
    if req.action in SKILL_ACTIONS:
        rtype = "Skill"
        attrs: dict[str, Any] = {
            "tenant": r.tenant,
            "risk_level": r.risk_level,
            "submitter": {"__entity": {"type": "User", "id": r.submitted_by}},
        }
    else:
        rtype = "Capability"
        attrs = {"system": r.system, "risk_level": r.risk_level, "data_class": r.data_class, "tenant": r.tenant}
    return {
        "principal": {"type": "User", "id": p.sub},
        "action": {"type": "Action", "id": req.action},
        "resource": {"type": rtype, "id": r.id},
        "context": _context_dict(req, approved),
        "entities": [
            {"uid": {"type": "User", "id": p.sub}, "attrs": {"tenant": p.tenant, "roles": sorted(set(p.roles))}, "parents": []},
            {"uid": {"type": rtype, "id": r.id}, "attrs": attrs, "parents": []},
        ],
    }


async def _cedar_authorize(queries: list[tuple[AuthzRequest, bool]]) -> tuple[list[RawDecision], str]:
    """一次往返批量调用 Cedar 引擎；任何异常都转换为 PolicyUnavailableError（调用方据此 deny）。"""
    if not queries:
        return [], UNAVAILABLE_VERSION
    body = {"requests": [_cedar_query(req, approved) for req, approved in queries]}
    try:
        async with httpx.AsyncClient(timeout=settings.cedar_timeout_s) as client:
            r = await client.post(f"{settings.cedar_url}/v1/authorize_batch", json=body)
        r.raise_for_status()
        data = r.json()
        items = data["results"]
        if not isinstance(items, list) or len(items) != len(queries):
            raise ValueError("result count mismatch")
        out: list[RawDecision] = []
        for item in items:
            errors = [str(e) for e in item.get("errors") or []]
            out.append((item["decision"] == "allow" and not errors, [str(i) for i in item.get("policy_ids") or []], errors))
        return out, str(data.get("policy_version") or UNAVAILABLE_VERSION)
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as e:
        raise PolicyUnavailableError(str(e) or type(e).__name__) from e


def _baseline_authorize(queries: list[tuple[AuthzRequest, bool]]) -> tuple[list[RawDecision], str]:
    out = [
        authorize(req.principal.model_dump(), req.action, req.resource.model_dump(), _context_dict(req, approved))
        for req, approved in queries
    ]
    return out, BASELINE_VERSION


def _record(req: dict[str, Any], decision: str, reason: str, policy_ids: list[str], version: str) -> str:
    did = "dec_" + uuid.uuid4().hex[:12]
    DECISION_LOG.append(
        {
            "id": did,
            "req": req,
            "decision": decision,
            "matched": reason,
            "policy_ids": policy_ids,
            "engine": settings.engine,
            "policy_version": version,
        }
    )
    return did


async def decide(requests: list[AuthzRequest]) -> tuple[list[tuple[str, list[str], str]], str]:
    """对一批请求判定并合成三态：未审批的请求额外查询一次 approved=true 版本（同一往返）。"""
    queries: list[tuple[AuthzRequest, bool]] = [(req, req.context.approved) for req in requests]
    second: list[int | None] = []  # 每条请求的 approved=true 二次查询下标（已审批的请求无需二次查询）
    for req in requests:
        if req.context.approved:
            second.append(None)
        else:
            second.append(len(queries))
            queries.append((req, True))
    if settings.engine == "baseline":
        raw, version = _baseline_authorize(queries)
    else:
        try:
            raw, version = await _cedar_authorize(queries)
        except PolicyUnavailableError:
            return [("deny", [], "policy_unavailable") for _ in requests], UNAVAILABLE_VERSION
    out = []
    for i in range(len(requests)):
        j = second[i]
        out.append(synthesize(raw[i], raw[j] if j is not None else None))
    return out, version


@app.post("/authz/check-batch", response_model=BatchResponse, dependencies=ServiceAuth)
async def check_batch(body: BatchRequest) -> BatchResponse:
    """批量判定（契约 1）：能力网关的发现过滤与 Runtime 闸口共用。"""
    decided, version = await decide(body.requests)
    results = []
    for req, (decision, policy_ids, reason) in zip(body.requests, decided, strict=True):
        did = _record(req.model_dump(), decision, reason, policy_ids, version)
        results.append(BatchResult(decision=decision, policy_ids=policy_ids, reason=reason, decision_id=did))  # type: ignore[arg-type]
    return BatchResponse(results=results, engine=settings.engine, policy_version=version)


# ---- 单条判定（兼容旧调用方） ----
class CheckRequest(BaseModel):
    """一次授权判定请求，由 Runtime 闸口或能力网关发起（旧格式，保持兼容）。"""

    subject: str  # 调用主体标识，如 human:123 / agent:demo
    agent: str | None = None  # 代理执行的 Agent 标识（租户信息放 context.agent）
    capability: str  # 目标能力 id，如 cap.ticket.close_batch
    action: str = "invoke"  # 动作，对应 Cedar Schema 中的 action
    principal: dict[str, Any] = Field(default_factory=lambda: {"role": "user", "tenant": "default"})  # 主体属性：tenant、roles / role
    resource: dict[str, Any] = Field(default_factory=dict)  # 能力属性：risk_level / tenant / system / data_class
    context: dict[str, Any] = Field(default_factory=lambda: {"approved": False, "taint": []})  # 审批状态、数据污点、agent


class CheckResponse(BaseModel):
    """判定结果。"""

    decision: Decision  # 放行 / 拒绝 / 需审批
    decision_id: str  # 判定编号，供事件日志与审计关联
    matched: str  # 命中的规则名（即 reason）
    engine: EngineName  # 判定引擎
    policy_ids: list[str] = Field(default_factory=list)  # 决定性策略 id
    policy_version: str = ""  # 策略版本


def _from_legacy(req: CheckRequest) -> AuthzRequest:
    """旧格式 → 契约 1 格式；缺租户 / 风险级别等必填属性时抛 ValidationError（随后按 deny 处理）。"""
    p = req.principal
    roles = p.get("roles")
    if roles is None:
        roles = [p["role"]] if isinstance(p.get("role"), str) else []
    agent = req.context.get("agent")
    return AuthzRequest.model_validate(
        {
            "principal": {"sub": p.get("sub") or req.subject, "tenant": p.get("tenant"), "roles": roles},
            "action": req.action,
            "resource": {**req.resource, "id": req.resource.get("id") or req.capability},
            "context": {
                "approved": req.context.get("approved", False),
                "taint": req.context.get("taint", []),
                "agent": agent if isinstance(agent, dict) else None,
            },
        }
    )


@app.post("/authz/check", response_model=CheckResponse, dependencies=ServiceAuth)
async def check(req: CheckRequest) -> CheckResponse:
    """执行一次判定并记录到判定日志；请求属性不全时 deny（invalid_request）。"""
    try:
        areq = _from_legacy(req)
    except ValidationError:
        did = _record(req.model_dump(), "deny", "invalid_request", [], "")
        return CheckResponse(decision="deny", decision_id=did, matched="invalid_request", engine=settings.engine)
    [(decision, policy_ids, reason)], version = await decide([areq])
    did = _record(req.model_dump(), decision, reason, policy_ids, version)
    return CheckResponse(
        decision=decision,  # type: ignore[arg-type]
        decision_id=did,
        matched=reason,
        engine=settings.engine,
        policy_ids=policy_ids,
        policy_version=version,
    )


@app.get("/authz/decisions", dependencies=ServiceAuth)
async def decisions(limit: int = 50) -> list[dict[str, Any]]:
    """返回最近的判定记录（审计 / 调试用）。"""
    return DECISION_LOG[-limit:]


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """健康检查（含当前引擎）。"""
    return {"status": "ok", "engine": settings.engine}

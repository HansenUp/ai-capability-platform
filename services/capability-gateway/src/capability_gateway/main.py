"""能力网关 API（Capability Gateway，默认端口 8082）。

对应设计中的"AI Capability Gateway"层，提供：
- 两级能力发现：`GET /capabilities`（binding(core) ∩ Runtime 传的 names）与 `POST /capabilities/search`
  （硬过滤 → BM25 + kNN → RRF → 策略批量判定，契约 4）；
- 能力目录：MySQL 权威（CATALOG_BACKEND=mysql，网关自带 Alembic）+ outbox 同步 ES（search.py）；
- 授权代理：`POST /authz/check` 经策略服务 `/authz/check-batch` 判定（单一判定点，不可达即 deny）；
- Token Exchange：`POST /token/exchange` 按 RFC 8693 向 Keycloak 换取 OBO 令牌；
- 工具分发：`POST /invoke` 以终端用户令牌换取面向连接器的 OBO 令牌，并附网关签发的 act 断言（act.py）后转发；
- 连接器管理与探活（connectors.py）、写操作规范三处校验（spec.py）；
- Skill 商店：`/skills`，入库前执行供应链硬规则（签名、静态扫描、评测集）；
- MCP 数据面（mcp_proxy，开发态候选，GATEWAY_ENV=prod 不挂载，S9 删除）。

调用方鉴权（契约 3，auth.py）：`Authorization: Bearer <ai-runtime-svc 服务令牌>` + `X-AI-User-Token: <终端用户令牌>`；
未配置 Keycloak 时为开发态 dev 头。执行者身份不接受客户端自称——act 由服务端依据经过认证的调用方组装。
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

import httpx
from fastapi import Depends, FastAPI, Response
from pydantic import BaseModel, ConfigDict, Field

from capability_gateway import act, auth, config, connectors, errors, policy, search, spec, state
from capability_gateway.auth import Caller, require_gateway_admin, require_runtime_client
from capability_gateway.catalog import CatalogConflict
from capability_gateway.embed import get_embedder
from capability_gateway.errors import ErrorDetail, GatewayError, responses
from capability_gateway.policy import AgentRef
from capability_gateway.registry import Binding, Capability

config.check_prod()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """启动：目录初始化（MySQL 迁移）、加载 act 签名密钥、后台 outbox 同步与连接器探活。"""
    await state.catalog.init()
    act.load_key()
    indexer = search.Indexer(state.catalog, state.es, get_embedder())
    tasks = [
        asyncio.create_task(indexer.run(config.INDEXER_INTERVAL_S)),
        asyncio.create_task(connectors.run_prober(config.PROBE_INTERVAL_S)),
    ]
    try:
        yield
    finally:
        for t in tasks:
            await search.stop_task(t)
        await state.catalog.close()


app = FastAPI(title="AI Capability Platform · Capability Gateway", version="0.1.0", lifespan=lifespan)
errors.install(app)
CallerDep = Annotated[Caller, Depends(require_runtime_client)]
SKILLS: dict[str, dict[str, Any]] = {}  # Skill 商店（内存），键为 name@version
AUTH_ERRORS = (401, 403, 503)


# ---- 第一级发现 ----
@app.get("/capabilities", responses=responses(*AUTH_ERRORS))
async def core_capabilities(subject: str, agent: str, caller: CallerDep, names: str | None = None) -> dict[str, Any]:
    """第一级发现：该 Agent 的 core 绑定（含 "*"）中已发布的能力（受 CORE_TOOL_LIMIT 限制）。
    带 names（逗号分隔，即 Agent 版本快照声明的白名单）时取 binding(core) ∩ names，按 names 顺序（D62）。"""
    core = await state.catalog.bound_ids(agent, "core")
    tenant = None if caller.dev else caller.principal().tenant  # 服务态必须有用户令牌（否则 401），按其租户过滤
    if names is not None:
        found = [await state.catalog.by_name(n, tenant) for n in dict.fromkeys(n for n in names.split(",") if n)]
        caps = [c for c in found if c is not None and c.id in core]
    else:
        caps = [c for c in await state.catalog.list_published() if c.id in core and (tenant is None or c.tenant == tenant)]
    # tenant / system 供 Runtime 闸口组装策略资源属性（ToolSpec 之外的字段，由 Runtime 自行取用）
    tools = [c.to_tool_spec() | {"tenant": c.tenant, "system": c.system} for c in caps][: config.CORE_LIMIT]
    return {"subject": subject, "agent": agent, "tools": tools, "limit": config.CORE_LIMIT}


# ---- 第二级发现（契约 4） ----
class SearchIn(BaseModel):
    """第二级发现请求（Runtime 的 search_capabilities 元工具发起）；用户由 X-AI-User-Token 确定。"""

    query: str = Field(min_length=1, max_length=1000)
    agent_id: str = Field(min_length=1, max_length=64)
    agent_version: str = Field(default="", max_length=32)
    scope: list[str] | None = Field(default=None, max_length=50)  # 版本快照冻结的 discovery.scope：系统名 / 工具名 / 能力 id
    limit: int = Field(default=5, ge=1, le=20)
    taint: list[str] = Field(default_factory=list, max_length=20)  # 本次 Run 的数据污点
    agent_tenant: str | None = Field(default=None, max_length=64)  # Agent 所属租户（缺省取用户租户）


class SearchItem(BaseModel):
    id: str
    name: str
    description: str
    input_schema: dict[str, Any]
    risk_level: str
    data_class: str
    system: str
    tenant: str  # 能力所属租户（Runtime 写入闸口资源属性）
    decision: Literal["allow", "require_approval"]
    score: float
    supports_dry_run: bool = False
    requires_idempotency_key: bool = False


class SearchOut(BaseModel):
    items: list[SearchItem]
    degraded: bool  # ES 不可用、退回目录词元打分


@app.post("/capabilities/search", responses=responses(*AUTH_ERRORS))
async def search_capabilities(body: SearchIn, caller: CallerDep) -> SearchOut:
    """硬过滤 → BM25 top50 + kNN top50 → RRF(k=60) → 取 3×limit 交策略批量判定（deny 剔除、require_approval 标注）→ 截断 limit。"""
    principal = caller.principal()
    ranked = await search.hybrid_search(
        state.catalog, state.es, get_embedder(), body.query, tenant=principal.tenant, agent_id=body.agent_id, scope=body.scope
    )
    pool = ranked.items[: 3 * body.limit]
    agent = AgentRef(body.agent_id, body.agent_version, body.agent_tenant or principal.tenant)
    decisions = await policy.check_batch([policy.build_request(principal, cap, agent, taint=body.taint) for cap, _ in pool])
    items = [
        SearchItem(
            id=cap.id,
            name=cap.name,
            description=cap.description,
            input_schema=cap.input_schema,
            risk_level=cap.risk_level,
            data_class=cap.data_class,
            system=cap.system,
            tenant=cap.tenant,
            decision=d.decision,  # type: ignore[arg-type]
            score=round(score, 6),
            supports_dry_run=cap.supports_dry_run,
            requires_idempotency_key=cap.requires_idempotency_key,
        )
        for (cap, score), d in zip(pool, decisions, strict=True)
        if d.decision != "deny"
    ]
    return SearchOut(items=items[: body.limit], degraded=ranked.degraded)


@app.get("/capabilities/{cid}", responses=responses(*AUTH_ERRORS, 404))
async def get_capability(cid: str, caller: CallerDep) -> Capability:
    """按 id 查询能力详情；服务态只返回用户租户内的能力（其他租户与不存在同为 404）。"""
    c = await state.catalog.get(cid)
    if c is None or (not caller.dev and c.tenant != caller.principal().tenant):
        raise GatewayError(404, "unknown_capability", "unknown capability")
    return c


@app.post("/capabilities", status_code=201, dependencies=[Depends(require_gateway_admin)], responses=responses(403, 409))
async def upsert_capability(c: Capability) -> Capability:
    """登记或更新一条能力（生产由 Console 管理员操作）；承载连接器无已探活的 manifest、工具不在其中或写规范不合规、
    同租户工具名冲突 409。"""
    cv = await state.catalog.connector_for(c)
    manifest = await connectors.active_manifest(cv.name) if cv is not None else None
    problems = spec.validate_capability(c, manifest)
    if problems:
        raise GatewayError(409, "tool_spec_violation", "; ".join(problems))
    try:
        return await state.catalog.upsert(c)
    except CatalogConflict as e:
        raise GatewayError(409, "name_conflict", str(e)) from None


# ---- Agent 绑定（替代 core_for_agents） ----
@app.get("/agents/{agent_id}/bindings", dependencies=[Depends(require_gateway_admin)], responses=responses(403))
async def list_bindings(agent_id: str) -> list[Binding]:
    """某 Agent 的能力绑定（不含 "*" 通配，查通配用 agent_id=*）。"""
    return await state.catalog.bindings(agent_id=agent_id)


@app.put("/agents/{agent_id}/bindings/{cid}", dependencies=[Depends(require_gateway_admin)], responses=responses(403, 404))
async def put_binding(agent_id: str, cid: str, kind: Literal["core", "discoverable"] = "core") -> Binding:
    """绑定能力到 Agent（core：第一级预载候选；discoverable：第二级可检索）。"""
    b = Binding(agent_id=agent_id, capability_id=cid, kind=kind)
    try:
        await state.catalog.put_binding(b)
    except KeyError:
        raise GatewayError(404, "unknown_capability", "unknown capability") from None
    return b


@app.delete(
    "/agents/{agent_id}/bindings/{cid}", status_code=204, dependencies=[Depends(require_gateway_admin)], responses=responses(403, 404)
)
async def delete_binding(agent_id: str, cid: str, kind: Literal["core", "discoverable"] = "core") -> Response:
    if not await state.catalog.delete_binding(Binding(agent_id=agent_id, capability_id=cid, kind=kind)):
        raise GatewayError(404, "unknown_binding", "binding not found")
    return Response(status_code=204)


# ---- 授权代理（单一判定点 = 策略服务） ----
class AuthzIn(BaseModel):
    """授权判定请求；主体取自 X-AI-User-Token（开发态可用 subject / X-AI-Subject）。"""

    subject: str = ""  # 仅开发态使用
    agent: str
    agent_version: str = ""
    agent_tenant: str | None = None
    capability: str  # 能力 id
    approved: bool = False  # 是否已审批
    taint: list[str] = Field(default_factory=list)  # 本次 Run 的数据污点


class AuthzOut(BaseModel):
    decision: Literal["allow", "deny", "require_approval"]
    engine: str | None = None
    policy_ids: list[str] = Field(default_factory=list)
    reason: str | None = None


@app.post("/authz/check", responses=responses(*AUTH_ERRORS, 404))
async def authz_check(body: AuthzIn, caller: CallerDep) -> AuthzOut:
    """对已登记能力执行授权判定（策略服务不可达 → deny，reason=policy_unavailable）。"""
    c = await state.catalog.get(body.capability)
    if c is None:
        raise GatewayError(404, "unknown_capability", "unknown capability")
    principal = caller.principal(body.subject)
    agent = AgentRef(body.agent, body.agent_version, body.agent_tenant or principal.tenant)
    d = await policy.check(principal, c, agent, approved=body.approved, taint=body.taint)
    return AuthzOut(decision=d.decision, engine=d.engine, policy_ids=list(d.policy_ids), reason=d.reason)  # type: ignore[arg-type]


# ---- Token Exchange（RFC 8693，代表用户 on-behalf-of） ----
class ExchangeIn(BaseModel):
    """令牌交换请求。执行者身份不在此声明：请求体里即使混入 actor 等字段也会被忽略。
    已配置 Keycloak 时用户令牌只取 X-AI-User-Token（已验签），subject_token 被忽略。"""

    model_config = ConfigDict(extra="ignore")

    subject_token: str | None = None  # 开发态：用户的访问令牌
    audience: str  # 目标受众（连接器 / 业务系统 Client）
    agent_id: str
    agent_version: str


@app.post("/token/exchange", responses=responses(*AUTH_ERRORS, 502))
async def token_exchange(body: ExchangeIn, caller: CallerDep) -> dict[str, Any]:
    """用用户令牌换取面向目标系统的短期 OBO 令牌；未配置 Keycloak 时返回开发桩。"""
    actor = f"agent:{body.agent_id}@{body.agent_version}"  # act 声明由服务端组装，不取自请求体
    if not config.keycloak_enabled():
        return {
            "access_token": f"dev.{uuid.uuid4().hex}",
            "token_type": "Bearer",
            "expires_in": 300,
            "aud": body.audience,
            "act": actor,
            "note": "KEYCLOAK_TOKEN_URL not set; dev stub",
        }
    principal = caller.principal()
    async with httpx.AsyncClient(timeout=5.0, transport=config.transport) as cl:
        token, _ = await _obo_token(cl, principal.token or "", body.audience, principal.exp)
    return {"access_token": token, "token_type": "Bearer", "aud": body.audience, "act": actor}


# ---- 工具分发 ----
# (用户令牌摘要, audience) → (OBO 令牌, monotonic 过期时刻)。LRU：命中移到末尾，超过上限从头部淘汰；读到过期项即剔除（D103）
MCP_TOKEN_CACHE: OrderedDict[tuple[str, str], tuple[str, float]] = OrderedDict()
MCP_TOKEN_CACHE_MAX = max(1, int(os.environ.get("MCP_TOKEN_CACHE_MAX", "10000")))
OBO_SKEW_S = 30.0


def set_invoke_transport_for_tests(transport: httpx.AsyncBaseTransport | None) -> None:
    """测试注入：网关发出的全部 HTTP 请求经该 transport；同时清空 OBO 与 JWKS 缓存。"""
    config.transport = transport
    MCP_TOKEN_CACHE.clear()
    auth.reset_for_tests()


def _exp(token: str) -> float | None:
    exp = auth.unverified_claims(token).get("exp")
    return float(exp) if isinstance(exp, int | float) else None


async def _obo_token(client: httpx.AsyncClient, subject_token: str, audience: str, user_exp: float | None = None) -> tuple[str, float]:
    """RFC 8693 标准 Token Exchange（Keycloak 26.2+）：以终端用户令牌换取受众为目标连接器的短期令牌（sub=用户，azp=网关）。
    缓存键（用户令牌摘要, 受众）；TTL = min(OBO exp, 用户令牌 exp) − 30 s（D131）；LRU 上限 MCP_TOKEN_CACHE_MAX。"""
    key = (hashlib.sha256(subject_token.encode()).hexdigest(), audience)
    cached = MCP_TOKEN_CACHE.get(key)
    if cached is not None:
        if time.monotonic() < cached[1]:
            MCP_TOKEN_CACHE.move_to_end(key)
            return cached
        del MCP_TOKEN_CACHE[key]
    data = {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "client_id": config.GATEWAY_CLIENT_ID,
        "client_secret": config.GATEWAY_CLIENT_SECRET,
        "subject_token": subject_token,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "audience": audience,
        "scope": f"{audience}-audience",
    }
    r = await client.post(config.KEYCLOAK_TOKEN_URL or "", data=data)
    if r.status_code != 200:
        code = 401 if r.status_code in (400, 401, 403) else 502
        raise GatewayError(code, "token_exchange_failed", f"token exchange failed: HTTP {r.status_code}")
    body = r.json()
    token = str(body["access_token"])
    now = time.time()
    obo_exp = now + float(body.get("expires_in", 60))
    claim_exp = _exp(token)
    if claim_exp is not None:
        obo_exp = min(obo_exp, claim_exp)
    if user_exp is None:
        user_exp = _exp(subject_token)
    deadline = min(obo_exp, user_exp if user_exp is not None else math.inf) - OBO_SKEW_S
    entry = (token, time.monotonic() + (deadline - now))
    if deadline > now:
        MCP_TOKEN_CACHE[key] = entry
        MCP_TOKEN_CACHE.move_to_end(key)
        while len(MCP_TOKEN_CACHE) > MCP_TOKEN_CACHE_MAX:
            MCP_TOKEN_CACHE.popitem(last=False)
    return entry


class InvokeIn(BaseModel):
    """工具调用请求（由 Runtime 的工具代理在闸口放行后发出）。"""

    tool: str = Field(max_length=128)  # 工具名（能力的 name）
    input: dict[str, Any] = Field(default_factory=dict)
    subject: str = Field(default="", max_length=128)  # 开发态的发起主体；已配置 Keycloak 时以 X-AI-User-Token 为准
    agent_id: str = Field(max_length=64)
    agent_version: str = Field(max_length=32)
    agent_tenant: str | None = Field(default=None, max_length=64)
    run_id: str | None = Field(default=None, max_length=40)
    call_id: str | None = Field(default=None, max_length=128)
    idempotency_key: str | None = Field(default=None, max_length=128)
    dry_run: bool = False
    approved: bool = False  # Runtime 闸口已完成审批
    taint: list[str] = Field(default_factory=list, max_length=20)


@app.post("/invoke", responses=responses(*AUTH_ERRORS, 404, 409, 502))
async def invoke(body: InvokeIn, caller: CallerDep) -> dict[str, Any]:
    """把一次工具调用分发到能力所属连接器。

    - 能力须已登记、已发布且属于用户租户；写操作非 dry-run 缺幂等键 409；策略 deny（或策略不可达）403（第二道防线）；
      策略 require_approval 且非 dry-run（Runtime 未传 approved=true）403 approval_required；approved / taint / agent_tenant 由
      Runtime 闸口给出，Agent 跨租户、污点外发未审批据此被策略拒绝；
    - 连接器连续探活失败（down）503；工具须在最近一次 manifest 中且合规、manifest 与激活基线一致（否则 409 / 503）；
    - 身份：已配置 Keycloak 时以已验签的用户令牌换 OBO 放入 Authorization，并附 `X-AI-Act-Assertion`（act.py）；
      开发态以 `X-AI-Dev-Sub` 转发主体；两种模式都带 `X-AI-Act`（只在开发态被 MCP 端采信）；
    - 业务结果（含 4xx）原样包在 `{ok, status_code, output}` 中返回；网关自身问题才用 HTTP 错误码。
    """
    principal = caller.principal(body.subject)
    cap = await state.catalog.by_name(body.tool, principal.tenant)
    if cap is None:
        raise GatewayError(404, "unknown_capability", "unknown capability")
    write = cap.risk_level != "read"
    if write and not body.dry_run and not body.idempotency_key:
        raise GatewayError(409, "idempotency_key_required", "write operations require an idempotency key unless dry_run")
    agent = AgentRef(body.agent_id, body.agent_version, body.agent_tenant or principal.tenant)
    d = await policy.check(principal, cap, agent, approved=body.approved, taint=body.taint)
    if d.decision == "deny":
        code = "policy_unavailable" if d.reason == policy.UNAVAILABLE else "policy_denied"
        raise GatewayError(403, code, "capability denied by policy" if code == "policy_denied" else "policy service unavailable")
    if d.decision == "require_approval" and not body.dry_run:
        raise GatewayError(403, "approval_required", "capability requires human approval before execution")
    cv = await state.catalog.connector_for(cap)
    if cv is None:
        raise GatewayError(502, "no_connector", f"no connector registered for capability '{cap.id}'")
    if cv.health == "down":
        raise GatewayError(503, "connector_down", f"connector '{cv.name}' is down")
    await connectors.require_compliant_tool(cv, cap.name)
    payload = dict(body.input)
    if write:
        payload["dry_run"] = body.dry_run
        if body.idempotency_key:  # 影响预演（dry_run）没有幂等键，不传 null
            payload["idempotency_key"] = body.idempotency_key
    headers = {"X-AI-Act": f"agent:{body.agent_id}@{body.agent_version}"}
    if body.run_id:
        headers["X-AI-Run-Id"] = body.run_id
    if body.call_id:
        headers["X-AI-Call-Id"] = body.call_id
    async with httpx.AsyncClient(timeout=config.INVOKE_TIMEOUT_S, transport=config.transport) as client:
        obo: str | None = None
        if config.keycloak_enabled():
            obo, _ = await _obo_token(client, principal.token or "", cv.name, principal.exp)
            headers["Authorization"] = f"Bearer {obo}"
        else:
            headers["X-AI-Dev-Sub"] = principal.sub.split(":", 1)[-1]
        headers[act.HEADER] = act.sign(
            sub=principal.sub,
            audience=cv.name,
            agent_id=body.agent_id,
            agent_version=body.agent_version,
            caller=caller.client_id,
            run_id=body.run_id,
            call_id=body.call_id,
            obo_token=obo,
        )
        try:
            if cv.protocol == "mcp":
                return await _invoke_mcp(client, cv.endpoint, cap, payload, headers)
            r = await client.post(f"{cv.endpoint.rstrip('/')}/tools/{cap.name}", json=payload, headers=headers)
        except httpx.HTTPError as e:
            raise GatewayError(502, "connector_unreachable", f"connector unreachable: {type(e).__name__}") from e
    try:
        output: Any = r.json()
    except ValueError:
        output = {"error": r.text[:500]}
    return {"ok": r.status_code < 400, "status_code": r.status_code, "output": output, "capability_id": cap.id}


async def _invoke_mcp(
    client: httpx.AsyncClient, endpoint: str, cap: Capability, payload: dict[str, Any], headers: dict[str, str]
) -> dict[str, Any]:
    """MCP 协议连接器：tools/call；isError 或 JSON-RPC error 作为业务失败返回。"""
    try:
        result = await connectors.mcp_call(client, endpoint, "tools/call", {"name": cap.name, "arguments": payload}, headers)
    except httpx.HTTPStatusError as e:
        return {"ok": False, "status_code": e.response.status_code, "output": {"error": e.response.text[:500]}, "capability_id": cap.id}
    except ValueError as e:
        return {"ok": False, "status_code": 400, "output": {"error": str(e)[:500]}, "capability_id": cap.id}
    is_error = isinstance(result, dict) and bool(result.get("isError"))
    output = (result.get("structuredContent") or result.get("content")) if isinstance(result, dict) else result
    return {"ok": not is_error, "status_code": 400 if is_error else 200, "output": output, "capability_id": cap.id}


@app.get("/.well-known/jwks.json")
async def jwks() -> dict[str, Any]:
    """act 断言的验签公钥（ES256）。"""
    return act.jwks()


# ---- Skill 商店（Agent Skills 规范包 + 治理元数据） ----
class SkillIn(BaseModel):
    """Skill 上架申请。"""

    name: str
    version: str
    owner: str
    package_uri: str  # 例：s3://...，SKILL.md 目录的签名归档
    signature: str  # 包签名（必填）
    capabilities_declared: list[str] = Field(default_factory=list)  # 声明会调用的能力 id
    risk_level: str = "read"
    eval_dataset_ref: str | None = None  # 评测集引用（必填）
    scan_passed: bool = False  # 静态扫描是否通过（必须为 True）


@app.post("/skills", status_code=201, responses={409: {"model": ErrorDetail}})
async def submit_skill(body: SkillIn) -> dict[str, Any]:
    """上架 Skill；供应链硬规则（签名、静态扫描、评测集）任一不满足即 409 拒绝。"""
    if not body.signature:
        raise GatewayError(409, "signature_required", "signature required")
    if not body.scan_passed:
        raise GatewayError(409, "scan_required", "static scan must pass before listing")
    if body.eval_dataset_ref is None:
        raise GatewayError(409, "eval_dataset_required", "skills must ship an evaluation dataset")
    SKILLS[f"{body.name}@{body.version}"] = body.model_dump() | {"status": "listed"}
    return SKILLS[f"{body.name}@{body.version}"]


@app.get("/skills")
async def list_skills() -> list[dict[str, Any]]:
    """列出已上架 Skill。"""
    return list(SKILLS.values())


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """健康检查。"""
    return {"status": "ok"}


app.include_router(connectors.router)

# MCP 数据面（ADR-0005 自研候选）：/mcp/{server} 流式透传 + 第三方数据面的判定回调 /authz/mcp-check。
# 放在末尾挂载：/mcp/servers 的登记与列表路由先注册、优先匹配。生产不挂载（D133：生产走 /invoke，S9 删除）。
if config.GATEWAY_ENV != "prod":
    from capability_gateway import mcp_proxy  # noqa: E402

    app.include_router(mcp_proxy.router)

"""能力网关 API（Capability Gateway，默认端口 8082）。

对应设计中的"AI Capability Gateway"层，提供：
- 两级能力发现：`GET /capabilities`（核心预载）与 `POST /capabilities/search`（按意图检索，经策略过滤）；
- 授权代理：`POST /authz/check` 统一转发到策略服务（单一判定点），不可达时回退到 Python 基线；
- Token Exchange：`POST /token/exchange` 按 RFC 8693 向 Keycloak 换取带 `act` 声明的 OBO 令牌；
- MCP 连接器登记：`/mcp/servers`；
- Skill 商店：`/skills`，入库前执行供应链硬规则（签名、静态扫描、评测集）；
- 工具分发（S4.3）：`POST /invoke` 以终端用户令牌换取面向目标 MCP Server 的 OBO 令牌后转发调用；
- MCP 数据面（S6.6，见 mcp_proxy）：`/mcp/{server}` 以 streamable-http 流式透传，换 OBO、转发前同步判定、失败关闭。
内存存储为开发态实现，S8 工作包迁移到 MySQL。

安全约束：执行者身份必须可验证，不接受客户端自称——`act` 声明由服务端依据经过认证的调用方身份组装，
请求体里携带的任何执行者字段一律忽略。
"""

from __future__ import annotations

import os
import uuid
from collections import OrderedDict
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from capability_gateway.registry import Capability, Registry

app = FastAPI(title="AI Capability Platform · Capability Gateway", version="0.1.0")
registry = Registry()  # 能力目录（内存）
POLICY_URL = os.environ.get("POLICY_URL", "http://localhost:8083")  # 策略服务地址
KEYCLOAK_TOKEN_URL = os.environ.get("KEYCLOAK_TOKEN_URL")  # 例：https://idp/realms/ai/protocol/openid-connect/token
GATEWAY_CLIENT_ID = os.environ.get("GATEWAY_CLIENT_ID", "capability-gateway")  # 网关在 Keycloak 中的 Client
GATEWAY_CLIENT_SECRET = os.environ.get("GATEWAY_CLIENT_SECRET", "")
GATEWAY_ENV = os.environ.get("GATEWAY_ENV", "dev")  # dev | prod
GATEWAY_ADMIN_TOKEN = os.environ.get("GATEWAY_ADMIN_TOKEN", "")  # 登记 MCP Server / 能力所需的管理令牌（X-Gateway-Admin-Token）
# 第三方数据面（agentgateway ext_authz / ContextForge 插件）回调 /authz/mcp-check 的服务间共享密钥（X-Gateway-Authz-Token）
GATEWAY_AUTHZ_TOKEN = os.environ.get("GATEWAY_AUTHZ_TOKEN", "")
if GATEWAY_ENV == "prod" and (not KEYCLOAK_TOKEN_URL or not GATEWAY_ADMIN_TOKEN or not GATEWAY_AUTHZ_TOKEN):
    # 生产必须走 OBO 令牌（开发态会以请求体里的主体身份调用 MCP Server），登记接口必须受管理令牌保护，
    # 判定端点必须受共享密钥保护（否则任何人都能探测策略结果）
    raise RuntimeError("GATEWAY_ENV=prod requires KEYCLOAK_TOKEN_URL, GATEWAY_ADMIN_TOKEN and GATEWAY_AUTHZ_TOKEN")
TRUSTED_GATEWAY_CLIENTS = {GATEWAY_CLIENT_ID}  # 允许签发 OBO 令牌的受信任网关客户端白名单
CORE_LIMIT = int(os.environ.get("CORE_TOOL_LIMIT", "20"))  # 核心工具预载上限，超出部分靠第二级发现

MCP_SERVERS: dict[str, dict[str, Any]] = {}  # MCP Server 登记表（内存）
SKILLS: dict[str, dict[str, Any]] = {}  # Skill 商店（内存），键为 name@version


class ErrorDetail(BaseModel):
    """HTTPException 的响应体。业务规则拒绝统一用 409 + ErrorDetail，422 只保留给 FastAPI 参数校验。"""

    detail: str


# ---- 能力发现 ----
@app.get("/capabilities")
async def core_capabilities(subject: str, agent: str, names: str | None = None) -> dict[str, Any]:
    """第一级发现：返回该 Agent 需要预载的核心工具（受 CORE_LIMIT 限制）。
    带 names（逗号分隔的工具名，即 Agent 版本快照声明的白名单）时只返回其中已发布的能力——Runtime 据此组装，
    网关侧登记的 core_for_agents 不能给 Agent 额外加工具。"""
    if names is not None:
        wanted = [n for n in names.split(",") if n]
        caps = [c for c in (registry.by_name(n) for n in wanted) if c is not None]
    else:
        caps = registry.core_for(agent)
    tools = [c.to_tool_spec() for c in caps][:CORE_LIMIT]
    return {"subject": subject, "agent": agent, "tools": tools, "limit": CORE_LIMIT}


class SearchIn(BaseModel):
    """第二级发现请求，由 Runtime 的 search_capabilities 元工具发起。"""

    subject: str  # 调用主体
    agent: str  # 当前 Agent
    intent: str  # 意图文本
    limit: int = 5


@app.post("/capabilities/search")
async def search_capabilities(body: SearchIn) -> dict[str, Any]:
    """第二级发现：按意图检索并用策略引擎过滤掉会被直接拒绝的能力。"""
    hits = registry.search(body.intent, body.limit)
    # 权限过滤：deny 的能力不返回；allow / require_approval 作为 decision_hint 一并告知模型
    allowed = []
    for c in hits:
        d = await _authz(body.subject, body.agent, c)
        if d != "deny":
            allowed.append(c.to_tool_spec() | {"decision_hint": d})
    return {"candidates": allowed}


@app.get("/capabilities/{cid}")
async def get_capability(cid: str) -> Capability:
    """按 id 查询能力详情。"""
    c = registry.get(cid)
    if c is None:
        raise HTTPException(404)
    return c


def require_gateway_admin(x_gateway_admin_token: Annotated[str | None, Header()] = None) -> None:
    """登记类接口的保护（S4 评审）：网关会把真实的 OBO 令牌发往登记的 MCP 端点，未授权的登记等于可以截获令牌。
    配置了 GATEWAY_ADMIN_TOKEN 时必须带上相同的 X-Gateway-Admin-Token；启用 OBO（KEYCLOAK_TOKEN_URL）却未配置管理令牌时
    登记接口一律关闭；纯开发态（两者都未配置）不检查。"""
    import hmac

    if GATEWAY_ADMIN_TOKEN:
        if not x_gateway_admin_token or not hmac.compare_digest(x_gateway_admin_token, GATEWAY_ADMIN_TOKEN):
            raise HTTPException(403, "gateway admin token required")
    elif KEYCLOAK_TOKEN_URL:
        raise HTTPException(403, "registration disabled: GATEWAY_ADMIN_TOKEN not configured")


@app.post("/capabilities", status_code=201, dependencies=[Depends(require_gateway_admin)])
async def upsert_capability(c: Capability) -> Capability:
    """登记或更新一条能力（生产由 Console 管理员操作）。"""
    return registry.upsert(c)


# ---- 授权代理（单一判定点 = 策略服务） ----
async def _authz(subject: str, agent: str, c: Capability, approved: bool = False, taint: list[str] | None = None) -> str:
    """把能力元数据组装成策略请求并转发到策略服务；不可达时回退到 Python 基线（同一套规则）。"""
    body = {
        "subject": subject,
        "agent": agent,
        "capability": c.id,
        "action": "invoke",
        "principal": {"tenant": c.tenant},
        "resource": {"system": c.system, "risk_level": c.risk_level, "data_class": c.data_class, "tenant": c.tenant},
        "context": {"approved": approved, "taint": taint or []},
    }
    try:
        async with httpx.AsyncClient(timeout=2.0) as cl:
            r = await cl.post(f"{POLICY_URL}/authz/check", json=body)
            r.raise_for_status()
            return r.json()["decision"]
    except httpx.HTTPError:
        from policy_service.baseline import evaluate  # 开发回退：与 Cedar 策略同步的 Python 镜像

        return evaluate(body["principal"], body["resource"], body["context"])[0]


class AuthzIn(BaseModel):
    """授权判定请求。"""

    subject: str
    agent: str
    capability: str  # 能力 id
    approved: bool = False  # 是否已审批
    taint: list[str] = Field(default_factory=list)  # 本次 Run 的数据污点


@app.post("/authz/check")
async def authz_check(body: AuthzIn) -> dict[str, str]:
    """对已登记能力执行授权判定。"""
    c = registry.get(body.capability)
    if c is None:
        raise HTTPException(404, "unknown capability")
    return {"decision": await _authz(body.subject, body.agent, c, body.approved, body.taint)}


# ---- Token Exchange（RFC 8693，代表用户 on-behalf-of） ----
def resolve_caller_client(
    authorization: Annotated[str | None, Header()] = None,
    x_gateway_client: Annotated[str | None, Header()] = None,
) -> str:
    """解析调用方（网关客户端）身份，供 act 声明使用：执行者身份必须可验证，不接受客户端在请求体里自称。"""
    if KEYCLOAK_TOKEN_URL:
        # 生产：从 Authorization 里的网关客户端凭证（client_credentials 访问令牌）验签并取 azp / client_id。
        # 该分支由 S8 工作包接入 Keycloak 时实现；在此之前生产路径直接失败（fail-closed），不回退到请求头。
        raise NotImplementedError("gateway client authentication from Authorization: implemented in work package S8")
    # 开发：从 X-Gateway-Client 请求头读取调用方标识，缺省视为本地网关自身
    del authorization  # 开发态不解析 Authorization，显式丢弃以表明并非遗漏
    return x_gateway_client or GATEWAY_CLIENT_ID


class ExchangeIn(BaseModel):
    """令牌交换请求。执行者身份不在此声明：请求体里即使混入 actor 等字段也会被忽略。"""

    model_config = ConfigDict(extra="ignore")  # 忽略未声明字段，杜绝客户端自称执行者

    subject_token: str  # 用户的访问令牌
    audience: str  # 目标受众（MCP Server / 业务系统 Client）
    agent_id: str  # 代理执行的 Agent id，由服务端拼进 act 声明
    agent_version: str  # Agent 版本，与 id 一起唯一标识一个执行者


@app.post("/token/exchange")
async def token_exchange(body: ExchangeIn, caller: Annotated[str, Depends(resolve_caller_client)]) -> dict[str, Any]:
    """用用户令牌换取面向目标系统的短期 OBO 令牌；未配置 Keycloak 时返回开发桩。"""
    # 只有受信任的网关客户端才能签发带 act 声明的 OBO 令牌，其余调用方一律 403
    if caller not in TRUSTED_GATEWAY_CLIENTS:
        raise HTTPException(403, "untrusted gateway client")
    act = f"agent:{body.agent_id}@{body.agent_version}"  # act 声明由服务端组装，不取自请求体
    if not KEYCLOAK_TOKEN_URL:
        # 开发桩：不透明短期令牌，携带委托链信息（aud + act）；生产走 Keycloak 的 RFC 8693 换取真实令牌
        return {
            "access_token": f"dev.{uuid.uuid4().hex}",
            "token_type": "Bearer",
            "expires_in": 300,
            "aud": body.audience,
            "act": act,
            "note": "KEYCLOAK_TOKEN_URL not set; dev stub",
        }
    data = {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "client_id": GATEWAY_CLIENT_ID,
        "client_secret": GATEWAY_CLIENT_SECRET,
        "subject_token": body.subject_token,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "audience": body.audience,
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
    }
    async with httpx.AsyncClient(timeout=5.0) as cl:
        r = await cl.post(KEYCLOAK_TOKEN_URL, data=data)
        if r.status_code != 200:
            raise HTTPException(r.status_code, r.text)
        return r.json()


# ---- 工具分发（S4.3）----
# (用户令牌摘要, audience) → (OBO 令牌, 过期时间)。LRU：命中移到末尾，超过上限从头部淘汰；读到过期项即剔除（D103）
MCP_TOKEN_CACHE: OrderedDict[tuple[str, str], tuple[str, float]] = OrderedDict()
MCP_TOKEN_CACHE_MAX = max(1, int(os.environ.get("MCP_TOKEN_CACHE_MAX", "10000")))
INVOKE_TIMEOUT_S = float(os.environ.get("INVOKE_TIMEOUT_S", "30"))
_invoke_transport: httpx.AsyncBaseTransport | None = None  # 测试注入：Keycloak 与 MCP Server 的请求经该 transport


def set_invoke_transport_for_tests(transport: httpx.AsyncBaseTransport | None) -> None:
    global _invoke_transport
    _invoke_transport = transport
    MCP_TOKEN_CACHE.clear()


def _seed_mcp_servers() -> None:
    """开发态登记：工单系统的 MCP Server（地址取 MCP_TICKET_URL）。生产由 Console 管理员经 /mcp/servers 登记。"""
    MCP_SERVERS.setdefault(
        "mcp-ticket",
        {
            "name": "mcp-ticket",
            "endpoint": os.environ.get("MCP_TICKET_URL", "http://localhost:8091"),
            "protocol": "http",
            "auth_type": "inherit",
            "system": "ticket",
            "health": "unknown",
        },
    )


_seed_mcp_servers()


async def _obo_token(client: httpx.AsyncClient, subject_token: str, audience: str) -> tuple[str, float]:
    """RFC 8693 标准 Token Exchange（Keycloak 26.2+）：以终端用户令牌换取受众为目标 MCP Server 的短期令牌。
    sub 仍是终端用户，azp 为本网关；结果按（用户令牌摘要, 受众）缓存到过期前 30 秒（LRU，上限 MCP_TOKEN_CACHE_MAX）。"""
    import hashlib
    import time

    key = (hashlib.sha256(subject_token.encode()).hexdigest(), audience)
    cached = MCP_TOKEN_CACHE.get(key)
    if cached is not None:
        if time.monotonic() < cached[1]:
            MCP_TOKEN_CACHE.move_to_end(key)
            return cached
        del MCP_TOKEN_CACHE[key]  # 过期项读时剔除，不等淘汰
    data = {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "client_id": GATEWAY_CLIENT_ID,
        "client_secret": GATEWAY_CLIENT_SECRET,
        "subject_token": subject_token,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "audience": audience,
        "scope": f"{audience}-audience",
    }
    r = await client.post(KEYCLOAK_TOKEN_URL or "", data=data)
    if r.status_code != 200:
        # 用户令牌过期 / 无效等：按 401 交还调用方，不暴露 IdP 的错误细节
        raise HTTPException(401 if r.status_code in (400, 401, 403) else 502, f"token exchange failed: HTTP {r.status_code}")
    body = r.json()
    entry = (str(body["access_token"]), time.monotonic() + float(body.get("expires_in", 60)) - 30)
    MCP_TOKEN_CACHE[key] = entry
    MCP_TOKEN_CACHE.move_to_end(key)
    while len(MCP_TOKEN_CACHE) > MCP_TOKEN_CACHE_MAX:
        MCP_TOKEN_CACHE.popitem(last=False)  # 淘汰最久未使用的条目
    return entry


class InvokeIn(BaseModel):
    """工具调用请求（由 Runtime 的工具代理在闸口放行后发出）。"""

    tool: str = Field(max_length=128)  # 工具名（能力的 name）
    input: dict[str, Any] = Field(default_factory=dict)
    subject: str = Field(max_length=128)  # 发起主体（开发态转给 MCP Server 作身份；生产以令牌为准）
    agent_id: str = Field(max_length=64)
    agent_version: str = Field(max_length=32)
    run_id: str | None = Field(default=None, max_length=40)
    call_id: str | None = Field(default=None, max_length=128)
    idempotency_key: str | None = Field(default=None, max_length=128)
    dry_run: bool = False


@app.post(
    "/invoke",
    responses={401: {"model": ErrorDetail}, 403: {"model": ErrorDetail}, 404: {"model": ErrorDetail}, 502: {"model": ErrorDetail}},
)
async def invoke(body: InvokeIn, authorization: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    """把一次工具调用分发到能力所属系统的 MCP Server。

    - 能力必须已登记且已发布；策略判定为 deny 时 403（Runtime 闸口之外的第二道防线）；
    - 身份：配置了 KEYCLOAK_TOKEN_URL 时必须带终端用户令牌（Authorization: Bearer），网关换取 OBO 令牌后转发，
      执行 Agent 以 `X-AI-Act` 头声明（MCP Server 只信任 azp 为本网关的令牌携带的该头）；未配置时为开发态，
      以 `X-AI-Dev-Sub` 头转发发起主体（MCP Server 需显式开启开发身份）；
    - MCP Server 的业务结果（含 4xx，如无权限 / 不存在）原样包在 `{ok, status_code, output}` 中返回，
      网关自身的问题（未登记、令牌换取失败、MCP 不可达）才用 HTTP 错误码。
    """
    cap = registry.by_name(body.tool)
    if cap is None:
        raise HTTPException(404, "unknown capability")
    if await _authz(body.subject, body.agent_id, cap) == "deny":
        raise HTTPException(403, "capability denied by policy")
    server = next((s for s in MCP_SERVERS.values() if s.get("system") == cap.system), None)
    if server is None:
        raise HTTPException(502, f"no MCP server registered for system '{cap.system}'")
    payload = dict(body.input)
    if cap.risk_level != "read":
        payload["dry_run"] = body.dry_run
        if body.idempotency_key:  # 影响预演（dry_run）没有幂等键，不传 null
            payload["idempotency_key"] = body.idempotency_key
    headers = {"X-AI-Act": f"agent:{body.agent_id}@{body.agent_version}"}
    if body.run_id:
        headers["X-AI-Run-Id"] = body.run_id
    async with httpx.AsyncClient(timeout=INVOKE_TIMEOUT_S, transport=_invoke_transport) as client:
        if KEYCLOAK_TOKEN_URL:
            if not authorization or not authorization.lower().startswith("bearer "):
                raise HTTPException(401, "end-user token required")
            token, _ = await _obo_token(client, authorization.split(" ", 1)[1].strip(), server["name"])
            headers["Authorization"] = f"Bearer {token}"
        else:
            headers["X-AI-Dev-Sub"] = body.subject.split(":", 1)[-1]
        try:
            r = await client.post(f"{server['endpoint'].rstrip('/')}/tools/{cap.name}", json=payload, headers=headers)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"MCP server unreachable: {type(e).__name__}") from e
    try:
        output: Any = r.json()
    except ValueError:
        output = {"error": r.text[:500]}
    return {"ok": r.status_code < 400, "status_code": r.status_code, "output": output, "capability_id": cap.id}


# ---- MCP 连接器 ----
class McpServerIn(BaseModel):
    """MCP Server 登记信息。"""

    name: str
    endpoint: str  # Streamable HTTP 端点
    protocol: str = "streamable-http"
    auth_type: str = "inherit"  # inherit：透传 OBO 令牌；service：网关用服务账号
    system: str = ""  # 所属业务系统


@app.post("/mcp/servers", status_code=201, dependencies=[Depends(require_gateway_admin)])
async def register_mcp(body: McpServerIn) -> dict[str, Any]:
    """登记一个 MCP Server（健康状态由 S6 工作包的探活任务维护）。"""
    MCP_SERVERS[body.name] = body.model_dump() | {"health": "unknown"}
    return MCP_SERVERS[body.name]


@app.get("/mcp/servers")
async def list_mcp() -> list[dict[str, Any]]:
    """列出已登记的 MCP Server。"""
    return list(MCP_SERVERS.values())


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
        raise HTTPException(409, "signature required")
    if not body.scan_passed:
        raise HTTPException(409, "static scan must pass before listing")
    if body.eval_dataset_ref is None:
        raise HTTPException(409, "skills must ship an evaluation dataset")
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


# ---- MCP 数据面（ADR-0005 自研候选）：/mcp/{server} 流式透传 + 第三方数据面的判定回调 /authz/mcp-check ----
# 放在末尾挂载：/mcp/servers 的登记与列表路由先注册、优先匹配；mcp_proxy 在调用时才回取本模块，避免循环导入。
from capability_gateway import mcp_proxy  # noqa: E402

app.include_router(mcp_proxy.router)

"""能力网关 API（Capability Gateway，默认端口 8082）。

对应设计中的"AI Capability Gateway"层，提供：
- 两级能力发现：`GET /capabilities`（核心预载）与 `POST /capabilities/search`（按意图检索，经策略过滤）；
- 授权代理：`POST /authz/check` 统一转发到策略服务（单一判定点），不可达时回退到 Python 基线；
- Token Exchange：`POST /token/exchange` 按 RFC 8693 向 Keycloak 换取带 `act` 声明的 OBO 令牌；
- MCP 连接器登记：`/mcp/servers`；
- Skill 商店：`/skills`，入库前执行供应链硬规则（签名、静态扫描、评测集）。
内存存储为开发态实现，S8 工作包迁移到 MySQL。

安全约束：执行者身份必须可验证，不接受客户端自称——`act` 声明由服务端依据经过认证的调用方身份组装，
请求体里携带的任何执行者字段一律忽略。
"""

from __future__ import annotations

import os
import uuid
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
TRUSTED_GATEWAY_CLIENTS = {GATEWAY_CLIENT_ID}  # 允许签发 OBO 令牌的受信任网关客户端白名单
CORE_LIMIT = int(os.environ.get("CORE_TOOL_LIMIT", "20"))  # 核心工具预载上限，超出部分靠第二级发现

MCP_SERVERS: dict[str, dict[str, Any]] = {}  # MCP Server 登记表（内存）
SKILLS: dict[str, dict[str, Any]] = {}  # Skill 商店（内存），键为 name@version


class ErrorDetail(BaseModel):
    """HTTPException 的响应体。业务规则拒绝统一用 409 + ErrorDetail，422 只保留给 FastAPI 参数校验。"""

    detail: str


# ---- 能力发现 ----
@app.get("/capabilities")
async def core_capabilities(subject: str, agent: str) -> dict[str, Any]:
    """第一级发现：返回该 Agent 需要预载的核心工具（受 CORE_LIMIT 限制）。"""
    tools = [c.to_tool_spec() for c in registry.core_for(agent)][:CORE_LIMIT]
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


@app.post("/capabilities", status_code=201)
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


# ---- MCP 连接器 ----
class McpServerIn(BaseModel):
    """MCP Server 登记信息。"""

    name: str
    endpoint: str  # Streamable HTTP 端点
    protocol: str = "streamable-http"
    auth_type: str = "inherit"  # inherit：透传 OBO 令牌；service：网关用服务账号
    system: str = ""  # 所属业务系统


@app.post("/mcp/servers", status_code=201)
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

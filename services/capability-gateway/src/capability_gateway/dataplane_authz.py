"""第三方数据面的转发前判定（ADR-0005 / D153）：``POST /authz/mcp-check``。

agentgateway ext_authz（及 POC 中的 ContextForge TOOL_PRE_INVOKE 插件）在把 ``tools/call`` 转发到 MCP Server 前同步回调本端点。
自研数据面 mcp_proxy 已于 S9 删除，本端点与判定函数 ``authorize()`` 独立保留，全环境挂载。

- 请求须带目标 Server 的登记名（server），按其登记的业务系统校验工具归属；未登记的 Server、工具不属于该系统、未登记的工具一律 deny；
- 与 ``/invoke`` 走同一个策略客户端（契约 1 的 ``/authz/check-batch``），主体与租户取自数据面已校验的令牌；
- 配置了 ``GATEWAY_AUTHZ_TOKEN`` 时须带相同的 ``X-Gateway-Authz-Token``（常量时间比较），否则 401；GATEWAY_ENV=prod 未配置时网关拒绝启动；
- allow / require_approval → 200（审批由 Runtime 闸口完成，网关是第二道防线只拦 deny），deny → 403，策略不可达或应答异常 → 503（失败关闭）。
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from capability_gateway import config, policy, state
from capability_gateway.policy import AgentRef, Principal

router = APIRouter()
CTX_MAX = 128  # agent 上下文（<agent_id>@<version>）的最大长度


async def authorize(tool: str, subject: str, tenant: str, agent: str | None, *, system: str) -> str:
    """转发前的同步授权判定（失败关闭）：返回 allow / require_approval / deny / error。

    system 为请求实际要转发到的 MCP Server 所属业务系统：工具名必须对应该系统的能力，否则 deny——防止对
    Server A 调用 Server B 的同名工具时按 B 的能力判 allow、请求却转发到 A。
    策略服务不可达或应答异常时返回 error（调用方据此拒绝），不回退任何本地基线。"""
    cap = await state.catalog.by_name(tool)
    if cap is None or not system or cap.system != system:
        return "deny"
    agent_id, _, agent_version = (agent or "").partition("@")
    req = policy.build_request(Principal(sub=subject, tenant=tenant), cap, AgentRef(agent_id, agent_version, tenant))
    (d,) = await policy.check_batch([req])
    return "error" if d.reason == policy.UNAVAILABLE else d.decision


class McpAuthzIn(BaseModel):
    """第三方数据面的转发前判定请求（主体与租户取自该数据面已校验的令牌）。"""

    server: str = Field(max_length=128)  # 目标连接器的登记名，据此校验工具归属
    tool: str = Field(max_length=128)
    subject: str = Field(max_length=128)
    tenant: str = Field(default="", max_length=64)
    agent: str | None = Field(default=None, max_length=CTX_MAX)


def require_authz_token(x_gateway_authz_token: Annotated[str | None, Header()] = None) -> None:
    """判定端点的服务间鉴权：配置了 GATEWAY_AUTHZ_TOKEN 时必须带相同的 X-Gateway-Authz-Token，否则 401。"""
    expected = config.GATEWAY_AUTHZ_TOKEN
    if expected and (not x_gateway_authz_token or not hmac.compare_digest(x_gateway_authz_token.encode(), expected.encode())):
        raise HTTPException(401, "gateway authz token required")


@router.post("/authz/mcp-check", include_in_schema=False, dependencies=[Depends(require_authz_token)])
async def mcp_authz_check(body: McpAuthzIn) -> JSONResponse:
    """ext_authz 回调：allow 与 require_approval → 200，deny → 403，策略不可达 → 503（调用方均按拒绝处理非 2xx）。"""
    entry = await state.catalog.active_connector(body.server)
    if entry is None:
        return JSONResponse({"decision": "deny"}, status_code=403)
    decision = await authorize(body.tool, body.subject, body.tenant, body.agent, system=entry.system)
    status = {"allow": 200, "require_approval": 200, "deny": 403}.get(decision, 503)
    return JSONResponse({"decision": decision}, status_code=status)

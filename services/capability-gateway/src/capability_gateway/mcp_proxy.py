"""MCP 数据面（ADR-0005 自研候选，S6.6）：`/mcp/{server}` 以 MCP streamable-http 流式透传到已登记的 MCP Server。

转发规则：
- 地址：`MCP_SERVERS[server]["endpoint"]` 即上游 streamable-http 端点（原样拼接，不追加路径）；未登记 → 404。
- 身份：客户端的 `Authorization` 与全部 `X-AI-*` 头一律不转发。配置了 KEYCLOAK_TOKEN_URL 时必须带终端用户令牌，
  网关以 RFC 8693 换取受众为该 Server 的 OBO 令牌（复用 main._obo_token 的 LRU 缓存）后放入 `Authorization`；
  未配置时为开发态，以 `X-AI-Subject` 的值转发为 `X-AI-Dev-Sub`（与 /invoke 的开发态一致）。
- 运行上下文（只由网关读取，不原样转发）：`X-AI-Agent`（`<agent_id>@<version>`，转发为 `X-AI-Act: agent:...`，
  MCP Server 只在令牌 azp 为本网关时采信）、`X-AI-Run-Id`（转发为同名头）、`X-AI-Call-Id`（只进 Span）。
- 策略：POST 体按 JSON-RPC 解析（单条或批量）。方法白名单：initialize、ping、tools/list、tools/call、notifications/*
  与客户端对服务端请求的应答（无 method、带 result / error）；resources/*、prompts/*、completion/* 等其他方法与
  畸形消息一律拒绝。每个 `tools/call` 在转发前同步做授权判定；工具必须属于目标 Server 的业务系统（能力的 system 与
  `MCP_SERVERS[server]["system"]` 一致，否则按未登记处理），未登记的工具、`deny`、策略服务不可达或应答异常都拒绝
  （失败关闭）。批量请求中任一条被拒即整批拒绝（对其中每个带 id 的请求返回 JSON-RPC 错误），请求不到达上游。
  `require_approval` 放行：审批由 Runtime 闸口完成，与 /invoke 的语义一致（网关是第二道防线，只拦 deny）。
  请求体无法解析为 JSON 时 400。
- 流式：上游响应（JSON 或 SSE）按原始字节边到边转，`Mcp-Session-Id` 等头双向透传；GET（服务端推送流）不设读超时。
- 可观测：每次转发一个 Span（父上下文取自入站 `traceparent`，出站 `traceparent` 换成本 Span），属性带 run_id、call_id、
  工具名与判定结果；设置了 OTEL_EXPORTER_OTLP_ENDPOINT 且进程尚无 TracerProvider 时首次使用懒安装 OTLP/HTTP 导出。
  令牌不进日志与 Span。

另提供 `POST /authz/mcp-check`：供第三方数据面（agentgateway ext_authz、ContextForge TOOL_PRE_INVOKE 插件）在转发前
同步回调的判定端点，与本代理共用同一判定函数；请求须带目标 Server 名（server，按其登记的 system 校验工具归属，
未登记的 Server 按 deny）。配置了 GATEWAY_AUTHZ_TOKEN 时须带相同的 `X-Gateway-Authz-Token`，否则 401（生产必须配置）。
allow / require_approval → 200，deny → 403，策略不可达 → 503。
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import importlib
import json
import os
from collections.abc import AsyncIterator
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

try:  # OTel 为软依赖：未安装时不产生 Span，traceparent 原样透传
    from opentelemetry import propagate, trace
    from opentelemetry.trace import SpanKind, Status, StatusCode
except ImportError:  # pragma: no cover
    trace = None  # type: ignore[assignment]

router = APIRouter()

BODY_LIMIT = int(os.environ.get("MCP_PROXY_BODY_LIMIT", str(4 * 1024 * 1024)))  # 单个 POST 体上限（字节）
POLICY_TIMEOUT_S = float(os.environ.get("MCP_PROXY_POLICY_TIMEOUT_S", "2"))  # 授权判定超时，超时即拒绝
RPC_DENIED = -32001  # JSON-RPC 错误码：策略拒绝
RPC_POLICY_UNAVAILABLE = -32002  # JSON-RPC 错误码：策略服务不可达（失败关闭）
RPC_METHOD_NOT_ALLOWED = -32601  # JSON-RPC 错误码：方法不在网关白名单内（或消息畸形）
# 允许经数据面转发的 MCP 方法（另有 notifications/* 与客户端应答）；tools/call 还要逐个做授权判定
ALLOWED_METHODS = frozenset({"initialize", "ping", "tools/list", "tools/call"})
CTX_MAX = 128  # 运行上下文头的最大长度

_HOP_BY_HOP = frozenset(
    {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}
)
# 入站不转发：逐跳头、主机与长度（由 httpx 重算）、客户端凭据、网关自身的管理头、traceparent（换成本 Span 后再注入）
_DROP_REQUEST = _HOP_BY_HOP | {
    "host",
    "content-length",
    "authorization",
    "cookie",
    "x-gateway-admin-token",
    "x-gateway-authz-token",
    "x-gateway-client",
    "traceparent",
    "tracestate",
}
_DROP_RESPONSE = _HOP_BY_HOP | {"date", "server"}  # date / server 由本服务自己的 HTTP 栈写

_clients: dict[tuple[int, int], httpx.AsyncClient] = {}  # (事件循环, 测试 transport) → 共享连接池
_tracing_ready = False


def _gw() -> Any:
    """延迟取网关主模块（main 在末尾挂载本路由，模块级导入会形成循环）。"""
    return importlib.import_module("capability_gateway.main")


def _http() -> httpx.AsyncClient:
    """进程内共享的上游连接池（保持长连接）；测试经 main.set_invoke_transport_for_tests 注入 transport。"""
    gw = _gw()
    transport = gw._invoke_transport
    key = (id(asyncio.get_running_loop()), id(transport))
    client = _clients.get(key)
    if client is None:
        timeout = httpx.Timeout(gw.INVOKE_TIMEOUT_S, connect=5.0)
        limits = httpx.Limits(max_connections=512, max_keepalive_connections=256)
        client = httpx.AsyncClient(timeout=timeout, limits=limits, transport=transport)
        _clients[key] = client
    return client


def _tracer() -> Any:
    """返回 Tracer；首次调用时若配置了 OTLP 端点且进程还没有 TracerProvider，就安装一个（批量异步导出）。"""
    global _tracing_ready
    if trace is None:
        return None
    if not _tracing_ready:
        _tracing_ready = True
        base = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        if base and isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            endpoint = base if "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT" in os.environ or base.endswith("/v1/traces") else f"{base}/v1/traces"
            provider = TracerProvider(resource=Resource.create({"service.name": "ai-capability-gateway"}))
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
            trace.set_tracer_provider(provider)
    return trace.get_tracer("ai-capability-gateway.mcp-proxy")


def _claims(token: str) -> dict[str, Any]:
    """读取 OBO 令牌的 claims（令牌由网关直接从 IdP 换得，不再验签）；非 JWT 时返回空字典。"""
    try:
        seg = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
    except (IndexError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


async def authorize(tool: str, subject: str, tenant: str, agent: str | None, *, system: str) -> str:
    """转发前的同步授权判定（失败关闭）：返回 allow / require_approval / deny / error。

    system 为请求实际要转发到的 MCP Server 所属业务系统：工具名必须对应该系统的能力，否则 deny——防止对
    Server A 调用 Server B 的同名工具时按 B 的能力判 allow、请求却转发到 A。
    与 main._authz 组装同样的策略请求，区别在于：主体租户取自令牌而不是能力本身；策略服务不可达或应答异常时
    返回 error（调用方据此拒绝），不回退到 Python 基线。"""
    gw = _gw()
    cap = gw.registry.by_name(tool)
    if cap is None or not system or cap.system != system:
        return "deny"  # 未登记的工具、或工具不属于目标 Server 的业务系统，一律拒绝
    body = {
        "subject": subject,
        "agent": agent,
        "capability": cap.id,
        "action": "invoke",
        "principal": {"tenant": tenant},
        "resource": {"system": cap.system, "risk_level": cap.risk_level, "data_class": cap.data_class, "tenant": cap.tenant},
        "context": {"approved": False, "taint": []},
    }
    try:
        r = await _http().post(f"{gw.POLICY_URL}/authz/check", json=body, timeout=POLICY_TIMEOUT_S)
        r.raise_for_status()
        decision = r.json()["decision"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return "error"
    return decision if decision in ("allow", "require_approval", "deny") else "error"


def _allowed_message(m: Any) -> bool:
    """单条 JSON-RPC 消息是否允许转发：白名单方法、notifications/*，或客户端对服务端请求的应答。"""
    if not isinstance(m, dict):
        return False
    method = m.get("method")
    if method is None:
        return "id" in m and ("result" in m or "error" in m)  # 应答（如 sampling / elicitation / roots 的结果）
    return isinstance(method, str) and (method in ALLOWED_METHODS or method.startswith("notifications/"))


def _tool_calls(body: bytes) -> tuple[list[tuple[Any, str]], bool] | None:
    """解析 JSON-RPC（单条或批量），返回（每个 tools/call 的（id, 工具名），是否全部消息都在白名单内）；
    不是合法 JSON 时返回 None。空批量按不允许处理。"""
    try:
        msg = json.loads(body)
    except ValueError:
        return None
    items = msg if isinstance(msg, list) else [msg]
    calls: list[tuple[Any, str]] = []
    for m in items:
        if isinstance(m, dict) and m.get("method") == "tools/call":
            params = m.get("params")
            name = params.get("name") if isinstance(params, dict) else None
            calls.append((m.get("id"), name if isinstance(name, str) else ""))
    return calls, bool(items) and all(_allowed_message(m) for m in items)


def _rpc_error(body: bytes, code: int, message: str, decision: str) -> JSONResponse:
    """对整条消息（含批量中的每个请求）返回 JSON-RPC 错误；HTTP 200，客户端按工具错误处理。"""
    msg = json.loads(body)
    ids = [m.get("id") for m in (msg if isinstance(msg, list) else [msg]) if isinstance(m, dict) and "id" in m]
    err = {"code": code, "message": message, "data": {"decision": decision}}
    errs = [{"jsonrpc": "2.0", "id": i, "error": err} for i in ids]
    if not errs:  # 只有通知或消息畸形（无 id）：返回一条 id 为 null 的错误
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": err}, status_code=200)
    return JSONResponse(errs if isinstance(msg, list) else errs[0], status_code=200)


async def _read_body(request: Request) -> bytes | None:
    """读取请求体，超过 BODY_LIMIT 返回 None。"""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > BODY_LIMIT:
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > BODY_LIMIT:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _ctx(request: Request, name: str) -> str | None:
    value = request.headers.get(name)
    return value[:CTX_MAX] if value else None


def _end(span: Any, status_code: int | None = None, error: str | None = None) -> None:
    if span is None:
        return
    if status_code is not None:
        span.set_attribute("http.response.status_code", status_code)
    if error:
        span.set_status(Status(StatusCode.ERROR, error))
    span.end()


@router.api_route("/mcp/{server}", methods=["GET", "POST", "DELETE"], include_in_schema=False)
async def mcp_proxy(server: str, request: Request) -> Response:
    """MCP streamable-http 透传（协议端点，不进 OpenAPI 契约）。"""
    gw = _gw()
    entry = gw.MCP_SERVERS.get(server)
    if entry is None:
        return JSONResponse({"detail": "unknown MCP server"}, status_code=404)
    body = b""
    if request.method == "POST":
        read = await _read_body(request)
        if read is None:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
        body = read
    parsed = _tool_calls(body) if body else ([], True)
    if parsed is None:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}, status_code=400)
    calls, methods_ok = parsed
    run_id, call_id, agent = _ctx(request, "x-ai-run-id"), _ctx(request, "x-ai-call-id"), _ctx(request, "x-ai-agent")

    span = None
    tracer = _tracer()
    if tracer is not None:
        parent = propagate.extract(dict(request.headers))
        span = tracer.start_span(f"mcp.proxy {calls[0][1] if calls else request.method}", context=parent, kind=SpanKind.SERVER)
        span.set_attribute("mcp.server", server)
        span.set_attribute("http.request.method", request.method)
        for k, v in (("ai.run_id", run_id), ("ai.call_id", call_id), ("ai.agent", agent)):
            if v:
                span.set_attribute(k, v)
        if calls:
            span.set_attribute("mcp.method.name", "tools/call")
            span.set_attribute("gen_ai.tool.name", ",".join(t for _, t in calls))
    try:
        client = _http()
        out = {k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQUEST and not k.lower().startswith("x-ai-")}
        if gw.KEYCLOAK_TOKEN_URL:
            auth = request.headers.get("authorization", "")
            if not auth.lower().startswith("bearer "):
                _end(span, 401, "missing end-user token")
                return JSONResponse({"detail": "end-user token required"}, status_code=401)
            token, _ = await gw._obo_token(client, auth.split(" ", 1)[1].strip(), entry["name"])
            claims = _claims(token)
            subject, tenant = str(claims.get("sub") or ""), str(claims.get("tenant") or "")
            out["Authorization"] = f"Bearer {token}"
        else:
            subject, tenant = _ctx(request, "x-ai-subject") or "", "default"
            if subject:
                out["X-AI-Dev-Sub"] = subject.split(":", 1)[-1]
        if agent:
            out["X-AI-Act"] = f"agent:{agent}"
        if run_id:
            out["X-AI-Run-Id"] = run_id

        if not methods_ok:
            if span is not None:
                span.set_attribute("ai.authz.decision", "deny")
            _end(span, 200, "method not allowed")
            return _rpc_error(body, RPC_METHOD_NOT_ALLOWED, "method not allowed by gateway", "deny")
        decisions: list[str] = []
        for _, tool in calls:
            decision = await authorize(tool, subject, tenant, agent, system=str(entry.get("system") or ""))
            decisions.append(decision)
            if decision in ("deny", "error"):
                break
        if span is not None and decisions:
            span.set_attribute("ai.authz.decision", decisions[-1])
        if decisions and decisions[-1] == "deny":
            _end(span, 200, "denied by policy")
            return _rpc_error(body, RPC_DENIED, "tool call denied by policy", "deny")
        if decisions and decisions[-1] == "error":
            _end(span, 200, "policy unavailable")
            return _rpc_error(body, RPC_POLICY_UNAVAILABLE, "policy service unavailable; request rejected", "error")

        if span is not None:
            propagate.inject(out, context=trace.set_span_in_context(span))
        else:
            for name in ("traceparent", "tracestate"):
                if name in request.headers:
                    out[name] = request.headers[name]
        # GET 是服务端推送的长连接 SSE 流，可以长时间无数据：不设读超时
        timeout = httpx.Timeout(gw.INVOKE_TIMEOUT_S, connect=5.0, read=None) if request.method == "GET" else client.timeout
        upstream_req = client.build_request(request.method, entry["endpoint"], headers=out, content=body or None, timeout=timeout)
        try:
            resp = await client.send(upstream_req, stream=True)
        except httpx.HTTPError as e:
            _end(span, 502, type(e).__name__)
            return JSONResponse({"detail": f"MCP server unreachable: {type(e).__name__}"}, status_code=502)
    except BaseException as e:
        _end(span, None, type(e).__name__)
        raise

    headers = {k: v for k, v in resp.headers.items() if k.lower() not in _DROP_RESPONSE}

    async def relay() -> AsyncIterator[bytes]:
        """原始字节边到边转；流结束、出错或客户端断开时都关闭上游响应并结束 Span。"""
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()
            _end(span, resp.status_code, None if resp.status_code < 500 else f"upstream HTTP {resp.status_code}")

    return StreamingResponse(relay(), status_code=resp.status_code, headers=headers)


class McpAuthzIn(BaseModel):
    """第三方数据面的转发前判定请求（主体与租户取自该数据面已校验的令牌）。"""

    server: str = Field(max_length=128)  # 目标 MCP Server 的登记名（MCP_SERVERS 的键），据此校验工具归属
    tool: str = Field(max_length=128)
    subject: str = Field(max_length=128)
    tenant: str = Field(default="", max_length=64)
    agent: str | None = Field(default=None, max_length=CTX_MAX)


def require_authz_token(x_gateway_authz_token: Annotated[str | None, Header()] = None) -> None:
    """判定端点的服务间鉴权：配置了 GATEWAY_AUTHZ_TOKEN 时必须带相同的 X-Gateway-Authz-Token（常量时间比较），
    否则 401；未配置时为开发态，不检查（GATEWAY_ENV=prod 未配置时网关拒绝启动）。"""
    expected = _gw().GATEWAY_AUTHZ_TOKEN
    if expected and (not x_gateway_authz_token or not hmac.compare_digest(x_gateway_authz_token.encode(), expected.encode())):
        raise HTTPException(401, "gateway authz token required")


@router.post("/authz/mcp-check", include_in_schema=False, dependencies=[Depends(require_authz_token)])
async def mcp_authz_check(body: McpAuthzIn) -> JSONResponse:
    """ext_authz / 插件回调：allow 与 require_approval → 200，deny → 403，策略不可达 → 503（调用方均按拒绝处理非 2xx）。
    目标 Server 未登记或工具不属于其业务系统 → deny。"""
    entry = _gw().MCP_SERVERS.get(body.server)
    if entry is None:
        return JSONResponse({"decision": "deny"}, status_code=403)
    decision = await authorize(body.tool, body.subject, body.tenant, body.agent, system=str(entry.get("system") or ""))
    status = {"allow": 200, "require_approval": 200, "deny": 403}.get(decision, 503)
    return JSONResponse({"decision": decision}, status_code=status)

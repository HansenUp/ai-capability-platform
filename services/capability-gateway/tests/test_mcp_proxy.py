"""MCP 数据面（ADR-0005 自研候选）：/mcp/{server} 流式透传、换 OBO 与剥离客户端身份头、转发前判定失败关闭、Span 与 traceparent。"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from capability_gateway import main, mcp_proxy

c = TestClient(main.app)
MCP_URL = "http://mcp.test/mcp"
SESSION = "sess-123"


def _jwt(claims: dict[str, Any]) -> str:
    """不签名的 JWT 形状令牌（网关只读 claims，不验签）。"""
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


OBO = _jwt({"sub": "user-1", "tenant": "default", "azp": "capability-gateway", "aud": "mcp-ticket"})


def rpc(method: str, rid: int = 1, **params: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}


class Fake:
    """替身 Keycloak 令牌端点 + 策略服务 + 上游 MCP Server（经 main 的测试 transport 注入）。"""

    def __init__(self) -> None:
        self.mcp: list[httpx.Request] = []
        self.policy: list[dict[str, Any]] = []
        self.token_calls = 0
        self.decision: str | None = "allow"  # None → 策略服务不可达
        self.sse = False
        self.stream: AsyncIterator[bytes] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            self.token_calls += 1
            return httpx.Response(200, json={"access_token": OBO, "expires_in": 300})
        if request.url.path == "/authz/check":
            if self.decision is None:
                raise httpx.ConnectError("policy down", request=request)
            self.policy.append(json.loads(request.content))
            return httpx.Response(200, json={"decision": self.decision, "decision_id": "d1", "matched": "x"})
        self.mcp.append(request)
        headers = {"Mcp-Session-Id": SESSION, "X-Upstream": "1", "Connection": "keep-alive"}
        if self.stream is not None:
            return httpx.Response(200, headers=headers | {"Content-Type": "text/event-stream"}, content=self.stream)
        # 响应体用异步迭代器给出：与真实连接一样是未读的流（bytes 内容会被 httpx 预先读入，无法 aiter_raw）
        if self.sse:
            body = b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"content":[]}}\n\n'
            return httpx.Response(200, headers=headers | {"Content-Type": "text/event-stream"}, content=_chunks(body))
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}).encode()
        return httpx.Response(200, headers=headers | {"Content-Type": "application/json"}, content=_chunks(body))


async def _chunks(body: bytes) -> AsyncIterator[bytes]:
    yield body


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[Fake]:
    f = Fake()
    monkeypatch.setattr(main, "KEYCLOAK_TOKEN_URL", "http://idp.test/token")
    monkeypatch.setattr(main, "POLICY_URL", "http://policy.test")
    monkeypatch.setitem(main.MCP_SERVERS, "mcp-ticket", main.MCP_SERVERS["mcp-ticket"] | {"endpoint": MCP_URL})
    main.set_invoke_transport_for_tests(httpx.MockTransport(f))
    yield f
    main.set_invoke_transport_for_tests(None)


HDRS = {
    "Authorization": "Bearer user-token",
    "Mcp-Session-Id": SESSION,
    "Accept": "application/json, text/event-stream",
    "X-AI-Agent": "ticket-summary@1",
    "X-AI-Run-Id": "run-1",
    "X-AI-Call-Id": "call-1",
    # 客户端自带的身份头与凭据：必须被剥离或覆盖
    "X-AI-Act": "agent:evil@9",
    "X-AI-Dev-Sub": "mallory",
    "X-AI-Dev-Tenant": "evil",
    "Cookie": "s=1",
    "X-Gateway-Admin-Token": "admin",
}


def test_forwards_with_obo_and_strips_client_identity(fake: Fake) -> None:
    raw = json.dumps(rpc("initialize", protocolVersion="2025-06-18")).encode()
    r = c.post("/mcp/mcp-ticket", content=raw, headers=HDRS | {"Content-Type": "application/json"})
    assert r.status_code == 200 and r.json()["result"] == {"ok": True}
    assert r.headers["mcp-session-id"] == SESSION and r.headers["x-upstream"] == "1"
    (up,) = fake.mcp
    assert str(up.url) == MCP_URL and up.method == "POST" and up.content == raw  # 请求体逐字节透传
    h = up.headers
    assert h["authorization"] == f"Bearer {OBO}"  # 用户令牌被换成 OBO 令牌
    assert h["x-ai-act"] == "agent:ticket-summary@1" and h["x-ai-run-id"] == "run-1"
    assert "x-ai-dev-sub" not in h and "x-ai-dev-tenant" not in h and "x-ai-call-id" not in h and "x-ai-agent" not in h
    assert "cookie" not in h and "x-gateway-admin-token" not in h
    assert h["mcp-session-id"] == SESSION and h["accept"] == "application/json, text/event-stream"
    assert fake.policy == []  # 非 tools/call 不做判定
    c.post("/mcp/mcp-ticket", json=rpc("tools/list"), headers=HDRS)
    assert fake.token_calls == 1  # OBO 令牌走 LRU 缓存


def test_tools_call_authorized_with_token_identity(fake: Fake) -> None:
    fake.sse = True
    r = c.post("/mcp/mcp-ticket", json=rpc("tools/call", name="ticket.search", arguments={"q": "x"}), headers=HDRS)
    assert r.status_code == 200 and r.headers["content-type"] == "text/event-stream"
    assert r.content.startswith(b"event: message\ndata: ")  # SSE 原样转发
    (req,) = fake.policy
    assert req["capability"] == "cap.ticket.search" and req["subject"] == "user-1" and req["agent"] == "ticket-summary@1"
    assert req["principal"] == {"tenant": "default"}  # 主体租户取自令牌，不取自能力
    assert len(fake.mcp) == 1


def test_require_approval_passes_like_invoke(fake: Fake) -> None:
    fake.decision = "require_approval"
    c.post("/mcp/mcp-ticket", json=rpc("tools/call", name="ticket.close_batch", arguments={}), headers=HDRS)
    assert len(fake.mcp) == 1


@pytest.mark.parametrize(
    ("decision", "tool", "code"),
    [("deny", "ticket.search", mcp_proxy.RPC_DENIED), ("allow", "no.such.tool", mcp_proxy.RPC_DENIED), (None, "ticket.search", -32002)],
)
def test_denied_or_policy_down_never_reaches_upstream(fake: Fake, decision: str | None, tool: str, code: int) -> None:
    fake.decision = decision
    r = c.post("/mcp/mcp-ticket", json=rpc("tools/call", rid=7, name=tool, arguments={}), headers=HDRS)
    assert r.status_code == 200 and r.json()["id"] == 7 and r.json()["error"]["code"] == code
    assert fake.mcp == []


def test_policy_error_status_fails_closed(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500) if request.url.path == "/authz/check" else fake(request)

    main.set_invoke_transport_for_tests(httpx.MockTransport(broken))
    r = c.post("/mcp/mcp-ticket", json=rpc("tools/call", name="ticket.search", arguments={}), headers=HDRS)
    assert r.json()["error"]["code"] == mcp_proxy.RPC_POLICY_UNAVAILABLE and fake.mcp == []


def test_batch_with_one_denied_tool_is_rejected_entirely(fake: Fake) -> None:
    batch = [rpc("tools/call", rid=1, name="ticket.search", arguments={}), rpc("tools/call", rid=2, name="no.such", arguments={})]
    r = c.post("/mcp/mcp-ticket", json=batch, headers=HDRS)
    assert [e["id"] for e in r.json()] == [1, 2] and all(e["error"]["code"] == mcp_proxy.RPC_DENIED for e in r.json())
    assert fake.mcp == []


def test_rejections(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    assert c.post("/mcp/mcp-ticket", json=rpc("initialize")).status_code == 401  # 缺终端用户令牌
    assert c.post("/mcp/nope", json=rpc("initialize"), headers=HDRS).status_code == 404
    assert c.post("/mcp/mcp-ticket", content=b"{not json", headers=HDRS).status_code == 400
    monkeypatch.setattr(mcp_proxy, "BODY_LIMIT", 10)
    assert c.post("/mcp/mcp-ticket", json=rpc("initialize"), headers=HDRS).status_code == 413
    assert fake.mcp == []
    assert c.get("/mcp/servers").status_code == 200  # 登记列表路由不被代理路由遮蔽


def test_get_and_delete_are_proxied(fake: Fake) -> None:
    fake.sse = True
    assert c.get("/mcp/mcp-ticket", headers=HDRS).status_code == 200
    assert c.delete("/mcp/mcp-ticket", headers=HDRS).status_code == 200
    assert [r.method for r in fake.mcp] == ["GET", "DELETE"] and all(r.headers["authorization"] == f"Bearer {OBO}" for r in fake.mcp)


def test_upstream_unreachable_is_502(fake: Fake) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        if request.url.host == "mcp.test":
            raise httpx.ConnectError("down", request=request)
        return fake(request)

    main.set_invoke_transport_for_tests(httpx.MockTransport(down))
    assert c.post("/mcp/mcp-ticket", json=rpc("initialize"), headers=HDRS).status_code == 502


def test_dev_mode_forwards_subject_not_client_token(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "KEYCLOAK_TOKEN_URL", None)
    c.post("/mcp/mcp-ticket", json=rpc("initialize"), headers=HDRS | {"X-AI-Subject": "human:u1"})
    h = fake.mcp[0].headers
    assert h["x-ai-dev-sub"] == "u1" and "authorization" not in h and fake.token_calls == 0


@pytest.fixture
def hr_server(monkeypatch: pytest.MonkeyPatch) -> str:
    """另一个业务系统（hr）的 MCP Server；能力目录里没有 hr 的工具。"""
    monkeypatch.setitem(main.MCP_SERVERS, "mcp-hr", {"name": "mcp-hr", "endpoint": "http://hr.test/mcp", "system": "hr"})
    return "mcp-hr"


@pytest.mark.parametrize("batch", [False, True])
def test_cross_server_tool_is_denied_and_never_reaches_upstream(fake: Fake, hr_server: str, batch: bool) -> None:
    """对 hr Server 调用工单系统的同名工具：不能按 ticket 能力判 allow 后转发到 hr 上游（策略服务也不会被问到）。"""
    call = rpc("tools/call", rid=3, name="ticket.search", arguments={})
    r = c.post(f"/mcp/{hr_server}", json=[call] if batch else call, headers=HDRS)
    err = r.json()[0] if batch else r.json()
    assert r.status_code == 200 and err["id"] == 3 and err["error"]["code"] == mcp_proxy.RPC_DENIED
    assert fake.mcp == [] and fake.policy == []
    # 同一工具对其所属 Server 照常放行
    assert c.post("/mcp/mcp-ticket", json=call, headers=HDRS).status_code == 200 and len(fake.mcp) == 1


@pytest.mark.parametrize(
    "msg",
    [
        rpc("resources/read", uri="file:///etc/passwd"),
        rpc("prompts/get", name="p"),
        rpc("resources/list"),
        rpc("completion/complete"),
        rpc("no/such/method"),
        {"jsonrpc": "2.0", "id": 1, "method": 42},
        {"jsonrpc": "2.0", "id": 1},  # 既不是请求也不是应答
        [],  # 空批量
        [rpc("tools/list", rid=1), rpc("resources/read", rid=2, uri="x")],  # 批量中混入不允许的方法：整批拒绝
        [rpc("tools/list", rid=1), "junk"],
    ],
)
def test_methods_outside_whitelist_are_rejected(fake: Fake, msg: Any) -> None:
    r = c.post("/mcp/mcp-ticket", json=msg, headers=HDRS)
    assert r.status_code == 200 and fake.mcp == [] and fake.policy == []
    errs = r.json() if isinstance(r.json(), list) else [r.json()]
    assert errs and all(e["error"]["code"] == mcp_proxy.RPC_METHOD_NOT_ALLOWED for e in errs)
    if isinstance(msg, list) and any(isinstance(m, dict) for m in msg):
        assert [e["id"] for e in errs] == [m["id"] for m in msg if isinstance(m, dict)]


@pytest.mark.parametrize(
    "msg",
    [
        rpc("ping"),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": "srv-1", "result": {"roots": []}},  # 客户端对服务端请求的应答
        [rpc("tools/list", rid=1), {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 0}}],
    ],
)
def test_whitelisted_methods_are_forwarded(fake: Fake, msg: Any) -> None:
    assert c.post("/mcp/mcp-ticket", json=msg, headers=HDRS).status_code == 200
    assert len(fake.mcp) == 1 and fake.policy == []


def test_mcp_check_endpoint(fake: Fake, hr_server: str) -> None:
    body = {"server": "mcp-ticket", "tool": "ticket.search", "subject": "user-1", "tenant": "t2", "agent": "a@1"}
    assert c.post("/authz/mcp-check", json=body).json() == {"decision": "allow"}
    assert fake.policy[-1]["principal"] == {"tenant": "t2"}
    fake.decision = "deny"
    assert c.post("/authz/mcp-check", json=body).status_code == 403
    fake.decision = None
    assert c.post("/authz/mcp-check", json=body).status_code == 503
    assert c.post("/authz/mcp-check", json=body | {"tool": "no.such"}).status_code == 403
    fake.decision = "allow"
    n = len(fake.policy)
    # 跨 Server 同名工具、未登记的 Server：deny，且不问策略服务；server 为必填
    r = c.post("/authz/mcp-check", json=body | {"server": hr_server})
    assert r.status_code == 403 and r.json() == {"decision": "deny"}
    assert c.post("/authz/mcp-check", json=body | {"server": "no-such"}).status_code == 403
    assert len(fake.policy) == n
    assert c.post("/authz/mcp-check", json={k: v for k, v in body.items() if k != "server"}).status_code == 422


def test_mcp_check_requires_shared_token_when_configured(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "GATEWAY_AUTHZ_TOKEN", "authz-secret")
    body = {"server": "mcp-ticket", "tool": "ticket.search", "subject": "user-1"}
    assert c.post("/authz/mcp-check", json=body).status_code == 401
    assert c.post("/authz/mcp-check", json=body, headers={"X-Gateway-Authz-Token": "wrong"}).status_code == 401
    assert c.post("/authz/mcp-check", json=body, headers={"X-Gateway-Authz-Token": "authz-secret"}).status_code == 200
    assert fake.policy and len(fake.policy) == 1  # 未通过鉴权的请求不到达策略服务
    # 代理不把该头转发给上游
    c.post("/mcp/mcp-ticket", json=rpc("initialize"), headers=HDRS | {"X-Gateway-Authz-Token": "authz-secret"})
    assert "x-gateway-authz-token" not in fake.mcp[-1].headers


def test_span_and_traceparent(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(mcp_proxy, "_tracing_ready", True)
    trace_id, parent = "0af7651916cd43dd8448eb211c80319c", "b7ad6b7169203331"
    hdrs = HDRS | {"traceparent": f"00-{trace_id}-{parent}-01"}
    c.post("/mcp/mcp-ticket", json=rpc("tools/call", name="ticket.search", arguments={}), headers=hdrs)
    fake.decision = "deny"
    c.post("/mcp/mcp-ticket", json=rpc("tools/call", name="ticket.search", arguments={}), headers=hdrs)
    spans = [s for s in exporter.get_finished_spans() if s.name.startswith("mcp.proxy")]
    assert len(spans) == 2
    ok, denied = spans
    a = ok.attributes or {}
    assert a["ai.run_id"] == "run-1" and a["ai.call_id"] == "call-1" and a["gen_ai.tool.name"] == "ticket.search"
    assert a["ai.authz.decision"] == "allow" and (denied.attributes or {})["ai.authz.decision"] == "deny"
    assert format(ok.context.trace_id, "032x") == trace_id and ok.parent is not None and format(ok.parent.span_id, "016x") == parent
    sent = fake.mcp[0].headers["traceparent"]  # 出站 traceparent：同一 Trace，父 span 换成网关 Span
    assert sent.split("-")[1] == trace_id and sent.split("-")[2] == format(ok.context.span_id, "016x")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_sse_is_relayed_incrementally(fake: Fake) -> None:
    """真实 HTTP 服务下验证边到边转发：客户端读到第一帧之前，上游还没有产出第二帧。"""
    first_seen = threading.Event()

    async def upstream_stream() -> AsyncIterator[bytes]:
        yield b'event: message\ndata: {"progress":1}\n\n'
        await asyncio.to_thread(first_seen.wait, 5)
        yield b'event: message\ndata: {"done":true}\n\n'

    fake.stream = upstream_stream()
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.02)
        body = rpc("tools/call", name="ticket.search", arguments={})
        t0 = time.monotonic()
        with httpx.stream("POST", f"http://127.0.0.1:{port}/mcp/mcp-ticket", json=body, headers=HDRS, timeout=10) as r:
            lines = r.iter_lines()
            first = next(line for line in lines if line.startswith("data:"))
            assert first == 'data: {"progress":1}' and time.monotonic() - t0 < 3  # 上游最多等 5 秒才出第二帧
            first_seen.set()
            rest = [line for line in lines if line.startswith("data:")]
        assert rest == ['data: {"done":true}']
    finally:
        server.should_exit = True
        t.join(timeout=5)

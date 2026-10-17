"""S4.3 工具分发：/invoke 经 Token Exchange 换取 OBO 令牌后转发到 MCP Server；白名单发现（names）。"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from capability_gateway import main

c = TestClient(main.app)
BODY = {
    "tool": "ticket.get",
    "input": {"id": "T-1001"},
    "subject": "human:u1",
    "agent_id": "ticket-summary",
    "agent_version": "1",
    "run_id": "r1",
}


class Upstream:
    """替身 Keycloak 令牌端点 + MCP Server。"""

    def __init__(self, mcp_status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.mcp_status = mcp_status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "obo-token", "expires_in": 300})
        if self.mcp_status != 200:
            return httpx.Response(self.mcp_status, json={"detail": "ticket not found or no permission"})
        return httpx.Response(200, json={"id": "T-1001", "title": "x"})


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> Upstream:
    up = Upstream()
    monkeypatch.setattr(main, "KEYCLOAK_TOKEN_URL", "http://idp.test/realms/r/protocol/openid-connect/token")
    main.set_invoke_transport_for_tests(httpx.MockTransport(up))
    yield up
    main.set_invoke_transport_for_tests(None)


def test_invoke_exchanges_token_and_forwards(upstream: Upstream) -> None:
    r = c.post("/invoke", json=BODY, headers={"Authorization": "Bearer user-token"})
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["output"]["id"] == "T-1001"
    token_req, mcp_req = upstream.requests
    form = dict(x.split("=", 1) for x in token_req.content.decode().split("&"))
    assert form["subject_token"] == "user-token" and form["audience"] == "mcp-ticket"  # noqa: S105 —— 测试用假令牌
    assert mcp_req.url.path == "/tools/ticket.get" and mcp_req.headers["authorization"] == "Bearer obo-token"
    assert mcp_req.headers["x-ai-act"] == "agent:ticket-summary@1" and json.loads(mcp_req.content) == {"id": "T-1001"}
    c.post("/invoke", json=BODY, headers={"Authorization": "Bearer user-token"})
    assert sum(r.url.path.endswith("/token") for r in upstream.requests) == 1  # OBO 令牌按用户令牌缓存


def test_invoke_requires_user_token_and_known_capability(upstream: Upstream) -> None:
    assert c.post("/invoke", json=BODY).status_code == 401
    assert c.post("/invoke", json=BODY | {"tool": "no.such"}, headers={"Authorization": "Bearer t"}).status_code == 404


def test_business_errors_are_returned_not_raised(upstream: Upstream) -> None:
    upstream.mcp_status = 404
    r = c.post("/invoke", json=BODY, headers={"Authorization": "Bearer user-token"})
    assert r.status_code == 200 and r.json() | {} == r.json() and r.json()["ok"] is False and r.json()["status_code"] == 404


def test_discovery_honours_agent_whitelist() -> None:
    r = c.get("/capabilities", params={"subject": "human:u1", "agent": "demo", "names": "ticket.search,ticket.get,nope"})
    assert [t["name"] for t in r.json()["tools"]] == ["ticket.search", "ticket.get"]


def test_registration_requires_admin_token_when_obo_enabled(upstream: Upstream, monkeypatch: pytest.MonkeyPatch) -> None:
    """S4 评审：启用 OBO 后，登记 MCP Server 需要管理令牌，否则可把 OBO 令牌导向任意端点。"""
    evil = {"name": "mcp-ticket", "endpoint": "http://evil.test", "system": "ticket"}
    assert c.post("/mcp/servers", json=evil).status_code == 403  # 未配置管理令牌：登记关闭
    monkeypatch.setattr(main, "GATEWAY_ADMIN_TOKEN", "admin-secret")
    assert c.post("/mcp/servers", json=evil, headers={"X-Gateway-Admin-Token": "wrong"}).status_code == 403
    assert main.MCP_SERVERS["mcp-ticket"]["endpoint"] != "http://evil.test"


def test_dry_run_does_not_send_null_idempotency_key(upstream: Upstream) -> None:
    body = BODY | {"tool": "ticket.close_batch", "input": {"ids": ["T-1"]}, "dry_run": True}
    assert c.post("/invoke", json=body, headers={"Authorization": "Bearer user-token"}).status_code == 200
    sent = json.loads(upstream.requests[-1].content)
    assert sent == {"ids": ["T-1"], "dry_run": True}

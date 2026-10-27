"""第三方数据面判定回调 ``/authz/mcp-check``（dataplane_authz，D153）回归；自研 MCP 数据面删除后 ``/mcp/*`` 代理为 404；
JWKS 刷新失败沿用旧公钥的上限（S8 遗留）。"""

from __future__ import annotations

import time

import httpx
import pytest
from fastapi.testclient import TestClient
from gateway_testkit import Upstream

from capability_gateway import auth, config, main, state
from capability_gateway.catalog import ConnectorVersion

c = TestClient(main.app)
BODY = {"server": "mcp-ticket", "tool": "ticket.search", "subject": "user-1", "tenant": "default", "agent": "a@1"}


@pytest.fixture
async def servers(up: Upstream) -> Upstream:
    await state.catalog.put_connector(
        ConnectorVersion(name="mcp-ticket", endpoint="http://mcp.test/mcp", protocol="mcp", system="ticket", status="active")
    )
    await state.catalog.put_connector(
        ConnectorVersion(name="mcp-hr", endpoint="http://hr.test/mcp", protocol="mcp", system="hr", status="active")
    )
    return up


def test_mcp_check_decisions(servers: Upstream) -> None:
    up = servers
    r = c.post("/authz/mcp-check", json=BODY | {"tenant": "t2"})
    assert r.status_code == 200 and r.json() == {"decision": "allow"}
    assert up.policy[-1]["principal"] == {"sub": "user-1", "tenant": "t2", "roles": []}
    assert up.policy[-1]["context"]["agent"] == {"id": "a", "version": "1", "tenant": "t2"}
    up.decisions["cap.ticket.close_batch"] = "require_approval"
    assert c.post("/authz/mcp-check", json=BODY | {"tool": "ticket.close_batch"}).json() == {"decision": "require_approval"}
    up.decisions["cap.ticket.search"] = "deny"
    assert c.post("/authz/mcp-check", json=BODY).status_code == 403
    up.policy_down = True
    r = c.post("/authz/mcp-check", json=BODY)
    assert r.status_code == 503 and r.json() == {"decision": "error"}  # 策略不可达：失败关闭
    up.policy_down = False
    n = len(up.policy)
    # 未登记的工具、跨 Server 同名工具、未登记的 Server：deny，且不问策略服务
    for bad in ({"tool": "no.such"}, {"server": "mcp-hr"}, {"server": "no-such"}):
        r = c.post("/authz/mcp-check", json=BODY | bad)
        assert r.status_code == 403 and r.json() == {"decision": "deny"}, bad
    assert len(up.policy) == n
    assert c.post("/authz/mcp-check", json={k: v for k, v in BODY.items() if k != "server"}).status_code == 422


def test_mcp_check_requires_shared_token_when_configured(servers: Upstream, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "GATEWAY_AUTHZ_TOKEN", "authz-secret")
    assert c.post("/authz/mcp-check", json=BODY).status_code == 401
    assert c.post("/authz/mcp-check", json=BODY, headers={"X-Gateway-Authz-Token": "wrong"}).status_code == 401
    assert servers.policy == []  # 未通过鉴权的请求不到达策略服务
    assert c.post("/authz/mcp-check", json=BODY, headers={"X-Gateway-Authz-Token": "authz-secret"}).status_code == 200


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_mcp_proxy_removed(servers: Upstream, method: str) -> None:
    r = c.request(method, "/mcp/mcp-ticket", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"} if method == "POST" else None)
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    assert c.get("/mcp/servers").status_code == 200  # 连接器登记接口不受影响


async def test_jwks_stale_keys_are_capped(keycloak: Upstream, monkeypatch: pytest.MonkeyPatch) -> None:
    up = keycloak
    token = up.idp.service_token()
    assert await auth.resolve_caller_client(f"Bearer {token}") == "ai-runtime-svc"
    cache = auth._cache()
    real = main.config.transport

    def down(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/certs"):
            raise httpx.ConnectError("idp down", request=request)
        return up(request)

    config.transport = httpx.MockTransport(down)
    try:
        cache._fetched_at -= config.JWKS_TTL_S + 1  # TTL 已过、刷新失败：在上限内沿用旧公钥
        cache._attempted_at = float("-inf")
        assert await auth.resolve_caller_client(f"Bearer {token}") == "ai-runtime-svc"
        cache._fetched_at = time.monotonic() - config.JWKS_MAX_STALE_S - 1  # 超过上限：不再信任旧公钥
        cache._attempted_at = float("-inf")
        with pytest.raises(auth.GatewayError) as e:
            await auth.resolve_caller_client(f"Bearer {token}")
        assert e.value.status_code == 503
    finally:
        config.transport = real

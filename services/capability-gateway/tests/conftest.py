"""网关测试的公共夹具：每个测试使用全新的内存目录、无 ES、开发态鉴权，测试结束由 monkeypatch 还原。"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from gateway_testkit import CONNECTOR_URL, JWKS_URL, TOKEN_URL, Upstream
from gateway_testkit import ISSUER as TEST_ISSUER

from capability_gateway import auth, config, main, state
from capability_gateway.catalog import ConnectorVersion, MemoryCatalog


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(state, "catalog", MemoryCatalog())
    monkeypatch.setattr(state, "es", None)
    monkeypatch.setattr(config, "KEYCLOAK_TOKEN_URL", None)
    monkeypatch.setattr(config, "POLICY_URL", "http://policy.test")
    monkeypatch.setattr(config, "GATEWAY_ADMIN_TOKEN", "")
    monkeypatch.setattr(config, "GATEWAY_AUTHZ_TOKEN", "")
    monkeypatch.setattr(main, "SKILLS", {})
    main.set_invoke_transport_for_tests(None)
    yield
    main.set_invoke_transport_for_tests(None)


@pytest.fixture
def up(monkeypatch: pytest.MonkeyPatch) -> Iterator[Upstream]:
    """替身上游（开发态）：策略服务、连接器 mcp-ticket 指向 ticket.test、ES 替身（默认不启用）。"""
    u = Upstream()
    main.set_invoke_transport_for_tests(httpx.MockTransport(u))
    yield u
    main.set_invoke_transport_for_tests(None)


@pytest.fixture
async def ticket_connector(up: Upstream) -> ConnectorVersion:
    return await state.catalog.put_connector(ConnectorVersion(name="mcp-ticket", endpoint=CONNECTOR_URL, system="ticket", status="active"))


@pytest.fixture
def keycloak(monkeypatch: pytest.MonkeyPatch, up: Upstream) -> Upstream:
    """已配置 Keycloak：服务令牌 + 用户令牌验签、OBO 换取。"""
    monkeypatch.setattr(config, "KEYCLOAK_TOKEN_URL", TOKEN_URL)
    monkeypatch.setattr(config, "JWT_ISSUER", TEST_ISSUER)
    monkeypatch.setattr(config, "JWKS_URL", JWKS_URL)
    monkeypatch.setattr(config, "SERVICE_AUDIENCE", "capability-gateway")
    monkeypatch.setattr(config, "USER_AUDIENCE", frozenset({"ai-runtime"}))
    monkeypatch.setattr(config, "TRUSTED_CALLERS", frozenset({"ai-runtime-svc"}))
    auth.reset_for_tests()
    return up

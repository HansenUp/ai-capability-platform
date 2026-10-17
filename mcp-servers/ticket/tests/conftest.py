"""ticket MCP Server 测试夹具：每个用例独立的 SQLite、固定的 IdP 配置，环境变量经 monkeypatch 设置（用例结束自动还原）。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ticket_testkit import AUDIENCE, ISSUER, JWKS_URL, IdP

from mcp_ticket import auth
from mcp_ticket.main import app


@pytest.fixture(autouse=True)
def ticket_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """每个用例使用独立数据库；清除可能影响行为的外部环境变量。"""
    monkeypatch.setenv("MCP_TICKET_DB", str(tmp_path / "ticket.db"))
    monkeypatch.setenv("MCP_TICKET_JWKS_URL", JWKS_URL)
    monkeypatch.setenv("MCP_TICKET_ISSUER", ISSUER)
    monkeypatch.setenv("MCP_TICKET_AUDIENCE", AUDIENCE)
    for name in ("MCP_TICKET_DATA", "MCP_TICKET_DEV_AUTH", "MCP_TICKET_TRUSTED_AZP"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def idp() -> Iterator[IdP]:
    """测试 IdP：JWKS 经 MockTransport 提供。"""
    provider = IdP()
    auth.set_transport_for_tests(provider.transport())
    yield provider
    auth.set_transport_for_tests(None)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)

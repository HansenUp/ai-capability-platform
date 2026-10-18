"""模板测试夹具：独立 SQLite、生产态鉴权环境、假 IdP 与假网关的 JWKS。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mcp_template import jwks
from mcp_template.act import replay_cache
from mcp_template.main import app
from mcp_template.testing import FakeGateway, FakeIdP, auth_env, jwks_transport


@pytest.fixture(autouse=True)
def template_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MCP_TEMPLATE_DB", str(tmp_path / "template.db"))
    for k, v in auth_env("MCP_TEMPLATE", "mcp-template").items():
        monkeypatch.setenv(k, v)
    for name in ("MCP_TEMPLATE_DEV_AUTH", "MCP_TEMPLATE_TRUSTED_AZP", "MCP_TEMPLATE_ACT_ISSUER", "MCP_TEMPLATE_ACT_AUDIENCE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def idp() -> FakeIdP:
    return FakeIdP()


@pytest.fixture
def gw() -> FakeGateway:
    return FakeGateway()


@pytest.fixture(autouse=True)
def fake_jwks(idp: FakeIdP, gw: FakeGateway) -> Iterator[None]:
    jwks.set_transport_for_tests(jwks_transport(idp, gw))
    replay_cache.clear()
    yield
    jwks.set_transport_for_tests(None)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)

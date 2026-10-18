"""mock 连接器测试夹具：生产态鉴权环境（假 IdP + 假网关的 JWKS）、独立 SQLite、每例清空审计。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mcp_mocks import crm, knowledge, mail
from mcp_template import jwks
from mcp_template.act import replay_cache
from mcp_template.testing import FakeGateway, FakeIdP, auth_env, jwks_transport, obo_headers

PREFIXES = {"MCP_KNOWLEDGE": "mcp-knowledge", "MCP_CRM": "mcp-crm", "MCP_MAIL": "mcp-mail"}


@pytest.fixture(autouse=True)
def mock_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for prefix, aud in PREFIXES.items():
        for k, v in auth_env(prefix, aud).items():
            monkeypatch.setenv(k, v)
        for suffix in ("DEV_AUTH", "TRUSTED_AZP", "ACT_ISSUER", "ACT_AUDIENCE"):
            monkeypatch.delenv(f"{prefix}_{suffix}", raising=False)
    monkeypatch.setenv("MCP_MAIL_DB", str(tmp_path / "mail.db"))
    for name in ("MCP_KNOWLEDGE_DATA", "MCP_CRM_DATA", "MCP_MAIL_INTERNAL_DOMAINS"):
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
    for c in (knowledge.connector, crm.connector, mail.connector):
        c._calls.clear()
    yield
    jwks.set_transport_for_tests(None)


@pytest.fixture
def auth(idp: FakeIdP, gw: FakeGateway) -> Callable[..., dict[str, str]]:
    """生产态请求头工厂：auth(aud, sub=..., roles=..., tenant=..., agent=...)，每次调用生成新断言。"""

    def make(aud: str, **kw: object) -> dict[str, str]:
        return obo_headers(idp, gw, aud=aud, **kw)  # type: ignore[arg-type]

    return make


@pytest.fixture
def kclient() -> TestClient:
    return TestClient(knowledge.app)


@pytest.fixture
def cclient() -> TestClient:
    return TestClient(crm.app)


@pytest.fixture
def mclient() -> TestClient:
    return TestClient(mail.app)

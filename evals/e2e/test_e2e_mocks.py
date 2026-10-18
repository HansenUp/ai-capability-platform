"""mock 连接器的进程级自测（不需要全栈）：真实子进程、只绑 127.0.0.1、生产态鉴权经真实 HTTP 拉 JWKS。

本机起一个小 HTTP 服务充当 IdP 与能力网关的 JWKS（假密钥自签），三个 mock 以 ``python -m mcp_mocks`` 启动，
校验：healthz / manifest 写规范（含与网关 ``spec.validate_tool`` 的兼容）、OBO + act 断言验签、ACL / 行级过滤、
mail 发件箱计数（dry-run 0 → 执行 1 → 重放仍 1）与调用审计里的 act 链。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from mockstack import RunningMock, free_port, mock_stack

from mcp_template.spec import validate_manifest
from mcp_template.testing import ALICE, BOB, ISSUER, FakeGateway, FakeIdP, obo_headers

AUDS = {"knowledge": "mcp-knowledge", "crm": "mcp-crm", "mail": "mcp-mail"}


@pytest.fixture(scope="module")
def keys() -> tuple[FakeIdP, FakeGateway]:
    return FakeIdP(), FakeGateway()


@pytest.fixture(scope="module")
def jwks_server(keys: tuple[FakeIdP, FakeGateway]) -> Iterator[str]:
    idp, gw = keys
    docs = {"/idp": json.dumps(idp.jwks()).encode(), "/gw": json.dumps(gw.jwks()).encode()}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 —— http.server 约定的方法名
            body = docs.get(self.path)
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body or b"{}")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 —— 覆盖基类签名
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="module")
def mocks(jwks_server: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, RunningMock]]:
    tmp = tmp_path_factory.mktemp("mocks")
    env = {"MCP_MAIL_DB": str(tmp / "mail.db")}
    for name, aud in AUDS.items():
        prefix = f"MCP_{name.upper()}"
        env |= {
            f"{prefix}_JWKS_URL": f"{jwks_server}/idp",
            f"{prefix}_ISSUER": ISSUER,
            f"{prefix}_AUDIENCE": aud,
            f"{prefix}_ACT_JWKS_URL": f"{jwks_server}/gw",
            f"{prefix}_DEV_AUTH": "false",
        }
    with mock_stack(env, log_dir=tmp / "logs", ports={name: free_port() for name in AUDS}) as running:
        yield running


def _post(mock: RunningMock, tool: str, body: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
    return httpx.post(f"{mock.url}/tools/{tool}", json=body, headers=headers, timeout=10)


def test_processes_bind_loopback_and_serve_valid_manifests(mocks: dict[str, RunningMock]) -> None:
    for name, mock in mocks.items():
        assert f"Uvicorn running on http://127.0.0.1:{mock.port}" in mock.log.read_text(encoding="utf-8", errors="replace")
        assert httpx.get(f"{mock.url}/healthz", timeout=5).json() == {"status": "ok"}
        manifest = httpx.get(f"{mock.url}/tools", timeout=5).json()
        assert validate_manifest(manifest) == [], name


def test_manifests_pass_gateway_write_spec() -> None:
    """与网关 ``spec.validate_tool``（S8.5）兼容：四个连接器的 manifest 不应有任何不合规工具。"""
    spec = pytest.importorskip("capability_gateway.spec")
    from mcp_mocks import crm, knowledge, mail
    from mcp_ticket.tools import TOOL_MANIFEST as TICKET

    for manifest in (TICKET, knowledge.TOOL_MANIFEST, crm.TOOL_MANIFEST, mail.TOOL_MANIFEST):
        assert [e for t in manifest for e in spec.validate_tool(t, manifest)] == []


def test_production_auth_over_real_http(mocks: dict[str, RunningMock], keys: tuple[FakeIdP, FakeGateway]) -> None:
    idp, gw = keys
    kb = mocks["knowledge"]
    assert _post(kb, "knowledge.search", {"query": "GW-200"}, {}).status_code == 401
    bare = {"Authorization": f"Bearer {idp.token(ALICE, aud='mcp-knowledge')}", "X-AI-Act": "agent:x@1"}
    assert _post(kb, "knowledge.search", {"query": "GW-200"}, bare).status_code == 401  # 生产态缺断言
    wrong_aud = obo_headers(idp, gw, aud="mcp-crm")
    assert _post(kb, "knowledge.search", {"query": "GW-200"}, wrong_aud).status_code == 401
    r = _post(kb, "knowledge.search", {"query": "GW-200", "limit": 20}, obo_headers(idp, gw, aud="mcp-knowledge", roles=("sales",)))
    assert r.status_code == 200 and {i["id"] for i in r.json()["items"]} == {"K-001", "K-003", "K-004", "K-006"}
    call = httpx.get(f"{kb.url}/_mock/calls", timeout=5).json()[-1]
    assert call["act_verified"] is True and call["act_chain"] == ["agent:demo@1", "client:ai-runtime-svc"]
    assert call["audience"] == "mcp-knowledge"


def test_crm_rows_and_confidential(mocks: dict[str, RunningMock], keys: tuple[FakeIdP, FakeGateway]) -> None:
    idp, gw = keys
    out = _post(mocks["crm"], "crm.opportunity.search", {}, obo_headers(idp, gw, aud="mcp-crm", sub=ALICE, roles=("sales",))).json()
    assert {i["id"] for i in out["items"]} == {"O-3001", "O-3002", "O-3005"} and out["data_class"] == "confidential"
    assert _post(mocks["crm"], "crm.opportunity.search", {}, obo_headers(idp, gw, aud="mcp-crm", sub=BOB)).json()["total"] == 0


def test_mail_outbox_counts_exactly_once(mocks: dict[str, RunningMock], keys: tuple[FakeIdP, FakeGateway]) -> None:
    idp, gw = keys
    mail = mocks["mail"]
    httpx.post(f"{mail.url}/_mock/reset", timeout=5)
    count = lambda: httpx.get(f"{mail.url}/_mock/outbox", timeout=5).json()["count"]  # noqa: E731
    msg = {"to": ["customer@client.example"], "subject": "商机跟进", "body": "附上报价。"}
    h = lambda: obo_headers(idp, gw, aud="mcp-mail", agent="agent:sales-assistant@1")  # noqa: E731
    assert _post(mail, "mail.send", msg | {"dry_run": True}, h()).json()["dry_run"] is True
    assert count() == 0
    first = _post(mail, "mail.send", msg | {"idempotency_key": "e2e-mail-0001"}, h()).json()
    assert first["status"] == "sent" and first["by"] == "agent:sales-assistant@1"
    assert _post(mail, "mail.send", msg | {"idempotency_key": "e2e-mail-0001"}, h()).json()["deduplicated"] is True
    assert count() == 1

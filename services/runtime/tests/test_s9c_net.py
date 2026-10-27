"""S9 WP-C：统一出站客户端——scheme + host + port 精确匹配（含前缀绕过反例）、重定向逐跳检查、源码审计。"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from runtime import net
from runtime.config import settings

GW = settings.capability_gateway_url  # 测试环境为 http://127.0.0.1:1


def test_configured_origins_allowed():
    for u in (settings.model_gateway_url, settings.capability_gateway_url, settings.policy_url, settings.observe_adapter_url):
        assert net.allowed(u + "/any/path?x=1"), u


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:1.evil.com/x",  # 前缀绕过：host 不同
        "http://127.0.0.1:12/x",  # 端口前缀
        "http://127.0.0.1:1@evil.com/x",  # userinfo 绕过
        "https://127.0.0.1:1/x",  # scheme 不同
        "http://127.0.0.2:1/x",
        "ftp://127.0.0.1:1/x",
        "file:///etc/passwd",
        "not a url",
    ],
)
def test_prefix_bypass_denied(url):
    assert net.allowed(GW)
    assert not net.allowed(url)
    with pytest.raises(net.EgressDenied):
        net.check(url)


def test_extra_allowlist_exact(monkeypatch):
    monkeypatch.setenv("EGRESS_ALLOWLIST", "https://api.example.com/v1, http://mcp.local:9000")
    assert net.allowed("https://api.example.com/other") and net.allowed("https://api.example.com:443/")
    assert net.allowed("http://mcp.local:9000/mcp")
    assert not net.allowed("https://api.example.com.evil.io/") and not net.allowed("http://api.example.com/")
    assert not net.allowed("http://mcp.local/mcp")  # 端口不同（默认 80）


async def test_client_enforces_per_request_and_redirect():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "http://evil.example.com/steal"})
        return httpx.Response(200, json={"ok": True})

    async with net.client(transport=httpx.MockTransport(handler), follow_redirects=True) as c:
        assert (await c.get(f"{GW}/ok")).json() == {"ok": True}
        with pytest.raises(net.EgressDenied) as ei:
            await c.get("http://evil.example.com/x")
        assert isinstance(ei.value, httpx.HTTPError) and ei.value.code == "EGRESS_DENIED"
        with pytest.raises(net.EgressDenied):
            await c.get(f"{GW}/redirect")  # 重定向的下一跳同样检查
    with pytest.raises(net.EgressDenied):
        net.client(base_url="http://evil.example.com")
    with pytest.raises(ValueError, match="mounts"):
        net.client(mounts={})


def test_audit_sources(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import httpx\nimport httpx as h\nfrom httpx import AsyncClient as AC\n"
        "a = httpx.AsyncClient()\nb = h.Client(timeout=1)\nc = AC()\nd = httpx.AsyncHTTPTransport()\n",
        encoding="utf-8",
    )
    findings = net.audit_sources([tmp_path])
    assert [f.split(":")[1] for f in findings] == ["4", "5", "6"]
    src = Path(net.__file__).resolve().parent
    # WP-C 范围（skills / sandbox / node / guard / assemble 与 net 自身）没有绕过统一出站工厂的构造
    scoped = [src / d for d in ("skills", "sandbox", "node", "guard", "assemble")] + [src / "net.py"]
    assert net.audit_sources(scoped) == []

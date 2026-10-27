"""出站越界（S9.6，D147；冻结契约 ⑤ ``runtime.net.client(**kw) -> httpx.AsyncClient``，越界抛 ``EgressDenied``）。

白名单按 scheme + host + port **精确匹配**：前缀 / 子域 / userinfo / 端口 / scheme 变体一律拒绝，重定向的每一跳同样检查。
用例以 ``EGRESS_ALLOWLIST`` 注入白名单（runtime.net 每次请求现算），以 ``httpx.MockTransport`` 代替真实网络——
transport 被 net.client 包裹在出站检查之后，故放行的请求落到 MockTransport，拒绝的请求根本到不了 transport。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
ALLOWED = "https://allowed.example.com"


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("EGRESS_ALLOWLIST", ALLOWED)
    return pytest.importorskip("runtime.net", reason="runtime.net（WP-C）尚未就绪")


def _transport(seen: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/redirect-out":
            return httpx.Response(302, headers={"location": "https://allowed.example.com.evil.com/steal"})
        return httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(handler)


async def test_allowed_origin_passes(net: Any) -> None:
    seen: list[str] = []
    async with net.client(transport=_transport(seen)) as c:
        r = await c.get(f"{ALLOWED}/api?q=1")
        r443 = await c.get("https://allowed.example.com:443/explicit-default-port")
    assert r.status_code == 200 and r443.status_code == 200 and len(seen) == 2


@pytest.mark.parametrize(
    "url",
    [
        "https://allowed.example.com.evil.com/",  # 前缀绕过（startswith 白名单的经典漏洞）
        "https://allowed.example.com.evil.com/https://allowed.example.com",
        "https://evil-allowed.example.com/",
        "https://sub.allowed.example.com/",  # 子域不自动放行
        "https://allowed.example.com@evil.com/",  # userinfo 伪装
        "https://allowed.example.com:8443/",  # 端口不同
        "http://allowed.example.com/",  # scheme 不同（降级为明文）
        "https://evil.com/?next=https://allowed.example.com",
        "https://127.0.0.1/",
    ],
)
async def test_out_of_allowlist_denied(net: Any, url: str) -> None:
    seen: list[str] = []
    async with net.client(transport=_transport(seen)) as c:
        with pytest.raises(net.EgressDenied) as ei:
            await c.get(url)
    assert ei.value.code == "EGRESS_DENIED"
    assert seen == [], "越界请求不应到达 transport"


async def test_redirect_out_of_allowlist_denied(net: Any) -> None:
    """白名单内地址 302 到越界地址：跟随重定向的下一跳被拒绝。"""
    seen: list[str] = []
    async with net.client(transport=_transport(seen), follow_redirects=True) as c:
        with pytest.raises(net.EgressDenied):
            await c.get(f"{ALLOWED}/redirect-out")
    assert seen == [f"{ALLOWED}/redirect-out"]


def test_out_of_allowlist_base_url_and_proxy_refused(net: Any) -> None:
    """构造期即拒绝：越界 base_url；proxy / mounts 会绕过出站检查，不接受。"""
    with pytest.raises(net.EgressDenied):
        net.client(base_url="https://allowed.example.com.evil.com")
    with pytest.raises(ValueError):
        net.client(proxy="http://proxy.evil.com:3128")


def test_runtime_constructs_http_clients_only_in_net(net: Any) -> None:
    """CI 架构检查：Runtime 源码中 net.py 之外不得直接构造 httpx.AsyncClient / httpx.Client（否则绕过白名单）。"""
    assert net.audit_sources([ROOT / "services" / "runtime" / "src" / "runtime"]) == []

"""OBO 令牌缓存（D103 / D131）：LRU 上限、命中刷新新近度、读时剔除过期项、TTL = min(OBO exp, 用户令牌 exp) − 30 s。"""

from __future__ import annotations

import base64
import hashlib
import json
import time

import httpx
import pytest
from fastapi import HTTPException

from capability_gateway import config, main


class Idp:
    """替身 Keycloak 令牌端点：每次签发不同的 OBO 令牌，记录调用次数。"""

    def __init__(self, expires_in: float = 300) -> None:
        self.calls = 0
        self.expires_in = expires_in

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(200, json={"access_token": f"obo-{self.calls}", "expires_in": self.expires_in})


@pytest.fixture
def idp(monkeypatch: pytest.MonkeyPatch) -> Idp:
    idp = Idp()
    monkeypatch.setattr(config, "KEYCLOAK_TOKEN_URL", "http://idp.test/token")
    main.MCP_TOKEN_CACHE.clear()
    yield idp
    main.MCP_TOKEN_CACHE.clear()


async def _obo(idp: Idp, user_token: str, audience: str = "mcp-ticket", user_exp: float | None = None) -> str:
    async with httpx.AsyncClient(transport=httpx.MockTransport(idp)) as client:
        token, _ = await main._obo_token(client, user_token, audience, user_exp)
    return token


def _key(user_token: str, audience: str = "mcp-ticket") -> tuple[str, str]:
    return (hashlib.sha256(user_token.encode()).hexdigest(), audience)


def _jwt(claims: dict[str, object]) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


async def test_cache_hit_avoids_second_exchange(idp: Idp) -> None:
    assert await _obo(idp, "u1") == await _obo(idp, "u1") == "obo-1"
    assert idp.calls == 1
    assert await _obo(idp, "u1", "other-aud") == "obo-2"  # 受众是缓存键的一部分


async def test_lru_evicts_least_recently_used(idp: Idp, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "MCP_TOKEN_CACHE_MAX", 2)
    await _obo(idp, "u1")
    await _obo(idp, "u2")
    await _obo(idp, "u1")  # 命中：u1 变为最近使用
    await _obo(idp, "u3")  # 超过上限：淘汰最久未使用的 u2
    assert len(main.MCP_TOKEN_CACHE) == 2
    assert _key("u2") not in main.MCP_TOKEN_CACHE and _key("u1") in main.MCP_TOKEN_CACHE
    calls = idp.calls
    await _obo(idp, "u2")
    assert idp.calls == calls + 1


async def test_expired_entry_removed_on_read(idp: Idp, monkeypatch: pytest.MonkeyPatch) -> None:
    idp.expires_in = 31  # 缓存到过期前 30 秒 → 只有 1 秒有效期
    await _obo(idp, "u1")
    assert _key("u1") in main.MCP_TOKEN_CACHE
    base = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base + 5)
    with pytest.raises(HTTPException):  # 换取失败（用户令牌也过期了）：过期项仍已在读时剔除
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(400))) as client:
            await main._obo_token(client, "u1", "mcp-ticket")
    assert _key("u1") not in main.MCP_TOKEN_CACHE
    idp.expires_in = 300
    assert await _obo(idp, "u1") == "obo-2"
    assert main.MCP_TOKEN_CACHE[_key("u1")][0] == "obo-2" and len(main.MCP_TOKEN_CACHE) == 1


async def test_ttl_bounded_by_user_token_exp(idp: Idp, monkeypatch: pytest.MonkeyPatch) -> None:
    """D131：用户令牌比 OBO 先过期时，缓存只到用户令牌 exp − 30 s（显式传入或从用户令牌 JWT 读取）。"""
    now = time.time()
    user = _jwt({"sub": "u", "exp": now + 40})
    await _obo(idp, user)
    remaining = main.MCP_TOKEN_CACHE[_key(user)][1] - time.monotonic()
    assert 5 < remaining <= 10.5  # min(300, 40) − 30
    await _obo(idp, "opaque", user_exp=now + 100)
    remaining = main.MCP_TOKEN_CACHE[_key("opaque")][1] - time.monotonic()
    assert 65 < remaining <= 70.5
    await _obo(idp, "short", user_exp=now + 20)  # 剩余不足 30 秒：不缓存，下次重新换取
    assert _key("short") not in main.MCP_TOKEN_CACHE
    base = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base + 11)
    calls = idp.calls
    await _obo(idp, user)  # 用户令牌 exp − 30 s 已过：重新换取
    assert idp.calls == calls + 1


def test_default_limit_is_10000() -> None:
    if "MCP_TOKEN_CACHE_MAX" not in main.os.environ:
        assert main.MCP_TOKEN_CACHE_MAX == 10000

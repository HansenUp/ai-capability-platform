"""OBO 令牌缓存（D103）：LRU 上限 MCP_TOKEN_CACHE_MAX、命中刷新新近度、读时剔除过期项。"""

from __future__ import annotations

import hashlib
import time

import httpx
import pytest

from capability_gateway import main


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
    monkeypatch.setattr(main, "KEYCLOAK_TOKEN_URL", "http://idp.test/token")
    main.MCP_TOKEN_CACHE.clear()
    yield idp
    main.MCP_TOKEN_CACHE.clear()


async def _obo(idp: Idp, user_token: str, audience: str = "mcp-ticket") -> str:
    async with httpx.AsyncClient(transport=httpx.MockTransport(idp)) as client:
        token, _ = await main._obo_token(client, user_token, audience)
    return token


def _key(user_token: str, audience: str = "mcp-ticket") -> tuple[str, str]:
    return (hashlib.sha256(user_token.encode()).hexdigest(), audience)


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
    await _obo(idp, "u2")  # 被淘汰的需要重新换取
    assert idp.calls == calls + 1


async def test_expired_entry_removed_on_read(idp: Idp, monkeypatch: pytest.MonkeyPatch) -> None:
    idp.expires_in = 31  # 缓存到过期前 30 秒 → 只有 1 秒有效期
    await _obo(idp, "u1")
    assert _key("u1") in main.MCP_TOKEN_CACHE
    base = time.monotonic()  # main 在函数内导入 time，这里直接替换标准库的 monotonic
    monkeypatch.setattr(time, "monotonic", lambda: base + 5)
    with pytest.raises(main.HTTPException):  # 换取失败（用户令牌也过期了）：过期项仍已在读时剔除
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(400))) as client:
            await main._obo_token(client, "u1", "mcp-ticket")
    assert _key("u1") not in main.MCP_TOKEN_CACHE
    idp.expires_in = 300
    assert await _obo(idp, "u1") == "obo-2"  # 重新换取
    assert main.MCP_TOKEN_CACHE[_key("u1")][0] == "obo-2" and len(main.MCP_TOKEN_CACHE) == 1


def test_default_limit_is_10000() -> None:
    if "MCP_TOKEN_CACHE_MAX" not in main.os.environ:
        assert main.MCP_TOKEN_CACHE_MAX == 10000

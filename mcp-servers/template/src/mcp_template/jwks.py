"""JWKS 公钥缓存：OBO 令牌（IdP 的 JWKS）与 act 断言（能力网关 ``/.well-known/jwks.json``）共用。

按 TTL 缓存；遇到未知 kid（对端轮换密钥）时提前刷新，但两次刷新至少间隔 ``JWKS_MIN_REFRESH_S``，
防止伪造 kid 的请求把对端打满。对端不可达且缓存中没有该 kid 时抛 :class:`JWKSUnavailable`（调用方映射为 503）；
缓存中有该 kid 时沿用旧公钥，但距上次成功拉取超过 ``JWKS_MAX_STALE_S``（默认 1 小时）后不再沿用，
同样抛 :class:`JWKSUnavailable`（失败关闭：被撤销的密钥不能无限期有效）。
"""

from __future__ import annotations

import asyncio
import time

import httpx
import jwt
from jwt import PyJWK

JWKS_TTL_S = 300.0  # JWKS 缓存有效期（秒）
JWKS_MIN_REFRESH_S = 10.0  # 两次刷新的最小间隔（秒）
JWKS_TIMEOUT_S = 5.0  # 拉取 JWKS 的超时（秒）
JWKS_MAX_STALE_S = 3600.0  # 刷新持续失败时沿用旧公钥的上限（秒，距上次成功拉取）


class JWKSUnavailable(Exception):
    """JWKS 端点不可达，且缓存中没有所需的 kid。"""


class JWKSCache:
    """单个 JWKS 地址的公钥缓存（按 kid 索引）。并发刷新以锁串行化，同一时刻只有一个请求去拉对端。"""

    def __init__(self, url: str, ttl_s: float = JWKS_TTL_S, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.url = url
        self.ttl_s = ttl_s
        self._transport = transport
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at = 0.0  # 上次成功拉取的时间（monotonic）
        self._attempted_at = float("-inf")  # 上次尝试拉取的时间，用于限制刷新频率
        self._lock = asyncio.Lock()

    async def _refresh(self) -> None:
        """拉取并解析 JWKS；跳过无 kid、非签名用途或本库不支持的密钥。"""
        self._attempted_at = time.monotonic()
        async with httpx.AsyncClient(transport=self._transport, timeout=JWKS_TIMEOUT_S) as client:
            r = await client.get(self.url)
            r.raise_for_status()
            body = r.json()
        keys: dict[str, PyJWK] = {}
        for jwk in body.get("keys", []):
            if not jwk.get("kid") or jwk.get("use", "sig") != "sig":
                continue
            try:
                keys[jwk["kid"]] = PyJWK(jwk)
            except jwt.PyJWTError:
                continue  # 例如加密用途的密钥或不支持的曲线
        self._keys = keys
        self._fetched_at = time.monotonic()

    async def get(self, kid: str) -> PyJWK | None:
        """返回 kid 对应的公钥；缓存过期或 kid 未知时（受刷新间隔限制）重新拉取。"""
        now = time.monotonic()
        if kid in self._keys and now - self._fetched_at < self.ttl_s:
            return self._keys[kid]
        async with self._lock:
            now = time.monotonic()
            due = now - self._attempted_at >= JWKS_MIN_REFRESH_S
            stale = now - self._fetched_at >= self.ttl_s
            if due and (stale or kid not in self._keys):
                try:
                    await self._refresh()
                except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                    if kid not in self._keys:
                        raise JWKSUnavailable(self.url) from None
                    # 对端暂时不可达但缓存中有该 kid：沿用旧公钥（受 JWKS_MAX_STALE_S 限制，见下）
            if kid in self._keys and time.monotonic() - self._fetched_at >= JWKS_MAX_STALE_S:
                raise JWKSUnavailable(self.url)  # 长时间无法刷新：不再沿用旧公钥
            return self._keys.get(kid)


_caches: dict[str, JWKSCache] = {}
_transport_for_tests: httpx.AsyncBaseTransport | None = None


def set_transport_for_tests(transport: httpx.AsyncBaseTransport | None) -> None:
    """测试注入：所有 JWKS 改经给定 transport 拉取，并清空缓存。"""
    global _transport_for_tests
    _transport_for_tests = transport
    _caches.clear()


def jwks_cache(url: str) -> JWKSCache:
    """进程级 JWKS 缓存（每个地址一个，首次使用时创建）。"""
    cache = _caches.get(url)
    if cache is None:
        cache = _caches[url] = JWKSCache(url, JWKS_TTL_S, _transport_for_tests)
    return cache

"""能力网关客户端（S8 §2 S4 #1，契约 3）：Runtime 调用能力网关的唯一出口（工具代理 /invoke、组装器 /capabilities、
发现 /capabilities/search）。

- 配置了 ``RUNTIME_GATEWAY_TOKEN_URL``（Keycloak 令牌端点）：以服务客户端 ``ai-runtime-svc`` 的 client credentials 换取服务令牌
  并按进程缓存（到期前 ``TOKEN_REFRESH_MARGIN_S`` 秒刷新），请求头为 ``Authorization: Bearer <服务令牌>``，终端用户令牌放
  ``X-AI-User-Token``（网关据此确定用户并换取 OBO 令牌）。换取失败抛 ``GatewayAuthError``（httpx.HTTPError 的子类，调用方按
  网关不可达处理，失败关闭）。
- 未配置（开发态）：保持此前行为——用户令牌（有则）放 ``Authorization: Bearer``，不带服务令牌。

``RUNTIME_ENV=prod`` 的安全基线要求令牌端点与服务客户端密钥（``RUNTIME_GATEWAY_CLIENT_SECRET``，经 Vault / 挂载文件注入）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from runtime import net
from runtime.config import settings

TOKEN_REFRESH_MARGIN_S = 30.0  # 服务令牌到期前提前刷新的秒数
TOKEN_TIMEOUT_S = 5.0  # 换取服务令牌的超时（秒）
USER_TOKEN_HEADER = "X-AI-User-Token"  # noqa: S105 —— 头名而非密钥：终端用户令牌头（契约 3）


class GatewayAuthError(httpx.HTTPError):
    """服务令牌换取失败（令牌端点不可达、拒绝或响应不合规）：调用方按网关不可达处理。"""


_transport: httpx.AsyncBaseTransport | None = None  # 测试注入：令牌端点与网关请求都经该 transport
_token: str | None = None  # 缓存的服务令牌
_token_expires_at = 0.0  # 服务令牌到期时间（monotonic）
_lock: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None  # 换取令牌的锁（按事件循环，锁不能跨循环复用）


def set_transport_for_tests(transport: httpx.AsyncBaseTransport | None) -> None:
    """测试注入：网关与令牌端点请求改经给定 transport，并清空服务令牌缓存。"""
    global _transport
    _transport = transport
    invalidate_service_token()


def transport() -> httpx.AsyncBaseTransport | None:
    """当前注入的 transport（None 表示真实网络）。"""
    return _transport


def invalidate_service_token() -> None:
    """丢弃缓存的服务令牌（网关返回 401 时调用，下次请求重新换取）。"""
    global _token, _token_expires_at
    _token, _token_expires_at = None, 0.0


def service_auth_enabled() -> bool:
    """是否启用服务间鉴权（配置了令牌端点）。"""
    return bool(settings.gateway_token_url)


async def _fetch_service_token() -> tuple[str, float]:
    """client credentials 换取服务令牌，返回 (令牌, 有效秒数)。"""
    if not settings.gateway_client_secret:
        raise GatewayAuthError("RUNTIME_GATEWAY_CLIENT_SECRET is not configured")
    data = {
        "grant_type": "client_credentials",
        "client_id": settings.gateway_client_id,
        "client_secret": settings.gateway_client_secret,
    }
    try:
        async with net.client(timeout=TOKEN_TIMEOUT_S, transport=_transport) as c:
            r = await c.post(str(settings.gateway_token_url), data=data)
    except httpx.HTTPError as e:
        raise GatewayAuthError(f"service token endpoint unreachable: {type(e).__name__}") from e
    if r.status_code != 200:
        raise GatewayAuthError(f"service token request rejected: HTTP {r.status_code}")
    try:
        body = r.json()
        token, expires_in = body["access_token"], float(body.get("expires_in", 60))
    except (ValueError, KeyError, TypeError) as e:
        raise GatewayAuthError("service token response malformed") from e
    if not isinstance(token, str) or not token:
        raise GatewayAuthError("service token response malformed")
    return token, expires_in


async def service_token() -> str:
    """缓存的服务令牌；剩余有效期不足 TOKEN_REFRESH_MARGIN_S 时重新换取（并发换取以锁串行化）。"""
    global _token, _token_expires_at, _lock
    if _token is not None and time.monotonic() < _token_expires_at - TOKEN_REFRESH_MARGIN_S:
        return _token
    loop = asyncio.get_running_loop()
    if _lock is None or _lock[0] is not loop:
        _lock = (loop, asyncio.Lock())
    async with _lock[1]:
        if _token is not None and time.monotonic() < _token_expires_at - TOKEN_REFRESH_MARGIN_S:
            return _token
        token, expires_in = await _fetch_service_token()
        _token, _token_expires_at = token, time.monotonic() + expires_in
        return token


async def headers(user_token: str | None) -> dict[str, str]:
    """调用网关的鉴权头（契约 3）：服务态为服务令牌 + X-AI-User-Token；开发态为用户令牌（有则）。"""
    if not service_auth_enabled():
        return {"Authorization": f"Bearer {user_token}"} if user_token else {}
    out = {"Authorization": f"Bearer {await service_token()}"}
    if user_token:
        out[USER_TOKEN_HEADER] = user_token
    return out


async def request(
    method: str,
    path: str,
    *,
    user_token: str | None = None,
    timeout_s: float,
    json: Any = None,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    """向能力网关发一次请求（带鉴权头）；网络错误与服务令牌换取失败均抛 httpx.HTTPError。
    网关返回 401 时丢弃缓存的服务令牌（可能已被吊销），由调用方按失败处理（不自动重试，写操作不重放）。"""
    url = f"{settings.capability_gateway_url.rstrip('/')}{path}"
    hdrs = await headers(user_token)
    async with net.client(timeout=timeout_s, transport=_transport) as c:
        r = await c.request(method, url, json=json, params=params, headers=hdrs)
    if r.status_code == 401 and service_auth_enabled():
        invalidate_service_token()
    return r

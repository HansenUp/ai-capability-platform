"""Console → 能力网关客户端（S9：Skill 评测结果回写、审批代理、推送前查询；鉴权同 S8 契约 3）。

- 配置了 ``CONSOLE_OIDC_TOKEN_URL`` 与服务客户端密钥（``CONSOLE_GATEWAY_CLIENT_SECRET``，缺省取 ``CONSOLE_CLIENT_SECRET``）：
  以 client credentials（客户端 ``CONSOLE_GATEWAY_CLIENT_ID``，缺省同 ``CONSOLE_CLIENT_ID`` / console-api）换取服务令牌并缓存，
  请求头 ``Authorization: Bearer <服务令牌>``，终端用户令牌（如审批人）放 ``X-AI-User-Token``；
- 未配置（开发态）：用户令牌（有则）放 ``Authorization: Bearer``，不带服务令牌。
- 网关的管理操作（吊销）另需管理令牌：``CONSOLE_GATEWAY_ADMIN_TOKEN``（与网关 ``GATEWAY_ADMIN_TOKEN`` 同值；未配置时不带，网关 403）。
网络错误与令牌换取失败抛 ``GatewayError``（调用方映射为 502）。
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

TIMEOUT_S = 30.0
USER_TOKEN_HEADER = "X-AI-User-Token"  # noqa: S105 —— 头名而非密钥

_transport: httpx.AsyncBaseTransport | None = None
_token: tuple[str, float] | None = None


class GatewayError(Exception):
    """能力网关不可达或服务令牌换取失败。"""


def set_transport_for_tests(transport: httpx.AsyncBaseTransport | None) -> None:
    global _transport, _token
    _transport, _token = transport, None


def base_url() -> str:
    return os.environ.get("CONSOLE_GATEWAY_URL", "http://localhost:8082").rstrip("/")


async def _service_token(c: httpx.AsyncClient) -> str | None:
    global _token
    token_url = os.environ.get("CONSOLE_OIDC_TOKEN_URL")
    secret = os.environ.get("CONSOLE_GATEWAY_CLIENT_SECRET") or os.environ.get("CONSOLE_CLIENT_SECRET")
    if not token_url or not secret:
        return None
    if _token and time.monotonic() < _token[1]:
        return _token[0]
    client_id = os.environ.get("CONSOLE_GATEWAY_CLIENT_ID") or os.environ.get("CONSOLE_CLIENT_ID", "console-api")
    r = await c.post(token_url, data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret})
    if r.status_code != 200:
        raise GatewayError(f"token endpoint returned HTTP {r.status_code}")
    try:
        body = r.json()
        _token = (str(body["access_token"]), time.monotonic() + float(body.get("expires_in", 60)) - 30)
    except (ValueError, KeyError, TypeError) as e:
        raise GatewayError("token response malformed") from e
    return _token[0]


async def request(
    method: str,
    path: str,
    *,
    user_token: str | None = None,
    json: Any = None,
    params: dict[str, Any] | None = None,
    timeout_s: float = TIMEOUT_S,
    admin: bool = False,
) -> httpx.Response:
    try:
        async with httpx.AsyncClient(timeout=timeout_s, transport=_transport) as c:
            svc = await _service_token(c)
            if svc:
                headers = {"Authorization": f"Bearer {svc}"} | ({USER_TOKEN_HEADER: user_token} if user_token else {})
            else:
                headers = {"Authorization": f"Bearer {user_token}"} if user_token else {}
            admin_token = os.environ.get("CONSOLE_GATEWAY_ADMIN_TOKEN")
            if admin and admin_token:
                headers["X-Gateway-Admin-Token"] = admin_token
            return await c.request(method, f"{base_url()}{path}", json=json, params=params, headers=headers)
    except httpx.HTTPError as e:
        raise GatewayError(f"gateway unreachable: {type(e).__name__}") from e

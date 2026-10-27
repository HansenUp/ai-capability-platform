"""Console → Runtime 管理接口的同步客户端（发布、灰度、回滚）。

鉴权两种方式：
- 配置了 ``CONSOLE_OIDC_TOKEN_URL`` 与 ``CONSOLE_CLIENT_SECRET``：以 Client Credentials 取短期令牌（客户端
  ``CONSOLE_CLIENT_ID``，默认 console-api，其服务账号带 platform-admin），令牌按过期时间缓存；
- 否则（本机开发、Runtime 未强制鉴权）：以 ``X-AI-Subject`` / ``X-AI-Tenant`` 头传递操作者身份。

任何失败（Runtime 不可达或返回错误状态码）都抛 ``RuntimeSyncError``，调用方返回 502 且不改 Console 侧状态：
避免出现"Console 显示已发布 / 已回滚，Runtime 实际仍是旧版本"。
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

RUNTIME_ADMIN_URL = os.environ.get("RUNTIME_ADMIN_URL", "http://localhost:8080")  # Runtime 管理接口地址
TOKEN_URL = os.environ.get("CONSOLE_OIDC_TOKEN_URL")  # 例：http://localhost:8180/realms/ai-platform/protocol/openid-connect/token
CLIENT_ID = os.environ.get("CONSOLE_CLIENT_ID", "console-api")
TIMEOUT_S = 10.0

_transport: httpx.AsyncBaseTransport | None = None
_token: tuple[str, float] | None = None  # (access_token, 过期时间 monotonic)


class RuntimeSyncError(Exception):
    """同步到 Runtime 失败；status 为 Runtime 返回的状态码（不可达时为 None）。"""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def set_transport_for_tests(transport: httpx.AsyncBaseTransport | None) -> None:
    """测试注入：Runtime 与 IdP 请求经给定 transport 处理，并清空令牌缓存。"""
    global _transport, _token
    _transport, _token = transport, None


async def _bearer(client: httpx.AsyncClient) -> str | None:
    """Client Credentials 令牌（未配置时返回 None）；提前 30 秒视为过期。"""
    global _token
    secret = os.environ.get("CONSOLE_CLIENT_SECRET")
    if not TOKEN_URL or not secret:
        return None
    if _token and time.monotonic() < _token[1]:
        return _token[0]
    r = await client.post(TOKEN_URL, data={"grant_type": "client_credentials", "client_id": CLIENT_ID, "client_secret": secret})
    if r.status_code != 200:
        raise RuntimeSyncError(f"token endpoint returned HTTP {r.status_code}")
    body = r.json()
    _token = (str(body["access_token"]), time.monotonic() + float(body.get("expires_in", 60)) - 30)
    return _token[0]


def subject(user: str) -> str:
    """Runtime 眼中本客户端请求的主体：令牌模式为服务账号 ``app:<CLIENT_ID>``（Runtime 只认令牌，不认 user 参数），
    开发头模式为 ``human:<user>``。用于 0% 预发布版本的评测白名单——白名单写错主体时 Runtime 按"版本不存在"处理。"""
    return f"app:{CLIENT_ID}" if TOKEN_URL and os.environ.get("CONSOLE_CLIENT_SECRET") else f"human:{user}"


async def call(
    method: str,
    path: str,
    *,
    user: str,
    tenant: str,
    json: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
    timeout_s: float = TIMEOUT_S,
) -> dict[str, Any]:
    """调用 Runtime 管理接口，返回 JSON 响应体（timeout_s：/v1/messages 等含模型调用的请求需要更长超时）。"""
    try:
        async with httpx.AsyncClient(base_url=RUNTIME_ADMIN_URL, timeout=timeout_s, transport=_transport) as client:
            token = await _bearer(client)
            headers = {"Authorization": f"Bearer {token}"} if token else {"X-AI-Subject": f"human:{user}", "X-AI-Tenant": tenant}
            r = await client.request(method, path, json=json, headers=headers, params=params)
    except httpx.HTTPError as e:
        raise RuntimeSyncError(f"runtime unreachable: {type(e).__name__}") from e
    if r.status_code >= 400:
        raise RuntimeSyncError(f"runtime returned HTTP {r.status_code}: {r.text[:300]}", r.status_code)
    return dict(r.json()) if r.content else {}

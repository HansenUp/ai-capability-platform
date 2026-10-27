"""节点接口鉴权（S9.8，D148）：``/nodes/register``、心跳、指令拉取与回执只接受 ``ai-node-svc`` 服务令牌。

两种模式（按配置自动选择，均失败关闭；都未配置时一律 401）：
- ``CONSOLE_NODE_JWKS_URL``（Keycloak 证书端点）：Bearer 须为 RS256/ES256 JWT，验签、校验 exp，``azp`` 须在
  ``CONSOLE_NODE_CLIENT_IDS``（逗号分隔，默认 ``ai-node-svc``）中；配置了 ``CONSOLE_NODE_ISSUER`` / ``CONSOLE_NODE_AUDIENCE``
  时同时校验 iss / aud。JWKS 按 ``CONSOLE_NODE_JWKS_TTL_S``（默认 300 秒）缓存，遇未知 kid 强制刷新一次。
- ``CONSOLE_NODE_TOKEN``（开发态静态令牌，与 Runtime ``NODE_CONSOLE_TOKEN`` 同值）：常量时间比较。

``CONSOLE_ENV=prod`` 要求 JWKS 模式且不得配置静态令牌（见 production_problems）。
"""

from __future__ import annotations

import hmac
import os
import time
from typing import Any

import httpx
import jwt
from fastapi import Header, HTTPException

JWKS_TIMEOUT_S = 5.0
_transport: httpx.AsyncBaseTransport | None = None
_jwks: tuple[str, float, jwt.PyJWKSet] | None = None  # (url, 获取时间, 公钥集)


def set_transport_for_tests(transport: httpx.AsyncBaseTransport | None) -> None:
    global _transport, _jwks
    _transport, _jwks = transport, None


def _client_ids() -> set[str]:
    return {x.strip() for x in os.environ.get("CONSOLE_NODE_CLIENT_IDS", "ai-node-svc").split(",") if x.strip()}


def production_problems() -> list[str]:
    problems: list[str] = []
    if not os.environ.get("CONSOLE_NODE_JWKS_URL"):
        problems.append("缺少 CONSOLE_NODE_JWKS_URL（节点接口须校验 ai-node-svc 服务令牌）")
    if os.environ.get("CONSOLE_NODE_TOKEN"):
        problems.append("CONSOLE_NODE_TOKEN 是开发态静态节点令牌，生产禁止配置")
    return problems


async def _keyset(url: str, force: bool = False) -> jwt.PyJWKSet:
    global _jwks
    ttl = float(os.environ.get("CONSOLE_NODE_JWKS_TTL_S", "300"))
    if not force and _jwks and _jwks[0] == url and time.monotonic() - _jwks[1] < ttl:
        return _jwks[2]
    async with httpx.AsyncClient(timeout=JWKS_TIMEOUT_S, transport=_transport) as c:
        r = await c.get(url)
    r.raise_for_status()
    keyset = jwt.PyJWKSet.from_dict(r.json())
    _jwks = (url, time.monotonic(), keyset)
    return keyset


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(401, detail, headers={"WWW-Authenticate": "Bearer"})


async def _verify_jwt(token: str, jwks_url: str) -> dict[str, Any]:
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError as e:
        raise _unauthorized("malformed node token") from e
    try:
        keyset = await _keyset(jwks_url)
        key = next((k for k in keyset.keys if k.key_id == kid), None)
        if key is None:
            keyset = await _keyset(jwks_url, force=True)
            key = next((k for k in keyset.keys if k.key_id == kid), None)
    except (httpx.HTTPError, ValueError, jwt.PyJWTError) as e:
        raise HTTPException(503, "node token verification unavailable") from e
    if key is None:
        raise _unauthorized("unknown node token key")
    audience = os.environ.get("CONSOLE_NODE_AUDIENCE") or None
    issuer = os.environ.get("CONSOLE_NODE_ISSUER") or None
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key.key,
            algorithms=["RS256", "ES256"],
            audience=audience,
            issuer=issuer,
            options={"require": ["exp"], "verify_aud": audience is not None},
            leeway=30,
        )
    except jwt.PyJWTError as e:
        raise _unauthorized(f"invalid node token: {type(e).__name__}") from e
    if claims.get("azp") not in _client_ids():
        raise HTTPException(403, "token is not a node service token")
    return claims


async def require_node(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """FastAPI 依赖：校验节点服务令牌，返回令牌声明（静态令牌模式返回 {"azp": "static"}）。"""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _unauthorized("node service token required")
    token = authorization.split(" ", 1)[1].strip()
    jwks_url = os.environ.get("CONSOLE_NODE_JWKS_URL")
    if jwks_url:
        return await _verify_jwt(token, jwks_url)
    static = os.environ.get("CONSOLE_NODE_TOKEN")
    if static and hmac.compare_digest(token.encode(), static.encode()):
        return {"azp": "static"}
    raise _unauthorized("invalid node service token")

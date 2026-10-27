"""只读路由服务身份；不接受开发角色头或节点服务令牌。"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
import jwt
from fastapi import Header, HTTPException

from console_api.auth import Operator, Role

_cached: tuple[str, float, jwt.PyJWKSet] | None = None
_transport: httpx.AsyncBaseTransport | None = None


async def routing_operator(authorization: str | None = Header(default=None)) -> Operator:
    url = os.environ.get("CONSOLE_ROUTING_JWKS_URL", "")
    issuer = os.environ.get("CONSOLE_ROUTING_ISSUER", "")
    audience = os.environ.get("CONSOLE_ROUTING_AUDIENCE", "")
    if not all((url, issuer, audience)):
        raise HTTPException(503, "routing service authentication is not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "routing bearer token required", headers={"WWW-Authenticate": "Bearer"})
    token = authorization[7:]
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str) or not header["kid"]:
            raise jwt.InvalidTokenError()
        global _cached
        if _cached is None or _cached[0] != url or time.monotonic() - _cached[1] >= 60:
            async with httpx.AsyncClient(timeout=3, transport=_transport, follow_redirects=False) as client:
                response = await client.get(url)
                response.raise_for_status()
                keyset = jwt.PyJWKSet.from_dict(response.json())
            _cached = (url, time.monotonic(), keyset)
        key = next((key for key in _cached[2].keys if key.key_id == header["kid"] and key.algorithm_name == "RS256"), None)
        if key is None:
            raise jwt.InvalidTokenError()
        claims: dict[str, Any] = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            issuer=issuer,
            audience=audience,
            options={"require": ["exp", "iat", "sub", "iss", "aud", "tenant", "client_id", "scope"]},
        )
    except (httpx.HTTPError, ValueError, KeyError) as error:
        if isinstance(error, jwt.PyJWTError):
            raise HTTPException(401, "invalid routing token", headers={"WWW-Authenticate": "Bearer"}) from None
        raise HTTPException(503, "routing JWKS unavailable") from None
    except jwt.PyJWTError:
        raise HTTPException(401, "invalid routing token", headers={"WWW-Authenticate": "Bearer"}) from None
    tenant, scope = claims.get("tenant"), claims.get("scope")
    if (
        claims.get("client_id") != "ai-routing-svc"
        or not isinstance(tenant, str)
        or not tenant.strip()
        or len(tenant) > 64
        or not isinstance(scope, str)
        or "routing:read" not in scope.split()
    ):
        raise HTTPException(403, "routing service identity, tenant and scope required")
    return Operator(user="service:ai-routing-svc", tenant=tenant, role=Role.TENANT_ADMIN)

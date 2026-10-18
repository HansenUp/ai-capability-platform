"""网关的调用方鉴权（§2 S4 #1 / D130，契约 3）。

已配置 Keycloak（KEYCLOAK_TOKEN_URL）时：
- ``Authorization: Bearer <服务令牌>``：ai-runtime-svc 的 client_credentials 令牌，本地按 JWKS 验签（iss、aud=GATEWAY_SERVICE_AUDIENCE、
  exp），``azp ∈ GATEWAY_TRUSTED_CALLERS``，否则 401（终端用户令牌直连网关同样 401）；
- ``X-AI-User-Token: <终端用户令牌>``：同一 JWKS 验签（aud 按 GATEWAY_USER_AUDIENCE，未配置时不校验），主体 sub / tenant /
  realm_access.roles 一律取自令牌（D128），缺 tenant 声明 403。
未配置 Keycloak 时为开发态：调用方取 ``X-Gateway-Client``（缺省为网关自身），主体取 ``X-AI-Subject`` / ``X-AI-Tenant`` / ``X-AI-Roles``
（或请求体中的 subject）；用户令牌可选（X-AI-User-Token，缺省取 Authorization: Bearer），只做不验签的 claims 读取。
服务令牌缺失 / 无效 / azp 不受信任一律 401（Runtime 据此丢弃服务令牌缓存后重取）。
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import time
from dataclasses import dataclass, replace
from typing import Annotated, Any

import httpx
import jwt
from fastapi import Header
from jwt import PyJWK
from jwt.types import Options

from capability_gateway import config
from capability_gateway.errors import GatewayError
from capability_gateway.policy import Principal

ALLOWED_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"})
JWKS_MIN_REFRESH_S = 10.0
JWKS_TIMEOUT_S = 5.0


def _unauthorized(code: str, detail: str) -> GatewayError:
    return GatewayError(401, code, detail, headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})


class JWKSCache:
    """JWKS 公钥缓存（按 kid）；未知 kid 时提前刷新，两次刷新至少间隔 JWKS_MIN_REFRESH_S。"""

    def __init__(self, url: str, ttl_s: float) -> None:
        self.url, self.ttl_s = url, ttl_s
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at = 0.0
        self._attempted_at = float("-inf")
        self._lock = asyncio.Lock()

    async def _refresh(self) -> None:
        self._attempted_at = time.monotonic()
        async with httpx.AsyncClient(transport=config.transport, timeout=JWKS_TIMEOUT_S) as client:
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
                continue
        self._keys = keys
        self._fetched_at = time.monotonic()

    async def get(self, kid: str) -> PyJWK | None:
        now = time.monotonic()
        if kid in self._keys and now - self._fetched_at < self.ttl_s:
            return self._keys[kid]
        async with self._lock:
            now = time.monotonic()
            due = now - self._attempted_at >= JWKS_MIN_REFRESH_S
            if due and (now - self._fetched_at >= self.ttl_s or kid not in self._keys):
                try:
                    await self._refresh()
                except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                    if kid not in self._keys:
                        raise httpx.HTTPError("JWKS unavailable") from None
            return self._keys.get(kid)


_jwks: JWKSCache | None = None


def reset_for_tests() -> None:
    global _jwks
    _jwks = None


def _cache() -> JWKSCache:
    global _jwks
    if not config.JWKS_URL:
        raise GatewayError(500, "misconfigured", "GATEWAY_JWKS_URL / KEYCLOAK_TOKEN_URL not configured")
    if _jwks is None or _jwks.url != config.JWKS_URL:
        _jwks = JWKSCache(config.JWKS_URL, config.JWKS_TTL_S)
    return _jwks


async def verify_jwt(token: str, *, audience: frozenset[str] | str | None, code: str) -> dict[str, Any]:
    """本地验签并返回 claims：算法白名单、kid、iss、aud（给定时）、exp / iat。失败 401（code 区分服务 / 用户令牌），JWKS 不可达 503。"""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise _unauthorized(code, "malformed token") from None
    alg, kid = header.get("alg"), header.get("kid")
    if not isinstance(alg, str) or alg not in ALLOWED_ALGORITHMS:
        raise _unauthorized(code, f"unsupported token algorithm: {alg}")
    if not isinstance(kid, str) or not kid:
        raise _unauthorized(code, "token header has no kid")
    try:
        key = await _cache().get(kid)
    except httpx.HTTPError:
        raise GatewayError(503, "idp_unavailable", "identity provider JWKS unavailable") from None
    if key is None:
        raise _unauthorized(code, "unknown signing key")
    if key.algorithm_name and key.algorithm_name != alg:
        raise _unauthorized(code, "token algorithm does not match signing key")
    aud = sorted(audience) if isinstance(audience, frozenset) else audience
    required = ["exp", "iat", "iss"] + (["aud"] if aud else [])
    options: Options = {"require": required, "verify_aud": bool(aud)}
    try:
        claims: dict[str, Any] = jwt.decode(
            token, key.key, algorithms=[alg], issuer=config.JWT_ISSUER, audience=aud or None, options=options
        )
    except jwt.ExpiredSignatureError:
        raise _unauthorized(code, "token expired") from None
    except jwt.PyJWTError as e:
        raise _unauthorized(code, f"invalid token: {type(e).__name__}") from None
    return claims


def unverified_claims(token: str | None) -> dict[str, Any]:
    """开发态：读取 JWT 形状令牌的 claims（不验签）；非 JWT 返回空字典。"""
    if not token:
        return {}
    try:
        seg = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
    except (IndexError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _bearer(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    if v.lower().startswith("bearer "):
        v = v.split(" ", 1)[1].strip()
    return v or None


def principal_from_claims(claims: dict[str, Any], token: str | None) -> Principal:
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise _unauthorized("invalid_user_token", "user token has no sub")
    tenant = claims.get("tenant")
    if not isinstance(tenant, str) or not tenant:
        raise GatewayError(403, "no_tenant", "user token has no 'tenant' claim")
    ra = claims.get("realm_access")
    roles = ra.get("roles", []) if isinstance(ra, dict) else []
    exp = claims.get("exp")
    return Principal(
        sub=sub,
        tenant=tenant,
        roles=tuple(str(r) for r in roles) if isinstance(roles, list) else (),
        exp=float(exp) if isinstance(exp, int | float) else None,
        token=token,
    )


@dataclass(frozen=True)
class Caller:
    """经认证的调用方：服务客户端（azp）与（可选）终端用户主体。"""

    client_id: str
    user: Principal | None
    dev: bool = False

    def principal(self, fallback_subject: str | None = None) -> Principal:
        """本次请求的主体。已配置 Keycloak 时必须有验签过的用户令牌（否则 401）；开发态可回退到请求体中的 subject。"""
        if not self.dev:
            if self.user is None:
                raise _unauthorized("user_token_required", "end-user token required (X-AI-User-Token)")
            return self.user
        base = self.user or Principal(sub="", tenant="default")
        return base if base.sub else replace(base, sub=fallback_subject or "")


async def resolve_caller_client(
    authorization: Annotated[str | None, Header()] = None,
    x_gateway_client: Annotated[str | None, Header()] = None,
) -> str:
    """解析调用方客户端：生产从服务令牌验签取 azp（不接受自称），开发取 X-Gateway-Client（缺省为网关自身）。"""
    if config.keycloak_enabled():
        token = _bearer(authorization)
        if not token:
            raise _unauthorized("service_token_required", "service token required (Authorization: Bearer)")
        claims = await verify_jwt(token, audience=config.SERVICE_AUDIENCE, code="invalid_service_token")
        azp = claims.get("azp") or claims.get("client_id")
        if not isinstance(azp, str) or not azp:
            raise _unauthorized("invalid_service_token", "service token has no azp")
        return azp
    return x_gateway_client or config.GATEWAY_CLIENT_ID


async def require_runtime_client(
    authorization: Annotated[str | None, Header()] = None,
    x_gateway_client: Annotated[str | None, Header()] = None,
    x_ai_user_token: Annotated[str | None, Header()] = None,
    x_ai_subject: Annotated[str | None, Header()] = None,
    x_ai_tenant: Annotated[str | None, Header()] = None,
    x_ai_roles: Annotated[str | None, Header()] = None,
) -> Caller:
    """FastAPI 依赖：保护 /invoke、/capabilities*、/authz/check、/token/exchange。"""
    client = await resolve_caller_client(authorization, x_gateway_client)
    user_token = _bearer(x_ai_user_token)
    if config.keycloak_enabled():
        if client not in config.TRUSTED_CALLERS:
            raise _unauthorized("untrusted_caller", "caller is not a trusted runtime client")
        user = None
        if user_token:
            claims = await verify_jwt(user_token, audience=config.USER_AUDIENCE or None, code="invalid_user_token")
            user = principal_from_claims(claims, user_token)
        return Caller(client, user)
    if client not in config.TRUSTED_CALLERS | {config.GATEWAY_CLIENT_ID}:
        raise GatewayError(403, "untrusted_caller", "untrusted gateway client")
    # 开发态兼容：Runtime 未配置服务令牌时以 Authorization: Bearer <用户令牌> 调用（与 S8 前的 /invoke 一致）
    user_token = user_token or _bearer(authorization)
    claims = unverified_claims(user_token)
    roles = tuple(r.strip() for r in (x_ai_roles or "").split(",") if r.strip())
    sub = x_ai_subject or str(claims.get("sub") or "")
    exp = claims.get("exp")
    user = Principal(
        sub=sub,
        tenant=x_ai_tenant or str(claims.get("tenant") or "default"),
        roles=roles,
        exp=float(exp) if isinstance(exp, int | float) else None,
        token=user_token,
    )
    return Caller(client, user, dev=True)


def require_gateway_admin(x_gateway_admin_token: Annotated[str | None, Header()] = None) -> None:
    """登记类接口的保护：网关会把 OBO 令牌发往登记的端点，未授权的登记等于可以截获令牌。
    配置了 GATEWAY_ADMIN_TOKEN 时必须带相同的 X-Gateway-Admin-Token；启用 Keycloak 却未配置管理令牌时登记接口关闭；
    纯开发态不检查。"""
    if config.GATEWAY_ADMIN_TOKEN:
        if not x_gateway_admin_token or not hmac.compare_digest(x_gateway_admin_token.encode(), config.GATEWAY_ADMIN_TOKEN.encode()):
            raise GatewayError(403, "admin_token_required", "gateway admin token required")
    elif config.keycloak_enabled():
        raise GatewayError(403, "registration_disabled", "registration disabled: GATEWAY_ADMIN_TOKEN not configured")


async def require_admin_or_service(
    authorization: Annotated[str | None, Header()] = None,
    x_gateway_admin_token: Annotated[str | None, Header()] = None,
) -> None:
    """连接器查询（GET /mcp/servers*，含端点与 manifest）的保护：带正确的管理令牌，或（已配置 Keycloak 时）受信服务令牌
    （azp ∈ GATEWAY_TRUSTED_CALLERS）。既未配置管理令牌也未配置 Keycloak 的纯开发态不检查。"""
    admin = config.GATEWAY_ADMIN_TOKEN
    if admin and x_gateway_admin_token and hmac.compare_digest(x_gateway_admin_token.encode(), admin.encode()):
        return
    if config.keycloak_enabled():
        if _bearer(authorization):
            if await resolve_caller_client(authorization, None) in config.TRUSTED_CALLERS:
                return
            raise GatewayError(403, "untrusted_caller", "caller is not a trusted service client")
        raise _unauthorized("service_token_required", "admin token or service token required")
    if admin:
        raise GatewayError(403, "admin_token_required", "gateway admin token required")

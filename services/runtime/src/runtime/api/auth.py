"""调用方身份提取（S2.1 JWT 验签、S2.2 应用凭证）。

- ``AUTH_REQUIRED=true``：只接受 ``Authorization: Bearer <JWT>``。令牌在本地按 JWKS 验签（不回调 IdP），
  校验签名算法、签发者（JWT_ISSUER）、受众（JWT_AUDIENCE）、exp / nbf / iat，再解析主体：
  - 用户令牌：``subject=human:<sub>``；``act`` 声明（RFC 8693，代为执行的 Agent / 应用）存在时记为 actor，
    否则 actor 取签发令牌的客户端 ``app:<azp>``；
  - 应用令牌（Client Credentials，Keycloak 服务账号）：``subject=actor=app:<client_id>``；
  - 租户取 ``JWT_TENANT_CLAIM``（默认 ``tenant``）声明，缺失时拒绝（403），不回退到默认租户。
- ``AUTH_REQUIRED=false``（本机开发）：信任 ``X-AI-Subject`` / ``X-AI-Tenant`` 请求头。

JWKS 按 ``JWKS_CACHE_TTL_S`` 缓存；遇到未知 kid（IdP 轮换密钥）时提前刷新，但两次刷新至少间隔
``JWKS_MIN_REFRESH_S``，防止伪造 kid 的请求把 IdP 打满。IdP 不可达且缓存中没有该 kid 时返回 503。

提取出的 Principal 同时挂到 ``request.state.principal``，供审计中间件记录"谁、以何身份"；
验签失败的原因写到 ``request.state.auth_error``。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import httpx
import jwt
from fastapi import Depends, Header, HTTPException, Request
from jwt import PyJWK
from jwt.types import Options

from runtime import net
from runtime.config import settings as settings

# 只接受非对称签名算法：拒绝 none 与 HS*（HS* 会让公开的 JWKS 公钥变成可伪造签名的共享密钥）
ALLOWED_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"})
JWKS_MIN_REFRESH_S = 10.0  # 因未知 kid 触发的 JWKS 刷新最小间隔（秒）
JWKS_TIMEOUT_S = 5.0  # 拉取 JWKS 的超时（秒）
ADMIN_ROLE = "platform-admin"  # 调用管理 API（/admin）所需的 realm 角色

AuthMethod = Literal["dev", "user", "client_credentials"]


@dataclass(frozen=True)
class Principal:
    """经身份提取后得到的调用主体（用于鉴权、审计与 OBO 场景下的 acting agent 记录）。"""

    subject: str  # "human:<id>" | "agent:<id>" | "app:<id>" | "service:<id>"
    tenant_id: str = "default"  # 租户标识
    actor: str | None = None  # 实际代为执行的 Agent / 应用（act 声明或签发令牌的客户端）
    auth_method: AuthMethod = "dev"  # 身份来源：dev 请求头 / 用户 JWT / 应用 Client Credentials
    client_id: str | None = None  # 签发令牌的 OIDC 客户端（azp）
    roles: frozenset[str] = frozenset()  # 令牌中的 realm 角色（realm_access.roles）
    # 原始访问令牌：只在内存中随 Run 传给工具代理，由能力网关换取 OBO 令牌；不进日志、审计与 repr
    token: str | None = field(default=None, repr=False, compare=False)


def _unauthorized(request: Request | None, detail: str) -> HTTPException:
    """401，附 RFC 6750 的 WWW-Authenticate 头；同时把原因留给审计。"""
    if request is not None:
        request.state.auth_error = detail
    return HTTPException(401, detail, headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})


class JWKSCache:
    """JWKS 公钥缓存（按 kid 索引）。并发刷新以锁串行化，同一时刻只有一个请求去拉 IdP。"""

    def __init__(self, url: str, ttl_s: float, transport: httpx.AsyncBaseTransport | None = None) -> None:
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
        async with net.client(transport=self._transport, timeout=JWKS_TIMEOUT_S) as client:
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
                continue  # 例如 RSA-OAEP 加密密钥或不支持的曲线
        self._keys = keys
        self._fetched_at = time.monotonic()

    async def get(self, kid: str) -> PyJWK | None:
        """返回 kid 对应的公钥；缓存过期或 kid 未知时（受刷新间隔限制）重新拉取。IdP 不可达时抛 httpx.HTTPError。"""
        now = time.monotonic()
        if kid in self._keys and now - self._fetched_at < self.ttl_s:
            return self._keys[kid]
        async with self._lock:
            now = time.monotonic()
            # 过期刷新与未知 kid 刷新都受最小间隔限制：IdP 不可达时不至于每个请求都排队等一次超时
            due = now - self._attempted_at >= JWKS_MIN_REFRESH_S
            stale = now - self._fetched_at >= self.ttl_s
            unknown = kid not in self._keys
            if due and (stale or unknown):
                try:
                    await self._refresh()
                except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                    if kid not in self._keys:
                        raise httpx.HTTPError("JWKS unavailable") from None
                    # IdP 暂时不可达但缓存中有该 kid：沿用旧公钥（密钥轮换期间短暂不可用不阻断业务）
            return self._keys.get(kid)


_jwks: JWKSCache | None = None
_transport_for_tests: httpx.AsyncBaseTransport | None = None


def set_transport_for_tests(transport: httpx.AsyncBaseTransport | None) -> None:
    """测试注入：JWKS 改经给定 transport 拉取，并清空缓存。"""
    global _transport_for_tests, _jwks
    _transport_for_tests, _jwks = transport, None


def _jwks_cache() -> JWKSCache:
    """进程级 JWKS 缓存（首次使用时按配置创建）。"""
    global _jwks
    if _jwks is None:
        if not settings.jwks_url:
            raise HTTPException(500, "AUTH_REQUIRED=true but JWKS_URL is not configured")
        _jwks = JWKSCache(settings.jwks_url, settings.jwks_cache_ttl_s, _transport_for_tests)
    return _jwks


async def verify_jwt(token: str, request: Request | None = None) -> dict[str, Any]:
    """本地验签并返回 claims；任何校验失败都映射为 401，IdP 不可达映射为 503。"""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise _unauthorized(request, "malformed token") from None
    alg, kid = header.get("alg"), header.get("kid")
    if not isinstance(alg, str) or alg not in ALLOWED_ALGORITHMS:
        raise _unauthorized(request, f"unsupported token algorithm: {alg}")
    if not isinstance(kid, str) or not kid:
        raise _unauthorized(request, "token header has no kid")
    try:
        key = await _jwks_cache().get(kid)
    except httpx.HTTPError:
        raise HTTPException(503, "identity provider JWKS unavailable") from None
    if key is None:
        raise _unauthorized(request, "unknown signing key")
    if key.algorithm_name and key.algorithm_name != alg:
        raise _unauthorized(request, "token algorithm does not match signing key")
    # 未配置 JWT_AUDIENCE 时不校验受众（仅开发联调；RUNTIME_ENV=prod 启动校验要求必须配置）
    options: Options = {"require": ["exp", "iat", "sub", "iss"], "verify_aud": bool(settings.jwt_audience)}
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key.key,
            algorithms=[alg],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            leeway=settings.jwt_leeway_s,
            options=options,
        )
    except jwt.ExpiredSignatureError:
        raise _unauthorized(request, "token expired") from None
    except jwt.PyJWTError as e:
        raise _unauthorized(request, f"invalid token: {type(e).__name__}") from None
    return claims


def _act_subject(claims: dict[str, Any]) -> str | None:
    """act 声明：RFC 8693 为对象 {"sub": ...}（可嵌套），Keycloak 会话备注映射为字符串，两种都接受。"""
    act = claims.get("act")
    if isinstance(act, dict):
        sub = act.get("sub")
        return str(sub) if sub else None
    return str(act) if act else None


def principal_from_claims(claims: dict[str, Any]) -> Principal:
    """把已验签的 claims 映射为 Principal（区分用户令牌与应用的 Client Credentials 令牌）。"""
    tenant = claims.get(settings.jwt_tenant_claim)
    if not isinstance(tenant, str) or not tenant:
        raise HTTPException(403, f"token has no '{settings.jwt_tenant_claim}' claim")
    # 应用令牌只认 client_id 声明（Keycloak 服务账号会话备注，用户无法设置）；不按用户名前缀判断，
    # 否则名为 service-account-x 的普通用户会被当成应用
    client_id = claims.get("client_id") or claims.get("azp")
    realm_access = claims.get("realm_access")
    raw_roles = realm_access.get("roles", []) if isinstance(realm_access, dict) else []
    roles = frozenset(str(r) for r in raw_roles) if isinstance(raw_roles, list) else frozenset()
    if claims.get("client_id"):
        app = f"app:{client_id}"
        return Principal(subject=app, tenant_id=tenant, actor=app, auth_method="client_credentials", client_id=str(client_id), roles=roles)
    act = _act_subject(claims)
    actor = act if act else (f"app:{client_id}" if client_id else None)
    return Principal(
        subject=f"human:{claims['sub']}",
        tenant_id=tenant,
        actor=actor,
        auth_method="user",
        client_id=str(client_id) if client_id else None,
        roles=roles,
    )


async def current_principal(
    request: Request,
    authorization: str | None = Header(default=None),
    x_ai_subject: str | None = Header(default=None),
    x_ai_tenant: str | None = Header(default=None),
) -> Principal:
    """FastAPI 依赖：根据是否强制鉴权，分别走 Bearer JWT 校验或开发态的直传身份头。"""
    if settings.auth_required:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise _unauthorized(request, "missing bearer token")
        raw = authorization.split(" ", 1)[1].strip()
        claims = await verify_jwt(raw, request)
        try:
            principal = replace(principal_from_claims(claims), token=raw)
        except HTTPException as e:
            request.state.auth_error = e.detail
            raise
    else:
        principal = Principal(subject=x_ai_subject or "human:dev", tenant_id=x_ai_tenant or "default")
    request.state.principal = principal
    return principal


def is_admin(principal: Principal) -> bool:
    """是否平台管理员：强制鉴权时看 realm 角色 platform-admin；开发态不检查角色（与 require_admin 一致）。"""
    return not settings.auth_required or ADMIN_ROLE in principal.roles


async def require_admin(request: Request, principal: Principal = Depends(current_principal)) -> Principal:
    """管理 API 的依赖：强制鉴权时要求 realm 角色 platform-admin（开发态不检查）。"""
    if not is_admin(principal):
        request.state.auth_error = f"missing role {ADMIN_ROLE}"
        raise HTTPException(403, f"role '{ADMIN_ROLE}' required")
    return principal


def approver_denial(principal: Principal, run_subject: str) -> str | None:
    """审批资格（强制鉴权时）：只有人员能审批，且不能审批自己发起的 Run；有资格返回 None，否则返回拒绝原因。
    开发态不检查。"""
    if not settings.auth_required:
        return None
    if principal.auth_method != "user":
        return "approval requires a human user token"
    if principal.subject == run_subject:
        return "the run initiator cannot approve or reject their own run"
    return None


def check_approver(request: Request, principal: Principal, run_subject: str) -> None:
    """审批人校验（强制鉴权时）：只有人员能审批，且不能审批自己发起的 Run——
    否则持 Client Credentials 令牌的应用可以自己批准自己挂起的高风险操作，人工审批形同虚设。"""
    reason = approver_denial(principal, run_subject)
    if reason:
        request.state.auth_error = reason
        raise HTTPException(403, reason)

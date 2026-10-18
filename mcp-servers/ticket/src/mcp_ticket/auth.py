"""调用方身份提取（OBO 令牌 JWKS 验签）。

- 默认只接受 ``Authorization: Bearer <JWT>``。令牌在本地按 JWKS 验签（不回调 IdP），校验签名算法、
  签发者（``MCP_TICKET_ISSUER``）、受众（``MCP_TICKET_AUDIENCE``，令牌 aud 可为列表）与 exp / iat，再解析身份：
  ``sub``（终端用户）、``tenant`` 声明（缺失一律 403，不回退默认租户）、``realm_access.roles``、``azp``。
- 执行方 Agent 来自能力网关签发的 ``X-AI-Act-Assertion``（S8.4，校验见 ``mcp_template.act``）：ES256 经网关 JWKS
  （``MCP_TICKET_ACT_JWKS_URL``）验签，iss / aud / exp≤60 s / jti 防重放，sub 与 OBO 令牌一致、obo_hash 绑定本令牌，
  通过后采信 act 链。生产态（未开开发模式）缺断言 401，断言无效 401，断言不属于本请求 403。
- ``MCP_TICKET_DEV_AUTH=true``（仅本机开发）：接受 ``X-AI-Dev-Sub`` / ``X-AI-Dev-Tenant`` / ``X-AI-Dev-Roles``
  代替令牌（此时 ``X-AI-Act`` 也直接采信）；Bearer 请求未带断言时，裸 ``X-AI-Act`` 仅在令牌 ``azp`` 属于
  ``MCP_TICKET_TRUSTED_AZP``（默认仅能力网关）时采信。关闭时开发头一律忽略，缺失或无效令牌返回 401。

JWKS 按 TTL 缓存；遇到未知 kid（IdP 轮换密钥）时提前刷新，但两次刷新至少间隔 ``JWKS_MIN_REFRESH_S``，
防止伪造 kid 的请求把 IdP 打满。IdP 不可达且缓存中没有该 kid 时返回 503。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from typing import Any

import httpx
import jwt
from fastapi import Header, HTTPException
from jwt import PyJWK
from jwt.types import Options

from mcp_template.act import ActConfig, resolve_act
from mcp_ticket.config import Settings, get_settings

# 只接受非对称签名算法：拒绝 none 与 HS*（HS* 会让公开的 JWKS 公钥变成可伪造签名的共享密钥）
ALLOWED_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"})
JWKS_TTL_S = 300.0  # JWKS 缓存有效期（秒）
JWKS_MIN_REFRESH_S = 10.0  # 因未知 kid 触发的 JWKS 刷新最小间隔（秒）
JWKS_TIMEOUT_S = 5.0  # 拉取 JWKS 的超时（秒）


@dataclass(frozen=True)
class Identity:
    """调用者身份：终端用户（sub）、其租户与角色，以及代其行事的 Agent（act，可空）。"""

    sub: str
    tenant: str
    roles: frozenset[str] = frozenset()
    azp: str | None = None  # 签发令牌的 OIDC 客户端
    act: str | None = None  # 执行的 Agent，例如 agent:ticket-summary@1
    act_chain: tuple[str, ...] = ()  # 完整委托链，例如 (agent:ticket-summary@1, client:ai-runtime-svc)
    run_id: str | None = None
    call_id: str | None = None
    act_verified: bool = False  # act 是否来自已验签的网关断言


def _unauthorized(detail: str) -> HTTPException:
    """401，附 RFC 6750 的 WWW-Authenticate 头。"""
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
                    # IdP 暂时不可达但缓存中有该 kid：沿用旧公钥
            return self._keys.get(kid)


_jwks: JWKSCache | None = None
_transport_for_tests: httpx.AsyncBaseTransport | None = None


def set_transport_for_tests(transport: httpx.AsyncBaseTransport | None) -> None:
    """测试注入：JWKS 改经给定 transport 拉取，并清空缓存。"""
    global _transport_for_tests, _jwks
    _transport_for_tests, _jwks = transport, None


def _jwks_cache(url: str) -> JWKSCache:
    """进程级 JWKS 缓存（首次使用或 URL 变化时创建）。"""
    global _jwks
    if _jwks is None or _jwks.url != url:
        _jwks = JWKSCache(url, JWKS_TTL_S, _transport_for_tests)
    return _jwks


async def verify_jwt(token: str, cfg: Settings) -> dict[str, Any]:
    """本地验签并返回 claims；任何校验失败都映射为 401，IdP 不可达映射为 503。"""
    if not cfg.jwks_url or not cfg.issuer:
        # 配置缺失时拒绝而不是放行：不校验签发者的令牌等于没有验证
        raise HTTPException(500, "MCP_TICKET_JWKS_URL / MCP_TICKET_ISSUER is not configured")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise _unauthorized("malformed token") from None
    alg, kid = header.get("alg"), header.get("kid")
    if not isinstance(alg, str) or alg not in ALLOWED_ALGORITHMS:
        raise _unauthorized(f"unsupported token algorithm: {alg}")
    if not isinstance(kid, str) or not kid:
        raise _unauthorized("token header has no kid")
    try:
        key = await _jwks_cache(cfg.jwks_url).get(kid)
    except httpx.HTTPError:
        raise HTTPException(503, "identity provider JWKS unavailable") from None
    if key is None:
        raise _unauthorized("unknown signing key")
    if key.algorithm_name and key.algorithm_name != alg:
        raise _unauthorized("token algorithm does not match signing key")
    options: Options = {"require": ["exp", "iat", "sub", "iss", "aud"]}
    try:
        claims: dict[str, Any] = jwt.decode(token, key.key, algorithms=[alg], issuer=cfg.issuer, audience=cfg.audience, options=options)
    except jwt.ExpiredSignatureError:
        raise _unauthorized("token expired") from None
    except jwt.PyJWTError as e:
        raise _unauthorized(f"invalid token: {type(e).__name__}") from None
    return claims


def identity_from_claims(claims: dict[str, Any]) -> Identity:
    """把已验签的 claims 映射为 Identity（act 另由 act 断言确定）。"""
    tenant = claims.get("tenant")
    if not isinstance(tenant, str) or not tenant:
        raise HTTPException(403, "token has no 'tenant' claim")
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise _unauthorized("token has no sub")
    realm_access = claims.get("realm_access")
    raw_roles = realm_access.get("roles", []) if isinstance(realm_access, dict) else []
    roles = frozenset(str(r) for r in raw_roles) if isinstance(raw_roles, list) else frozenset()
    azp = claims.get("azp")
    azp = azp if isinstance(azp, str) and azp else None
    return Identity(sub=sub, tenant=tenant, roles=roles, azp=azp)


async def identity(
    authorization: str | None = Header(default=None),
    x_ai_act: str | None = Header(default=None),
    x_ai_act_assertion: str | None = Header(default=None),
    x_ai_dev_sub: str | None = Header(default=None),
    x_ai_dev_tenant: str | None = Header(default=None),
    x_ai_dev_roles: str | None = Header(default=None),
) -> Identity:
    """FastAPI 依赖：开发模式且带 X-AI-Dev-Sub 时直传身份，否则必须带 Bearer JWT。"""
    cfg = get_settings()
    if cfg.dev_auth and x_ai_dev_sub:
        roles = frozenset(r.strip() for r in (x_ai_dev_roles or "").split(",") if r.strip())
        act = (x_ai_act or "").strip() or None
        return Identity(sub=x_ai_dev_sub, tenant=x_ai_dev_tenant or "default", roles=roles, act=act, act_chain=(act,) if act else ())
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _unauthorized("missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    ident = identity_from_claims(await verify_jwt(token, cfg))
    ctx = await resolve_act(
        assertion=x_ai_act_assertion,
        bare_act=x_ai_act,
        obo_token=token,
        obo_sub=ident.sub,
        azp=ident.azp,
        trusted_azp=cfg.trusted_azp,
        dev_auth=cfg.dev_auth,
        cfg=ActConfig(
            jwks_url=cfg.act_jwks_url, issuer=cfg.act_issuer, audience=cfg.act_audience, replay_redis_url=cfg.act_replay_redis_url
        ),
    )
    return replace(ident, act=ctx.act, act_chain=ctx.chain, run_id=ctx.run_id, call_id=ctx.call_id, act_verified=ctx.verified)

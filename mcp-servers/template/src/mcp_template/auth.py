"""连接器通用鉴权层：OBO 令牌 JWKS 验签 + act 断言校验（S8.4）。模板与 mock 连接器共用，新连接器照此接入。

用法::

    identity = identity_dependency("MCP_KNOWLEDGE", default_audience="mcp-knowledge")

    @app.post("/tools/knowledge.search")
    def search(body: SearchIn, ident: Identity = Depends(identity)) -> dict: ...

环境变量（``<P>`` 为连接器前缀，每次请求现读，便于测试 monkeypatch）：

| 变量 | 说明 | 默认 |
|---|---|---|
| ``<P>_JWKS_URL`` | IdP 的 JWKS 地址（非开发头路径必填） | 无 |
| ``<P>_ISSUER`` | OBO 令牌签发者 iss（非开发头路径必填） | 无 |
| ``<P>_AUDIENCE`` | OBO 令牌受众 aud（令牌里可以是字符串或列表），也是 act 断言的默认受众 | ``default_audience`` |
| ``<P>_TRUSTED_AZP`` | 开发态下可携带裸 ``X-AI-Act`` 的签发客户端（逗号分隔） | ``capability-gateway`` |
| ``<P>_DEV_AUTH`` | ``true`` 为开发态：接受 ``X-AI-Dev-*`` 头代替令牌；Bearer 请求缺断言时回退裸 ``X-AI-Act`` | ``false`` |
| ``<P>_ACT_JWKS_URL`` | 能力网关 JWKS（``/.well-known/jwks.json``），校验 act 断言 | 无（收到断言时必填） |
| ``<P>_ACT_ISSUER`` | act 断言签发者 | ``capability-gateway`` |
| ``<P>_ACT_AUDIENCE`` | act 断言受众 | 同 ``<P>_AUDIENCE`` |
| ``<P>_ACT_REPLAY_REDIS_URL`` | act 断言 jti 防重放的共享存储（多副本部署必配；需可选依赖 ``redis``） | 无（进程内缓存） |

身份判定：
1. 开发态且带 ``X-AI-Dev-Sub``：直传身份（``X-AI-Dev-Tenant`` 缺省 ``default``，``X-AI-Dev-Roles`` 逗号分隔），裸 ``X-AI-Act`` 直接采信；
2. 否则必须 ``Authorization: Bearer <OBO>``：本地按 JWKS 验签（只收非对称算法），校验 iss / aud / exp / iat / sub，
   ``tenant`` 声明缺失 403；再按 :func:`mcp_template.act.resolve_act` 确定执行 Agent（生产态缺断言 401）。
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import Header, HTTPException
from jwt.types import Options

from mcp_template.act import DEFAULT_ACT_ISSUER, ActConfig, resolve_act, unauthorized
from mcp_template.jwks import JWKSUnavailable, jwks_cache

# 只接受非对称签名算法：拒绝 none 与 HS*（HS* 会让公开的 JWKS 公钥变成可伪造签名的共享密钥）
ALLOWED_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"})


def _csv(value: str) -> frozenset[str]:
    return frozenset(p.strip() for p in value.split(",") if p.strip())


@dataclass(frozen=True)
class AuthSettings:
    """一次请求内使用的鉴权配置快照。"""

    jwks_url: str | None
    issuer: str | None
    audience: str
    trusted_azp: frozenset[str]
    dev_auth: bool
    act: ActConfig


def load_auth_settings(prefix: str, default_audience: str) -> AuthSettings:
    """按前缀从环境变量读取鉴权配置。"""
    env = os.environ
    audience = env.get(f"{prefix}_AUDIENCE") or default_audience
    return AuthSettings(
        jwks_url=env.get(f"{prefix}_JWKS_URL") or None,
        issuer=env.get(f"{prefix}_ISSUER") or None,
        audience=audience,
        trusted_azp=_csv(env.get(f"{prefix}_TRUSTED_AZP", "capability-gateway")),
        dev_auth=env.get(f"{prefix}_DEV_AUTH", "").strip().lower() == "true",
        act=ActConfig(
            jwks_url=env.get(f"{prefix}_ACT_JWKS_URL") or None,
            issuer=env.get(f"{prefix}_ACT_ISSUER") or DEFAULT_ACT_ISSUER,
            audience=env.get(f"{prefix}_ACT_AUDIENCE") or audience,
            replay_redis_url=env.get(f"{prefix}_ACT_REPLAY_REDIS_URL") or None,
        ),
    )


@dataclass(frozen=True)
class Identity:
    """调用者身份：终端用户（sub）、租户、角色，以及代其行事的 Agent 与委托链。"""

    sub: str
    tenant: str = "default"
    roles: frozenset[str] = frozenset()
    azp: str | None = None  # 签发 OBO 令牌的客户端
    act: str | None = None  # 执行 Agent，例如 agent:ticket-summary@1
    act_chain: tuple[str, ...] = ()  # 完整委托链，例如 (agent:x@1, client:ai-runtime-svc)
    run_id: str | None = None
    call_id: str | None = None
    act_verified: bool = False  # act 是否来自已验签的网关断言
    audience: str | None = None  # 已校验的 OBO 受众（开发头路径为 None）


async def verify_obo(token: str, cfg: AuthSettings) -> dict[str, Any]:
    """本地验签 OBO 令牌并返回 claims；校验失败 401，IdP JWKS 不可达 503，配置缺失 500。"""
    if not cfg.jwks_url or not cfg.issuer:
        raise HTTPException(500, "JWKS_URL / ISSUER is not configured")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise unauthorized("malformed token") from None
    alg, kid = header.get("alg"), header.get("kid")
    if not isinstance(alg, str) or alg not in ALLOWED_ALGORITHMS:
        raise unauthorized(f"unsupported token algorithm: {alg}")
    if not isinstance(kid, str) or not kid:
        raise unauthorized("token header has no kid")
    try:
        key = await jwks_cache(cfg.jwks_url).get(kid)
    except JWKSUnavailable:
        raise HTTPException(503, "identity provider JWKS unavailable") from None
    if key is None:
        raise unauthorized("unknown signing key")
    if key.algorithm_name and key.algorithm_name != alg:
        raise unauthorized("token algorithm does not match signing key")
    options: Options = {"require": ["exp", "iat", "sub", "iss", "aud"]}
    try:
        claims: dict[str, Any] = jwt.decode(token, key.key, algorithms=[alg], issuer=cfg.issuer, audience=cfg.audience, options=options)
    except jwt.ExpiredSignatureError:
        raise unauthorized("token expired") from None
    except jwt.PyJWTError as e:
        raise unauthorized(f"invalid token: {type(e).__name__}") from None
    return claims


def _roles(claims: dict[str, Any]) -> frozenset[str]:
    realm_access = claims.get("realm_access")
    raw = realm_access.get("roles", []) if isinstance(realm_access, dict) else []
    return frozenset(str(r) for r in raw) if isinstance(raw, list) else frozenset()


def identity_dependency(prefix: str, default_audience: str) -> Callable[..., Awaitable[Identity]]:
    """生成 FastAPI 依赖：按 ``prefix`` 读配置，返回已验证的 :class:`Identity`。"""

    async def identity(
        authorization: str | None = Header(default=None),
        x_ai_act: str | None = Header(default=None),
        x_ai_act_assertion: str | None = Header(default=None),
        x_ai_dev_sub: str | None = Header(default=None),
        x_ai_dev_tenant: str | None = Header(default=None),
        x_ai_dev_roles: str | None = Header(default=None),
    ) -> Identity:
        cfg = load_auth_settings(prefix, default_audience)
        if cfg.dev_auth and x_ai_dev_sub:
            roles = frozenset(r.strip() for r in (x_ai_dev_roles or "").split(",") if r.strip())
            act = (x_ai_act or "").strip() or None
            return Identity(sub=x_ai_dev_sub, tenant=x_ai_dev_tenant or "default", roles=roles, act=act, act_chain=(act,) if act else ())
        if not authorization or not authorization.lower().startswith("bearer "):
            raise unauthorized("missing bearer token")
        token = authorization.split(" ", 1)[1].strip()
        claims = await verify_obo(token, cfg)
        tenant = claims.get("tenant")
        if not isinstance(tenant, str) or not tenant:
            raise HTTPException(403, "token has no 'tenant' claim")
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise unauthorized("token has no sub")
        azp = claims.get("azp")
        azp = azp if isinstance(azp, str) and azp else None
        ctx = await resolve_act(
            assertion=x_ai_act_assertion,
            bare_act=x_ai_act,
            obo_token=token,
            obo_sub=sub,
            azp=azp,
            trusted_azp=cfg.trusted_azp,
            dev_auth=cfg.dev_auth,
            cfg=cfg.act,
        )
        return Identity(
            sub=sub,
            tenant=tenant,
            roles=_roles(claims),
            azp=azp,
            act=ctx.act,
            act_chain=ctx.chain,
            run_id=ctx.run_id,
            call_id=ctx.call_id,
            act_verified=ctx.verified,
            audience=cfg.audience,
        )

    return identity

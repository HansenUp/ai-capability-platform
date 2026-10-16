"""Identity extraction. Production: verify the OIDC JWT locally against JWKS (Keycloak) and read
`sub` (user) + `act` (acting agent/app). Dev: AUTH_REQUIRED=false accepts an `X-AI-Subject` header.
（身份提取：生产环境下应基于 JWKS（如 Keycloak）在本地校验 OIDC JWT，读取 `sub`（用户）与
`act`（实际代为执行的 agent/app）声明；开发环境下 AUTH_REQUIRED=false 时直接信任
`X-AI-Subject` 请求头，便于本地联调。）
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, HTTPException

from runtime.config import settings


@dataclass(frozen=True)
class Principal:
    """经身份提取后得到的调用主体（用于鉴权、审计与 OBO 场景下的 acting agent 记录）。"""

    subject: str  # "human:<id>" | "agent:<id>" | "app:<id>" | "service:<id>"
    tenant_id: str = "default"  # 租户标识
    actor: str | None = None  # act claim: executing agent（act 声明：实际代为执行的 agent）


async def current_principal(
    authorization: str | None = Header(default=None),
    x_ai_subject: str | None = Header(default=None),
    x_ai_tenant: str | None = Header(default=None),
) -> Principal:
    """FastAPI 依赖：根据是否强制鉴权，分别走 Bearer JWT 校验或开发态的直传身份头。"""
    if settings.auth_required:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "missing bearer token")
        claims = _verify_jwt(authorization.split(" ", 1)[1])
        return Principal(
            subject=f"human:{claims['sub']}", tenant_id=claims.get("tenant", "default"), actor=claims.get("act", {}).get("sub")
        )
    return Principal(subject=x_ai_subject or "human:dev", tenant_id=x_ai_tenant or "default")


def _verify_jwt(token: str) -> dict:
    """JWKS verification hook. Wire python-jose/PyJWT + cached JWKS from settings.jwks_url in S2.
    （JWT 验签占位钩子：S2 工作包实现——接入 python-jose/PyJWT，并基于 settings.jwks_url
    做带缓存的 JWKS 拉取与验签。当前直接返回 501，表示该能力尚未接线。）
    """
    raise HTTPException(501, "JWT verification not configured (set JWKS_URL and implement in S2)")

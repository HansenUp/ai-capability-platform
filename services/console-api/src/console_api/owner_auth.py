from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import jwt
from fastapi import HTTPException, Request
from sqlalchemy import select

from console_api.auth import Role
from console_api.db import Agent, SessionLocal, UserAgent

if TYPE_CHECKING:
    from console_api.owner_jwks import IsolatedJwks


@dataclass(frozen=True)
class Binding:
    issuer: str
    tenant: str
    subject: str
    user: str


@dataclass(frozen=True)
class LocalProfile:
    issuer: str
    audience: str
    client: str
    public_jwk: str
    bindings: tuple[Binding, ...]
    roles: tuple[tuple[str, Role], ...]
    access_contract: bool = False
    jwks: IsolatedJwks | None = None


@dataclass(frozen=True)
class Owner:
    tenant: str
    subject: str
    user: str
    role: Role


_profile: LocalProfile | None = None


def now() -> datetime:
    return datetime.now(UTC)


def error(status: int, detail: str) -> HTTPException:
    return HTTPException(status, detail)


def authenticate(request: Request, profile: LocalProfile | None = None, *, public_jwk: str | None = None) -> tuple[LocalProfile, Owner]:
    current = _profile
    if os.environ.get("CONSOLE_ENV", "dev") != "dev" or os.environ.get("CONSOLE_OWNER_LOCAL_READ") != "1" or current is None:
        raise error(503, "Owner local read profile is disabled")
    if profile is not None and profile is not current:
        raise error(403, "Owner identity binding changed")
    if any(name.lower().startswith("x-console-") for name in request.headers):
        raise error(400, "Owner identity overrides are not accepted")
    raw = request.headers.getlist("authorization")
    if len(raw) != 1 or not raw[0].startswith("Bearer ") or not 0 < len(raw[0][7:]) <= 16384:
        raise error(401, "verified Owner user token required")
    try:
        key_body = json.loads(public_jwk if public_jwk is not None else current.public_jwk)
        if (
            not isinstance(key_body, dict)
            or key_body.get("kty") != "RSA"
            or "d" in key_body
            or not key_body.get("kid")
            or key_body.get("use", "sig") != "sig"
        ):
            raise ValueError
        if not current.issuer or not current.audience or not current.client or not all(isinstance(role, Role) for _, role in current.roles):
            raise ValueError
        keys = [(binding.issuer, binding.tenant, binding.subject) for binding in current.bindings]
        users = [(binding.tenant, binding.user) for binding in current.bindings]
        if len(set(keys)) != len(keys) or len(set(users)) != len(users) or len(dict(current.roles)) != len(current.roles):
            raise ValueError
        if not all(
            binding.issuer == current.issuer and binding.tenant and binding.subject and binding.user for binding in current.bindings
        ):
            raise ValueError
        key = jwt.PyJWK.from_dict(key_body, algorithm="RS256").key
    except (ValueError, KeyError, TypeError, jwt.PyJWTError) as exc:
        raise error(503, "Owner identity configuration unavailable") from exc
    try:
        header = jwt.get_unverified_header(raw[0][7:])
        if (
            header.get("alg") != "RS256"
            or header.get("kid") != key_body["kid"]
            or any(name in header for name in ("jku", "jwk", "x5u", "crit"))
        ):
            raise ValueError
        claims = jwt.decode(
            raw[0][7:],
            key,
            algorithms=["RS256"],
            issuer=current.issuer,
            audience=current.audience,
            options={"require": ["iss", "aud", "sub", "tenant", "exp", "iat", "nbf", "azp", "identity_type"], "strict_aud": True},
        )
        for field in ("exp", "iat", "nbf"):
            if type(claims[field]) is not int:
                raise ValueError
        stamp = now().timestamp()
        if claims["exp"] <= stamp or claims["iat"] > stamp or claims["nbf"] > stamp:
            raise ValueError
        if current.access_contract and (
            header.get("typ") != "at+jwt"
            or claims.get("token_use") != "access"
            or not 0 < claims["exp"] - claims["iat"] <= 600
            or claims["nbf"] > claims["exp"]
        ):
            raise ValueError
        if not all(isinstance(claims[name], str) and claims[name] for name in ("tenant", "sub")):
            raise ValueError
    except (ValueError, KeyError, TypeError, jwt.PyJWTError) as exc:
        raise error(401, "invalid Owner user token") from exc
    if (
        claims["identity_type"] != "user"
        or claims["azp"] != current.client
        or any(name in claims for name in ("act", "may_act", "client_id"))
        or str(claims.get("preferred_username", "")).startswith("service-account-")
    ):
        raise error(403, "Owner requires a direct user identity")
    roles = claims.get("realm_access", {}).get("roles") if isinstance(claims.get("realm_access"), dict) else None
    if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
        raise error(403, "Owner role not granted")
    granted = max((role for name, role in current.roles if name in roles), default=Role.USER)
    binding = next(
        (b for b in current.bindings if (b.issuer, b.tenant, b.subject) == (current.issuer, claims["tenant"], claims["sub"])), None
    )
    if granted < Role.ADMIN or binding is None:
        raise error(403, "Owner identity or role not granted")
    return current, Owner(binding.tenant, binding.subject, binding.user, granted)


async def authenticate_async(request: Request, profile: LocalProfile | None = None) -> tuple[LocalProfile, Owner]:
    current = _profile
    if current is None or current.jwks is None:
        return authenticate(request, profile)
    if not current.access_contract:
        raise error(503, "Owner JWKS requires the access token contract")
    if os.environ.get("CONSOLE_ENV", "dev") != "dev" or os.environ.get("CONSOLE_OWNER_LOCAL_READ") != "1":
        raise error(503, "Owner local read profile is disabled")
    if profile is not None and profile is not current:
        raise error(403, "Owner identity binding changed")
    if any(name.lower().startswith("x-console-") for name in request.headers):
        raise error(400, "Owner identity overrides are not accepted")
    raw = request.headers.getlist("authorization")
    try:
        if len(raw) != 1 or not raw[0].startswith("Bearer ") or not 0 < len(raw[0][7:]) <= 16384:
            raise ValueError
        header = jwt.get_unverified_header(raw[0][7:])
        if header.get("alg") != "RS256" or any(name in header for name in ("jku", "jwk", "x5u", "crit")):
            raise ValueError
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise ValueError
    except (ValueError, jwt.PyJWTError) as exc:
        raise error(401, "invalid Owner user token") from exc
    try:
        key = await current.jwks.resolve(kid)
    except ValueError as exc:
        raise error(503, "Owner JWKS unavailable") from exc
    if current is not _profile:
        raise error(403, "Owner identity binding changed")
    return authenticate(request, current, public_jwk=key)


async def assigned(owner: Owner, agent_id: str) -> None:
    async with SessionLocal() as session:
        statement = (
            select(Agent.id)
            .join(UserAgent, (UserAgent.tenant_id == Agent.tenant_id) & (UserAgent.agent_id == Agent.agent_id))
            .where(Agent.tenant_id == owner.tenant, Agent.agent_id == agent_id, UserAgent.user == owner.user)
        )
        if await session.scalar(statement) is None:
            raise error(404, "Owner Agent unavailable")

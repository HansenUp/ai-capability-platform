"""Experimental verified OBO + gateway act pairing for isolated integration only."""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import jwt

from .query import QueryDenied
from .reviewed import VerifiedUser


@dataclass(frozen=True)
class DelegationProfile:
    obo_issuer: str
    audience: str
    gateway_client: str
    act_issuer: str
    runtime_client: str
    agent_id: str
    agent_version: str
    run_id: str
    call_id: str


class DelegatedIdentity:
    """Both verifiers must check signature, issuer, audience, expiry and required claims.

    No network routes or replay store are created. Test-only verifier injection does not
    establish production trust or prove that an identity has not been revoked.
    """

    def __init__(
        self,
        profile: DelegationProfile,
        tokens: Callable[[], Awaitable[tuple[str, str]]],
        obo_verify: Callable[[str], Awaitable[Mapping[str, Any]]],
        act_verify: Callable[[str], Awaitable[Mapping[str, Any]]],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._profile = profile
        self._tokens = tokens
        self._obo_verify = obo_verify
        self._act_verify = act_verify
        self._clock = clock

    async def __call__(self) -> VerifiedUser:
        try:
            profile = self._profile
            if type(profile) is not DelegationProfile or any(not isinstance(v, str) or not v.strip() for v in vars(profile).values()):
                raise QueryDenied()
            obo, assertion = await self._tokens()
            if not isinstance(obo, str) or not obo or not isinstance(assertion, str) or not assertion:
                raise QueryDenied()
            user = await self._obo_verify(obo)
            if jwt.get_unverified_header(obo).get("typ") != "at+jwt":
                raise QueryDenied()
            act = await self._act_verify(assertion)
            now = self._clock()
            roles = user.get("realm_access", {}).get("roles") if isinstance(user.get("realm_access"), dict) else None
            expected_act = {"sub": f"agent:{profile.agent_id}@{profile.agent_version}", "act": {"sub": f"client:{profile.runtime_client}"}}
            digest = hashlib.sha256(obo.encode()).hexdigest()
            if (
                user.get("iss") != profile.obo_issuer
                or user.get("aud") != profile.audience
                or user.get("azp") != profile.gateway_client
                or user.get("token_use") != "access"
                or user.get("principal_type") != "user"
                or not isinstance(user.get("sub"), str)
                or not user["sub"].strip()
                or not isinstance(user.get("tenant"), str)
                or not user["tenant"].strip()
                or not isinstance(roles, list)
                or len(roles) > 64
                or any(not isinstance(r, str) or not r.strip() for r in roles)
                or len(set(roles)) != len(roles)
                or type(user.get("iat")) is not int
                or type(user.get("exp")) is not int
                or not 0 < user["exp"] - user["iat"] <= 600
                or not user["iat"] <= now < user["exp"]
                or act.get("iss") != profile.act_issuer
                or act.get("aud") != profile.audience
                or act.get("sub") != user["sub"]
                or act.get("act") != expected_act
                or act.get("run_id") != profile.run_id
                or act.get("call_id") != profile.call_id
                or not isinstance(act.get("jti"), str)
                or not act["jti"].strip()
                or not isinstance(act.get("obo_hash"), str)
                or not hmac.compare_digest(act["obo_hash"], digest)
                or type(act.get("iat")) is not int
                or type(act.get("exp")) is not int
                or not 0 < act["exp"] - act["iat"] <= 60
                or not act["iat"] <= now < act["exp"]
            ):
                raise QueryDenied()
            return VerifiedUser(
                user["iss"],
                user["aud"],
                user["azp"],
                user["tenant"],
                user["sub"],
                frozenset(roles),
                user["iat"],
                min(user["exp"], act["exp"]),
                "access",
                "at+jwt",
                "user",
                True,
            )
        except Exception:
            raise QueryDenied() from None

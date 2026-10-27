"""Injected local JWT profile adapter; no configured IdP or network client."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import jwt

from .query import QueryDenied
from .reviewed import UserProfile, VerifiedUser


class LocalJwtIdentity:
    """Verifier must authenticate issuer, audience, signature, exp and iat.

    The verifier and token provider are explicit trusted host inputs. This
    experimental profile is not a production IdP or service-token adapter.
    """

    def __init__(
        self,
        profile: UserProfile,
        token_provider: Callable[[], Awaitable[str]],
        verifier: Callable[[str, str], Awaitable[Mapping[str, Any]]],
    ) -> None:
        self._profile = profile
        self._token_provider = token_provider
        self._verifier = verifier

    async def __call__(self) -> VerifiedUser:
        try:
            token = await self._token_provider()
            if not isinstance(token, str) or not token:
                raise QueryDenied()
            header = jwt.get_unverified_header(token)
            claims = await self._verifier(token, self._profile.audience)
            roles = claims.get("realm_access", {}).get("roles") if isinstance(claims.get("realm_access"), dict) else None
            if (
                header.get("typ") != "at+jwt"
                or claims.get("iss") != self._profile.issuer
                or claims.get("aud") != self._profile.audience
                or claims.get("azp") not in self._profile.clients
                or claims.get("token_use") != "access"
                or claims.get("principal_type") != "user"
                or "client_identity" in claims
                or "act" in claims
                or not isinstance(claims.get("sub"), str)
                or not claims["sub"].strip()
                or not isinstance(claims.get("tenant"), str)
                or not claims["tenant"].strip()
                or type(claims.get("iat")) is not int
                or type(claims.get("exp")) is not int
                or not isinstance(roles, list)
                or len(roles) > 64
                or any(not isinstance(role, str) or not role.strip() for role in roles)
                or len(set(roles)) != len(roles)
            ):
                raise QueryDenied()
            return VerifiedUser(
                claims["iss"],
                claims["aud"],
                claims["azp"],
                claims["tenant"],
                claims["sub"],
                frozenset(roles),
                claims["iat"],
                claims["exp"],
                claims["token_use"],
                header["typ"],
                "user",
                False,
            )
        except Exception:
            raise QueryDenied() from None

"""Isolated reviewed-view adapter. Providers and approvals are injected test doubles."""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from .query import Identity, Query, QueryDenied, Registry, Result, _quote, _verify_metadata, query_view

_SHA = re.compile(r"[a-f0-9]{64}\Z")
_ACCESS_USE = "access"
_AT_JWT = "at+jwt"


@dataclass(frozen=True)
class VerifiedUser:
    issuer: str
    audience: str
    client_id: str
    tenant: str
    subject: str
    roles: frozenset[str]
    issued_at: int
    expires_at: int
    token_use: str
    token_type: str
    kind: str
    delegated: bool


@dataclass(frozen=True)
class UserProfile:
    issuer: str
    audience: str
    clients: frozenset[str]
    allow_delegated: bool = False


@dataclass(frozen=True)
class ReviewedBinding:
    tenant: str
    view_id: str
    version: str
    sqlite_view: str
    source_table: str
    tenant_column: str
    view_sha256: str
    source_sha256: str
    generation: int
    maintainer: str
    reviewers: frozenset[str]
    state: str


@dataclass(frozen=True)
class AccessRequest:
    user: VerifiedUser
    view_id: str
    version: str
    generation: int
    view_sha256: str
    source_sha256: str
    columns: tuple[str, ...]
    filter_columns: tuple[str, ...]
    limit: int
    action: str = "read_data"
    resource_kind: str = "DataView"


@dataclass(frozen=True)
class AccessDecision:
    request: AccessRequest
    effect: str
    policy_version: str


def canonical_view_sql(view: str, source: str, tenant_column: str, columns: tuple[str, ...]) -> str:
    """The local C3 profile allows only a direct, unaliased, single-table projection."""
    names = (tenant_column, *(column for column in columns if column != tenant_column))
    if len(set(names)) != len(names):
        raise QueryDenied()
    # All identifiers are ASCII-validated; this string is compared, not executed by the adapter.
    return f"CREATE VIEW {_quote(view)} AS SELECT {', '.join(_quote(name) for name in names)} FROM {_quote(source)}"  # noqa: S608


class ReviewedQuery:
    """No default IdP, registration source or policy; never enable as a production boundary.

    Two checks narrow the revocation race but do not provide a cross-service atomic snapshot.
    The direct view profile establishes syntactic lineage only; mislabeled source rows remain
    a trusted-data and human-review obligation.
    """

    def __init__(
        self,
        database: Path,
        registry: Registry,
        profile: UserProfile,
        identity_provider: Callable[[], Awaitable[VerifiedUser]],
        binding_provider: Callable[[Identity, Query], Awaitable[ReviewedBinding]],
        policy_provider: Callable[[AccessRequest], Awaitable[AccessDecision]],
        *,
        gate_timeout_s: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if (
            type(profile) is not UserProfile
            or not isinstance(profile.issuer, str)
            or not profile.issuer.strip()
            or not isinstance(profile.audience, str)
            or not profile.audience.strip()
            or type(profile.allow_delegated) is not bool
            or not isinstance(profile.clients, frozenset)
            or not profile.clients
            or any(not isinstance(client, str) or not client.strip() for client in profile.clients)
            or isinstance(gate_timeout_s, bool)
            or not isinstance(gate_timeout_s, (int, float))
            or not math.isfinite(gate_timeout_s)
            or gate_timeout_s <= 0
        ):
            raise QueryDenied()
        self._database = database
        self._registry = registry
        self._profile = profile
        self._identity = identity_provider
        self._binding = binding_provider
        self._policy = policy_provider
        self._gate_timeout_s = gate_timeout_s
        self._clock = clock

    async def _check(self, query: Query) -> tuple[VerifiedUser, ReviewedBinding, AccessDecision]:
        user = await self._identity()
        now = self._clock()
        if (
            type(user) is not VerifiedUser
            or type(now) not in (int, float)
            or not math.isfinite(now)
            or any(
                not isinstance(value, str) or not value.strip()
                for value in (user.issuer, user.audience, user.client_id, user.tenant, user.subject)
            )
            or user.issuer != self._profile.issuer
            or user.audience != self._profile.audience
            or user.client_id not in self._profile.clients
            or user.token_use != _ACCESS_USE
            or user.token_type != _AT_JWT
            or user.kind != "user"
            or user.delegated is not self._profile.allow_delegated
            or type(user.issued_at) is not int
            or type(user.expires_at) is not int
            or not 0 < user.expires_at - user.issued_at <= 600
            or user.issued_at > now
            or user.expires_at <= now
            or not isinstance(user.roles, frozenset)
            or any(not isinstance(role, str) or not role for role in user.roles)
        ):
            raise QueryDenied()
        identity = Identity(user.tenant, user.subject, user.roles)
        rule = self._registry.resolve(identity, query)
        binding = await self._binding(identity, query)
        if (
            type(binding) is not ReviewedBinding
            or (binding.tenant, binding.view_id, binding.version, binding.sqlite_view, binding.tenant_column)
            != (identity.tenant, query.view_id, query.version, rule.sqlite_view, rule.tenant_column)
            or binding.state != "active"
            or type(binding.generation) is not int
            or binding.generation < 1
            or not isinstance(binding.maintainer, str)
            or not binding.maintainer.strip()
            or not isinstance(binding.reviewers, frozenset)
            or len(binding.reviewers) < 2
            or binding.maintainer in binding.reviewers
            or any(not isinstance(name, str) or not name.strip() for name in binding.reviewers)
            or not isinstance(binding.view_sha256, str)
            or not _SHA.fullmatch(binding.view_sha256)
            or not isinstance(binding.source_sha256, str)
            or not _SHA.fullmatch(binding.source_sha256)
        ):
            raise QueryDenied()
        expected = canonical_view_sql(binding.sqlite_view, binding.source_table, binding.tenant_column, rule.columns)
        if hashlib.sha256(expected.encode("utf-8")).hexdigest() != binding.view_sha256:
            raise QueryDenied()
        scope = AccessRequest(
            user,
            query.view_id,
            query.version,
            binding.generation,
            binding.view_sha256,
            binding.source_sha256,
            query.columns,
            tuple(name for name, _ in query.filters),
            query.limit,
        )
        decision = await self._policy(scope)
        if (
            type(decision) is not AccessDecision
            or type(decision.request) is not AccessRequest
            or decision.request != scope
            or type(decision.effect) is not str
            or decision.effect != "allow"
            or type(decision.policy_version) is not str
            or not decision.policy_version.strip()
        ):
            raise QueryDenied()
        return user, binding, decision

    async def query(self, request: Query) -> Result:
        try:
            initial_user, initial_binding, initial_decision = await self._timed_check(request)
            identity = Identity(initial_user.tenant, initial_user.subject, initial_user.roles)
            result = query_view(
                self._database,
                self._registry,
                identity,
                request,
                expected_definition_sha256=initial_binding.view_sha256,
                expected_source_table=initial_binding.source_table,
                expected_source_sha256=initial_binding.source_sha256,
            )
            final_user, final_binding, final_decision = await self._timed_check(request)
            if (final_user, final_binding, final_decision) != (initial_user, initial_binding, initial_decision):
                raise QueryDenied()
            _verify_metadata(
                self._database,
                self._registry.resolve(Identity(final_user.tenant, final_user.subject, final_user.roles), request),
                expected_definition_sha256=final_binding.view_sha256,
                expected_source_table=final_binding.source_table,
                expected_source_sha256=final_binding.source_sha256,
            )
            return result
        except Exception:
            raise QueryDenied() from None

    async def _timed_check(self, request: Query) -> tuple[VerifiedUser, ReviewedBinding, AccessDecision]:
        deadline = asyncio.get_running_loop().time() + self._gate_timeout_s
        async with asyncio.timeout(self._gate_timeout_s):
            result = await self._check(request)
        if asyncio.get_running_loop().time() >= deadline:
            raise QueryDenied()
        now = self._clock()
        if type(now) not in (int, float) or not math.isfinite(now) or result[0].expires_at <= now:
            raise QueryDenied()
        return result

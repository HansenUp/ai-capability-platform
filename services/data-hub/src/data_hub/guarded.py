"""Explicitly injected local boundary experiment; not a production authorization adapter."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from .query import Identity, Query, QueryDenied, Registry, Result, query_view


@dataclass(frozen=True)
class SchemaPin:
    tenant: str
    view_id: str
    version: str
    sqlite_view: str
    definition_sha256: str


@dataclass(frozen=True)
class PolicyRequest:
    action: str
    tenant: str
    subject: str
    roles: frozenset[str]
    view_id: str
    version: str
    columns: tuple[str, ...]
    filter_columns: tuple[str, ...]
    limit: int


@dataclass(frozen=True)
class PolicyVerdict:
    request: PolicyRequest
    effect: str
    policy_version: str


class GuardedQuery:
    """Only explicit trusted host injection; no default verifier, policy or public endpoint.

    Pins detect changes to the SQLite view definition, not tenant lineage or base-table
    changes. The real service authentication, policy mapping and review remain unimplemented.
    """

    def __init__(
        self,
        database: Path,
        registry: Registry,
        pins: tuple[SchemaPin, ...],
        identity_provider: Callable[[], Awaitable[Identity]],
        policy_provider: Callable[[PolicyRequest], Awaitable[PolicyVerdict]],
    ) -> None:
        if not isinstance(pins, tuple):
            raise QueryDenied()
        index: dict[tuple[str, str, str], SchemaPin] = {}
        for pin in pins:
            if (
                not isinstance(pin, SchemaPin)
                or any(not isinstance(v, str) or not v for v in (pin.tenant, pin.view_id, pin.version, pin.sqlite_view))
                or not isinstance(pin.definition_sha256, str)
                or not re.fullmatch(r"[a-f0-9]{64}", pin.definition_sha256)
            ):
                raise QueryDenied()
            key = (pin.tenant, pin.view_id, pin.version)
            if key in index:
                raise QueryDenied()
            index[key] = pin
        self._database = database
        self._registry = registry
        self._pins = MappingProxyType(index)
        self._identity = identity_provider
        self._policy = policy_provider

    async def query(self, request: Query) -> Result:
        try:
            identity = await self._identity()
            rule = self._registry.resolve(identity, request)
            pin = self._pins.get((identity.tenant, request.view_id, request.version))
            if pin is None or pin.sqlite_view != rule.sqlite_view:
                raise QueryDenied()
            scope = PolicyRequest(
                "read_data",
                identity.tenant,
                identity.subject,
                identity.roles,
                request.view_id,
                request.version,
                request.columns,
                tuple(name for name, _ in request.filters),
                request.limit,
            )
            verdict = await self._policy(scope)
            if (
                type(verdict) is not PolicyVerdict
                or type(verdict.request) is not PolicyRequest
                or type(verdict.effect) is not str
                or verdict.effect != "allow"
                or verdict.request != scope
                or type(verdict.policy_version) is not str
                or not verdict.policy_version.strip()
            ):
                raise QueryDenied()
        except Exception:
            raise QueryDenied() from None
        return query_view(self._database, self._registry, identity, request, expected_definition_sha256=pin.definition_sha256)

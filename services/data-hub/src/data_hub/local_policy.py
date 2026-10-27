"""Explicit in-memory DataView policy for isolated tests, not a Cedar permit."""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType

from .query import QueryDenied
from .reviewed import AccessDecision, AccessRequest


@dataclass(frozen=True)
class LocalPolicyRule:
    tenant: str
    view_id: str
    version: str
    generation: int
    view_sha256: str
    source_sha256: str
    subjects: frozenset[str]
    roles: frozenset[str]
    columns: frozenset[str]
    filter_columns: frozenset[str]
    max_limit: int


class LocalReadDataPolicy:
    """No persistence, global roles, network or production policy registration."""

    def __init__(self, version: str, rules: tuple[LocalPolicyRule, ...]) -> None:
        if not isinstance(version, str) or not version.strip() or not isinstance(rules, tuple):
            raise QueryDenied()
        index: dict[tuple[str, str, str, int], LocalPolicyRule] = {}
        for rule in rules:
            if (
                type(rule) is not LocalPolicyRule
                or any(not isinstance(v, str) or not v.strip() for v in (rule.tenant, rule.view_id, rule.version))
                or type(rule.generation) is not int
                or rule.generation < 1
                or any(not isinstance(v, str) or not re.fullmatch(r"[a-f0-9]{64}", v) for v in (rule.view_sha256, rule.source_sha256))
                or any(not isinstance(v, frozenset) for v in (rule.subjects, rule.roles, rule.columns, rule.filter_columns))
                or not (rule.subjects or rule.roles)
                or not rule.columns
                or not rule.filter_columns <= rule.columns
                or any(not isinstance(item, str) or not item.strip() for item in rule.subjects | rule.roles | rule.columns)
                or type(rule.max_limit) is not int
                or not 1 <= rule.max_limit <= 100
            ):
                raise QueryDenied()
            key = (rule.tenant, rule.view_id, rule.version, rule.generation)
            if key in index:
                raise QueryDenied()
            index[key] = rule
        self._version = version
        self._rules = MappingProxyType(index)

    async def __call__(self, request: AccessRequest) -> AccessDecision:
        if type(request) is not AccessRequest:
            raise QueryDenied()
        rule = self._rules.get((request.user.tenant, request.view_id, request.version, request.generation))
        allow = (
            rule is not None
            and request.action == "read_data"
            and request.resource_kind == "DataView"
            and (request.user.subject in rule.subjects or bool(request.user.roles.intersection(rule.roles)))
            and request.view_sha256 == rule.view_sha256
            and request.source_sha256 == rule.source_sha256
            and bool(request.columns)
            and set(request.columns) <= rule.columns
            and set(request.filter_columns) <= rule.filter_columns
            and type(request.limit) is int
            and 1 <= request.limit <= rule.max_limit
        )
        return AccessDecision(request, "allow" if allow else "deny", self._version)

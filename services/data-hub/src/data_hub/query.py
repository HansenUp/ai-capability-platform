"""Restricted SQLite VIEW queries with injected, trusted local test policy."""

from __future__ import annotations

import hashlib
import hmac
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TypeAlias

Scalar: TypeAlias = str | int | None
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_MASK = "[REDACTED]"


class QueryDenied(ValueError):
    """Request is malformed, unauthorized, or outside the registered scope."""

    def __init__(self) -> None:
        super().__init__("Restricted query denied")


class QueryUnavailable(RuntimeError):
    """Do not expose database errors or query text to callers."""

    def __init__(self) -> None:
        super().__init__("Restricted query unavailable")


def _identifier(name: object) -> str:
    if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name):
        raise QueryDenied()
    return name


def _quote(name: str) -> str:
    return f'"{_identifier(name)}"'


def _plain_table_definition(sql: str) -> bool:
    """Fail closed for virtual tables even when DDL whitespace varies."""
    return re.match(r"\ACREATE\s+TABLE\s", sql.lstrip(), flags=re.IGNORECASE) is not None


@dataclass(frozen=True)
class Identity:
    """Only a trusted host may construct this; this is not an authentication adapter."""

    tenant: str
    subject: str
    roles: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ViewRule:
    tenant: str
    view_id: str
    version: str
    sqlite_view: str
    tenant_column: str
    columns: tuple[str, ...]
    masked_columns: frozenset[str]
    subjects: frozenset[str] = frozenset()
    roles: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Query:
    view_id: str
    version: str
    columns: tuple[str, ...]
    filters: tuple[tuple[str, Scalar], ...]
    limit: int


@dataclass(frozen=True)
class Result:
    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]
    truncated: bool
    view_version: str


class Registry:
    """Immutable, explicitly supplied configuration; no registration endpoint."""

    def __init__(self, rules: tuple[ViewRule, ...]) -> None:
        if not isinstance(rules, tuple):
            raise QueryDenied()
        registered: dict[tuple[str, str, str], ViewRule] = {}
        for rule in rules:
            if (
                not isinstance(rule, ViewRule)
                or not isinstance(rule.tenant, str)
                or not rule.tenant
                or not isinstance(rule.version, str)
                or not rule.version
                or not isinstance(rule.columns, tuple)
                or not isinstance(rule.masked_columns, frozenset)
                or not isinstance(rule.subjects, frozenset)
                or not isinstance(rule.roles, frozenset)
                or not (rule.subjects or rule.roles)
                or any(not isinstance(item, str) or not item for item in rule.subjects | rule.roles)
            ):
                raise QueryDenied()
            _identifier(rule.view_id)
            _identifier(rule.sqlite_view)
            _identifier(rule.tenant_column)
            if (
                not rule.columns
                or any(not isinstance(column, str) for column in rule.columns)
                or len(set(rule.columns)) != len(rule.columns)
            ):
                raise QueryDenied()
            for column in rule.columns:
                _identifier(column)
            if not rule.masked_columns <= frozenset(rule.columns):
                raise QueryDenied()
            key = (rule.tenant, rule.view_id, rule.version)
            if key in registered:
                raise QueryDenied()
            registered[key] = rule
        self._rules = MappingProxyType(registered)

    def resolve(self, identity: Identity, query: Query) -> ViewRule:
        if (
            not isinstance(identity, Identity)
            or not isinstance(identity.tenant, str)
            or not identity.tenant
            or not isinstance(identity.subject, str)
            or not identity.subject
            or not isinstance(identity.roles, frozenset)
            or any(not isinstance(role, str) or not role for role in identity.roles)
            or not isinstance(query, Query)
            or not isinstance(query.view_id, str)
            or not isinstance(query.version, str)
            or not isinstance(query.columns, tuple)
            or not isinstance(query.filters, tuple)
            or any(not isinstance(column, str) for column in query.columns)
        ):
            raise QueryDenied()
        rule = self._rules.get((identity.tenant, query.view_id, query.version))
        if rule is None or (identity.subject not in rule.subjects and not identity.roles.intersection(rule.roles)):
            raise QueryDenied()
        if type(query.limit) is not int or not 1 <= query.limit <= 100 or not query.columns:
            raise QueryDenied()
        if len(set(query.columns)) != len(query.columns) or any(column not in rule.columns for column in query.columns):
            raise QueryDenied()
        names: set[str] = set()
        for item in query.filters:
            if not isinstance(item, tuple) or len(item) != 2 or not isinstance(item[0], str):
                raise QueryDenied()
            name, value = item
            if name in names or name not in rule.columns or name in rule.masked_columns:
                raise QueryDenied()
            if value is not None and type(value) not in (str, int):
                raise QueryDenied()
            if type(value) is int and not -(2**63) <= value < 2**63:
                raise QueryDenied()
            names.add(name)
        return rule


def _validate_schema(
    connection: sqlite3.Connection,
    rule: ViewRule,
    expected_definition_sha256: str | None,
    expected_source_table: str | None,
    expected_source_sha256: str | None,
) -> None:
    object_row = connection.execute("SELECT type, sql FROM sqlite_master WHERE name = ?", (rule.sqlite_view,)).fetchone()
    if object_row is None or object_row[0] != "view" or not isinstance(object_row[1], str):
        raise QueryDenied()
    if expected_definition_sha256 is not None:
        actual = hashlib.sha256(object_row[1].encode("utf-8")).hexdigest()
        if not hmac.compare_digest(actual, expected_definition_sha256):
            raise QueryDenied()
    if expected_source_table is not None and expected_source_sha256 is not None:
        source = connection.execute("SELECT type, sql FROM sqlite_master WHERE name = ?", (expected_source_table,)).fetchone()
        if source is None or source[0] != "table" or not isinstance(source[1], str) or not _plain_table_definition(source[1]):
            raise QueryDenied()
        if not hmac.compare_digest(hashlib.sha256(source[1].encode("utf-8")).hexdigest(), expected_source_sha256):
            raise QueryDenied()
        source_columns = {row[1]: row for row in connection.execute(f"PRAGMA table_xinfo({_quote(expected_source_table)})")}
        tenant_info = source_columns.get(rule.tenant_column)
        if (
            not {rule.tenant_column, *rule.columns} <= source_columns.keys()
            or tenant_info is None
            or tenant_info[2].strip().upper() != "TEXT"
            or tenant_info[6] != 0
        ):
            raise QueryDenied()
    # SQLite accepts unknown double-quoted identifiers as string literals on some builds.
    # Check the registered view schema explicitly before constructing the actual SELECT.
    view_columns = {row[1] for row in connection.execute(f"PRAGMA table_info({_quote(rule.sqlite_view)})")}
    if not {rule.tenant_column, *rule.columns} <= view_columns:
        raise QueryUnavailable()


def _verify_metadata(
    database: Path, rule: ViewRule, *, expected_definition_sha256: str, expected_source_table: str, expected_source_sha256: str
) -> None:
    """A separate current metadata snapshot, used only after the final policy check."""
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        disable_extensions = getattr(connection, "enable_load_extension", None)
        if disable_extensions is not None:
            disable_extensions(False)
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        _validate_schema(connection, rule, expected_definition_sha256, expected_source_table, expected_source_sha256)
    except (sqlite3.Error, OSError, OverflowError):
        raise QueryUnavailable() from None
    finally:
        if connection is not None:
            connection.close()


def query_view(
    database: Path,
    registry: Registry,
    identity: Identity,
    query: Query,
    *,
    expected_definition_sha256: str | None = None,
    expected_source_table: str | None = None,
    expected_source_sha256: str | None = None,
) -> Result:
    """Open a read-only database snapshot. The caller owns database and policy provenance."""
    rule = registry.resolve(identity, query)
    if expected_definition_sha256 is not None and (
        not isinstance(expected_definition_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_definition_sha256)
    ):
        raise QueryDenied()
    if (expected_source_table is None) != (expected_source_sha256 is None):
        raise QueryDenied()
    if expected_source_table is not None:
        _identifier(expected_source_table)
        if not isinstance(expected_source_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_source_sha256):
            raise QueryDenied()
    selections: list[str] = []
    parameters: list[Scalar] = []
    for column in query.columns:
        if column in rule.masked_columns:
            selections.append(f"? AS {_quote(column)}")
            parameters.append(_MASK)
        else:
            selections.append(_quote(column))
    tenant_column = _quote(rule.tenant_column)
    predicates = [f"typeof({tenant_column}) = 'text'", f"CAST({tenant_column} AS TEXT) COLLATE BINARY = ?"]
    parameters.append(identity.tenant)
    for name, value in query.filters:
        if value is None:
            predicates.append(f"{_quote(name)} IS NULL")
        else:
            predicates.append(f"{_quote(name)} = ?")
            parameters.append(value)
    parameters.append(query.limit + 1)
    # Identifiers come from the validated registry/allowlist; every request value is bound.
    sql = f"SELECT {', '.join(selections)} FROM {_quote(rule.sqlite_view)} WHERE {' AND '.join(predicates)} LIMIT ?"  # noqa: S608
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        disable_extensions = getattr(connection, "enable_load_extension", None)
        if disable_extensions is not None:
            disable_extensions(False)
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        _validate_schema(connection, rule, expected_definition_sha256, expected_source_table, expected_source_sha256)
        connection.set_authorizer(
            lambda action, _one, two, _db, _source: (
                sqlite3.SQLITE_OK
                if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
                and not (action == sqlite3.SQLITE_FUNCTION and (two or "").lower() == "load_extension")
                else sqlite3.SQLITE_DENY
            )
        )
        rows = connection.execute(sql, parameters).fetchall()
        return Result(query.columns, tuple(tuple(row) for row in rows[: query.limit]), len(rows) > query.limit, rule.version)
    except (sqlite3.Error, OSError, OverflowError):
        raise QueryUnavailable() from None
    finally:
        if connection is not None:
            connection.close()

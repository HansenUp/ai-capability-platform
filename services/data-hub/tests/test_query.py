import hashlib
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from data_hub.query import Identity, Query, QueryDenied, QueryUnavailable, Registry, ViewRule, query_view  # noqa: E402


@pytest.fixture
def sample(tmp_path):
    path = tmp_path / "synthetic.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE synthetic (tenant TEXT, item TEXT, secret TEXT, category TEXT, amount INTEGER)")
        db.executemany(
            "INSERT INTO synthetic VALUES (?, ?, ?, ?, ?)",
            [
                ("tenant-a", "a1", "a-secret-1", "open", 1),
                ("tenant-a", "a2", "a-secret-2", None, 2),
                ("tenant-a", "a3", "a-secret-3", "open", 3),
                ("tenant-b", "b1", "b-secret-1", "open", 4),
                ("TENANT-A", "wrong-case", "wrong-case-secret", "open", 5),
            ],
        )
        db.execute("CREATE VIEW report AS SELECT tenant COLLATE NOCASE AS tenant, item, secret, category, amount FROM synthetic")
        db.execute("CREATE VIEW unsafe AS SELECT tenant, load_extension('not-a-real-library') AS item FROM synthetic")
        db.execute("CREATE VIEW numeric_tenant AS SELECT 7 AS tenant, item FROM synthetic")
    rule = ViewRule(
        "tenant-a",
        "orders",
        "v1",
        "report",
        "tenant",
        ("item", "secret", "category", "amount"),
        frozenset({"secret"}),
        frozenset({"alice"}),
        frozenset({"viewer"}),
    )
    other = replace(rule, tenant="tenant-b", subjects=frozenset({"bob"}), roles=frozenset())
    return path, Registry((rule, other)), rule


def q(columns=("item", "secret"), filters=(), limit=10, version="v1", view_id="orders"):
    return Query(view_id, version, columns, filters, limit)


def test_tenant_bound_masked_projection_and_database_unchanged(sample):
    path, registry, _ = sample
    before = hashlib.sha256(path.read_bytes()).digest()
    result = query_view(path, registry, Identity("tenant-a", "alice"), q())
    assert result.columns == ("item", "secret")
    assert set(result.rows) == {("a1", "[REDACTED]"), ("a2", "[REDACTED]"), ("a3", "[REDACTED]")}
    assert result.truncated is False and result.view_version == "v1"
    assert hashlib.sha256(path.read_bytes()).digest() == before
    assert query_view(path, registry, Identity("tenant-b", "bob"), q()).rows == (("b1", "[REDACTED]"),)


def test_explicit_role_grant_and_no_admin_wildcard(sample):
    path, registry, _ = sample
    assert len(query_view(path, registry, Identity("tenant-a", "other", frozenset({"viewer"})), q()).rows) == 3
    with pytest.raises(QueryDenied):
        query_view(path, registry, Identity("tenant-a", "other", frozenset({"platform-admin"})), q())
    with pytest.raises(QueryDenied):
        query_view(path, registry, Identity("tenant-b", "alice", frozenset({"viewer"})), q())


def test_filter_parameterization_null_and_masked_column_denial(sample):
    path, registry, _ = sample
    who = Identity("tenant-a", "alice")
    assert set(query_view(path, registry, who, q(("item",), (("category", "open"),))).rows) == {("a1",), ("a3",)}
    assert query_view(path, registry, who, q(("item",), (("category", None),))).rows == (("a2",),)
    assert query_view(path, registry, who, q(("item",), (("item", "a1' OR 1=1 --"),))).rows == ()
    with pytest.raises(QueryDenied):
        query_view(path, registry, who, q(("item",), (("secret", "a-secret-1"),)))


def test_limit_reports_truncation_without_adding_other_tenant(sample):
    path, registry, _ = sample
    result = query_view(path, registry, Identity("tenant-a", "alice"), q(("item",), limit=2))
    assert len(result.rows) == 2 and result.truncated is True
    assert all(row[0].startswith("a") for row in result.rows)
    assert query_view(path, registry, Identity("tenant-b", "bob"), q(limit=2)).truncated is False


@pytest.mark.parametrize(
    "bad",
    [
        q(version="v2"),
        q(view_id="unknown"),
        q(columns=("tenant",)),
        q(columns=("item", "item")),
        q(columns=("item; DROP TABLE synthetic",)),
        q(filters=(("item", "a1"), ("item", "a2"))),
        q(filters=(("amount", True),)),
        q(filters=(("amount", 2**64),)),
        q(filters=(("unknown", 1),)),
        q(limit=0),
        q(limit=101),
        q(limit=True),
        q(columns=()),
    ],
)
def test_unapproved_scope_and_malformed_query_fail_closed(sample, bad):
    path, registry, _ = sample
    with pytest.raises(QueryDenied, match="^Restricted query denied$"):
        query_view(path, registry, Identity("tenant-a", "alice"), bad)


def test_duplicate_registry_and_bad_identifier_rejected(sample):
    _, _, rule = sample
    with pytest.raises(QueryDenied):
        Registry((rule, rule))
    with pytest.raises(QueryDenied):
        Registry((replace(rule, sqlite_view="report; DELETE FROM synthetic"),))
    with pytest.raises(QueryDenied):
        Registry((replace(rule, masked_columns=frozenset({"not_allowed"})),))
    with pytest.raises(QueryDenied):
        Registry((replace(rule, subjects=frozenset(), roles=frozenset()),))


def test_tables_and_missing_view_are_rejected(sample):
    path, _, rule = sample
    who = Identity("tenant-a", "alice")
    for name in ("synthetic", "not_present"):
        with pytest.raises(QueryDenied):
            query_view(path, Registry((replace(rule, sqlite_view=name),)), who, q())


def test_db_errors_are_controlled_and_no_database_created(tmp_path, sample):
    _, registry, _ = sample
    missing = tmp_path / "never-created.sqlite"
    with pytest.raises(QueryUnavailable, match="^Restricted query unavailable$"):
        query_view(missing, registry, Identity("tenant-a", "alice"), q())
    assert not missing.exists()


def test_schema_conflict_does_not_leak_sql(sample):
    path, _, rule = sample
    registry = Registry((replace(rule, tenant_column="absent"),))
    with pytest.raises(QueryUnavailable, match="^Restricted query unavailable$"):
        query_view(path, registry, Identity("tenant-a", "alice"), q())


def test_extension_loading_is_denied_even_inside_a_registered_view(sample):
    path, _, rule = sample
    registry = Registry((replace(rule, sqlite_view="unsafe", columns=("item",), masked_columns=frozenset()),))
    with pytest.raises(QueryUnavailable, match="^Restricted query unavailable$"):
        query_view(path, registry, Identity("tenant-a", "alice"), q(("item",)))


def test_existing_baseline_has_no_real_read_data_permit():
    from policy_service.baseline import authorize

    allowed, _, errors = authorize({"tenant": "tenant-a"}, "read_data", {"tenant": "tenant-a"}, {})
    assert allowed is False and errors == []


def test_numeric_tenant_is_not_coerced_to_the_trusted_text_identity(sample):
    path, _, rule = sample
    numeric_rule = replace(rule, tenant="7", sqlite_view="numeric_tenant", columns=("item",), masked_columns=frozenset())
    result = query_view(path, Registry((numeric_rule,)), Identity("7", "alice"), q(("item",)))
    assert result.rows == ()


def test_malformed_identity_and_query_types_fail_closed_before_connect(sample, monkeypatch):
    path, registry, _ = sample
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: pytest.fail("Rejected input must not open a database"))
    bad = [
        (Identity("tenant-a", "alice", ["viewer"]), q()),
        (Identity("tenant-a", "alice"), q(columns=("item", ["bad"]))),
        (Identity("tenant-a", "alice"), q(filters=(("item",),))),
    ]
    for identity, request in bad:
        with pytest.raises(QueryDenied, match="^Restricted query denied$"):
            query_view(path, registry, identity, request)


def test_missing_masked_source_column_is_a_configuration_error(sample):
    path, _, rule = sample
    rule = replace(rule, columns=("item", "missing"), masked_columns=frozenset({"missing"}))
    with pytest.raises(QueryUnavailable, match="^Restricted query unavailable$"):
        query_view(path, Registry((rule,)), Identity("tenant-a", "alice"), q(("item", "missing")))

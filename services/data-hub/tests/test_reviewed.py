import asyncio
import hashlib
import sqlite3
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from capability_gateway import auth

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from test_guarded import signed_idp as signed_idp  # noqa: E402
from test_query import q  # noqa: E402
from test_query import sample as sample

import data_hub.reviewed as reviewed  # noqa: E402
from data_hub.local_identity import LocalJwtIdentity  # noqa: E402
from data_hub.local_policy import LocalPolicyRule, LocalReadDataPolicy  # noqa: E402
from data_hub.query import QueryDenied, Registry, _plain_table_definition  # noqa: E402
from data_hub.reviewed import (  # noqa: E402
    AccessDecision,
    ReviewedBinding,
    ReviewedQuery,
    UserProfile,
    canonical_view_sql,
)


@pytest.fixture
def setup(sample, signed_idp):
    path, _, original = sample
    token, _ = signed_idp
    rule = replace(original, sqlite_view="strict_report")
    view_sql = canonical_view_sql(rule.sqlite_view, "synthetic", rule.tenant_column, rule.columns)
    with sqlite3.connect(path) as db:
        db.execute(view_sql)
        source_sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='synthetic'").fetchone()[0]
        stored_view = db.execute("SELECT sql FROM sqlite_master WHERE type='view' AND name='strict_report'").fetchone()[0]
    assert stored_view == view_sql
    binding = ReviewedBinding(
        "tenant-a",
        "orders",
        "v1",
        "strict_report",
        "synthetic",
        "tenant",
        hashlib.sha256(view_sql.encode()).hexdigest(),
        hashlib.sha256(source_sql.encode()).hexdigest(),
        1,
        "synthetic-maintainer",
        frozenset({"reviewer-a", "reviewer-b"}),
        "active",
    )
    profile = UserProfile("https://idp.invalid/realms/synthetic", "ai-runtime", frozenset({"portal-web"}))
    policy_rule = LocalPolicyRule(
        binding.tenant,
        binding.view_id,
        binding.version,
        binding.generation,
        binding.view_sha256,
        binding.source_sha256,
        frozenset({"alice"}),
        frozenset({"viewer"}),
        frozenset(rule.columns),
        frozenset({"item", "category", "amount"}),
        10,
    )
    state = {
        "token": token(_typ="at+jwt"),
        "binding": binding,
        "policy_rule": policy_rule,
        "policy_version": "synthetic-v1",
        "effect": "allow",
        "calls": [],
    }

    async def get_token():
        state["calls"].append("identity")
        return state["token"]

    async def verify(value, audience):
        return await auth.verify_jwt(value, audience=audience, code="invalid_user_token")

    identity = LocalJwtIdentity(profile, get_token, verify)

    async def binding_provider(identity, query):
        state["calls"].append("binding")
        return state["binding"]

    async def policy(scope):
        state["calls"].append("policy")
        if state["effect"] == "hang":
            try:
                await asyncio.Event().wait()
            finally:
                state["cancelled"] = True
        if state["effect"] == "suppress_cancellation":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                state["suppressed"] = True
            return AccessDecision(scope, "allow", state["policy_version"])
        if state["effect"] == "timeout":
            raise TimeoutError("synthetic policy timeout")
        if state["effect"] == "wrong_scope":
            return AccessDecision(replace(scope, view_id="other"), "allow", state["policy_version"])
        if state["effect"] == "deny":
            return AccessDecision(scope, "deny", state["policy_version"])
        decision = await LocalReadDataPolicy(state["policy_version"], (state["policy_rule"],))(scope)
        if state.get("revoke_after_final") and state["calls"].count("policy") == 2:
            state["effect"] = "deny"
        return decision

    def build(*, clock=time.time, gate_timeout_s=1.0):
        return ReviewedQuery(
            path, Registry((rule,)), profile, identity, binding_provider, policy, clock=clock, gate_timeout_s=gate_timeout_s
        )

    return path, rule, state, token, build


@pytest.mark.asyncio
async def test_signed_direct_user_reviewed_view_and_two_matching_checks(setup):
    _, _, state, _, build = setup
    result = await build().query(q(("item", "secret"), (("category", "open"),)))
    assert set(result.rows) == {("a1", "[REDACTED]"), ("a3", "[REDACTED]")}
    assert state["calls"] == ["identity", "binding", "policy", "identity", "binding", "policy"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"_typ": "JWT"},
        {"token_use": "id"},
        {"aud": "wrong"},
        {"azp": "service"},
        {"exp": 1},
        {"act": {"sub": "agent"}},
        {"client_identity": "service"},
        {"principal_type": "service"},
        {"principal_type": None},
        {"realm_access": {"roles": ["viewer", "viewer"]}},
        {"tenant": "other"},
        {"_kid": "unknown"},
        {"iat": 1},
    ],
)
async def test_invalid_identity_fails_before_binding_and_read(setup, monkeypatch, changes):
    _, _, state, token, build = setup
    state["token"] = token(_typ="at+jwt", **changes) if "_typ" not in changes else token(**changes)
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: pytest.fail("invalid identity must not access data"))
    with pytest.raises(QueryDenied):
        await build().query(q())
    assert "binding" not in state["calls"] and "policy" not in state["calls"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"state": "revoked"},
        {"generation": 0},
        {"generation": True},
        {"reviewers": frozenset({"only-one"})},
        {"reviewers": frozenset({"synthetic-maintainer", "reviewer-b"})},
        {"tenant": "other"},
        {"view_sha256": "f" * 64},
        {"tenant_column": "other"},
        {"reviewers": frozenset({" ", "reviewer-b"})},
    ],
)
async def test_missing_invalid_or_unreviewed_binding_fails_before_policy(setup, monkeypatch, changes):
    _, _, state, _, build = setup
    state["binding"] = replace(state["binding"], **changes)
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: pytest.fail("unreviewed binding must not access data"))
    with pytest.raises(QueryDenied):
        await build().query(q())
    assert "policy" not in state["calls"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["binding", "regrant", "policy", "revoke", "identity", "roles", "timeout"])
async def test_changes_after_read_are_denied_before_result_is_returned(setup, monkeypatch, change):
    _, _, state, token, build = setup
    original_query = reviewed.query_view
    read = []

    def query_then_change(*args, **kwargs):
        result = original_query(*args, **kwargs)
        read.append(result)
        if change == "binding":
            state["binding"] = replace(state["binding"], generation=2)
        elif change == "regrant":
            state["binding"] = replace(state["binding"], generation=3)
            state["policy_rule"] = replace(state["policy_rule"], generation=3)
        elif change == "policy":
            state["policy_version"] = "synthetic-v2"
        elif change == "revoke":
            state["effect"] = "deny"
        elif change == "identity":
            state["token"] = token(_typ="at+jwt", sub="other")
        elif change == "roles":
            state["token"] = token(_typ="at+jwt", realm_access={"roles": []})
        else:
            state["effect"] = "timeout"
        return result

    monkeypatch.setattr(reviewed, "query_view", query_then_change)
    with pytest.raises(QueryDenied):
        await build().query(q())
    assert len(read) == 1 and len(read[0].rows) == 3


@pytest.mark.asyncio
async def test_replayed_generation_cannot_prove_that_no_intermediate_revocation_occurred(setup, monkeypatch):
    _, _, state, _, build = setup
    original_query = reviewed.query_view
    evidence = []

    def change_then_replay(*args, **kwargs):
        result = original_query(*args, **kwargs)
        initial = state["binding"]
        state["binding"] = replace(initial, generation=2, state="revoked")
        evidence.append(state["binding"])
        state["binding"] = initial  # A broken provider replays generation 1 after revocation.
        return result

    monkeypatch.setattr(reviewed, "query_view", change_then_replay)
    assert len((await build().query(q())).rows) == 3
    assert evidence[0].state == "revoked" and evidence[0].generation == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("effect", ["deny", "wrong_scope", "timeout"])
async def test_policy_failure_before_read_never_opens_database(setup, monkeypatch, effect):
    _, _, state, _, build = setup
    state["effect"] = effect
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: pytest.fail("denied policy must not access data"))
    with pytest.raises(QueryDenied):
        await build().query(q())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes,query_case",
    [
        ({"subjects": frozenset({"other"}), "roles": frozenset()}, q()),
        ({"columns": frozenset({"item"}), "filter_columns": frozenset({"item"})}, q()),
        ({"filter_columns": frozenset({"item"})}, q(("item",), (("category", "open"),))),
        ({"max_limit": 5}, q()),
        ({"generation": 2}, q()),
        ({"source_sha256": "f" * 64}, q()),
    ],
)
async def test_local_dataview_policy_enforces_exact_scope_before_read(setup, monkeypatch, changes, query_case):
    _, _, state, _, build = setup
    state["policy_rule"] = replace(state["policy_rule"], **changes)
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: pytest.fail("out-of-scope policy must not access data"))
    with pytest.raises(QueryDenied):
        await build().query(query_case)


def test_duplicate_and_invalid_local_policy_configuration_rejected(setup):
    _, _, state, _, _ = setup
    rule = state["policy_rule"]
    with pytest.raises(QueryDenied):
        LocalReadDataPolicy("v1", (rule, rule))
    with pytest.raises(QueryDenied):
        LocalReadDataPolicy("v1", (replace(rule, generation=True),))


def test_binding_state_must_be_explicit_not_defaulted_to_active(setup):
    _, _, state, _, _ = setup
    fields = {name: value for name, value in vars(state["binding"]).items() if name != "state"}
    with pytest.raises(TypeError):
        ReviewedBinding(**fields)


def test_virtual_table_ddl_is_rejected_for_all_whitespace_variants():
    assert _plain_table_definition('CREATE TABLE "t" (tenant TEXT)')
    assert _plain_table_definition('create\n table "t" (tenant TEXT)')
    assert not _plain_table_definition('CREATE VIRTUAL TABLE "t" USING fts5(tenant)')
    assert not _plain_table_definition('CREATE\nVIRTUAL TABLE "t" USING fts5(tenant)')


@pytest.mark.asyncio
async def test_stalled_policy_is_cancelled_with_configured_local_gate_deadline(setup, monkeypatch):
    _, _, state, _, build = setup
    state["effect"] = "hang"
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: pytest.fail("timed-out policy must not access data"))
    with pytest.raises(QueryDenied):
        await build(gate_timeout_s=0.02).query(q())
    assert state["cancelled"] is True


@pytest.mark.asyncio
async def test_provider_suppressing_cancellation_cannot_return_late_allow(setup, monkeypatch):
    _, _, state, _, build = setup
    state["effect"] = "suppress_cancellation"
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: pytest.fail("late policy must not access data"))
    with pytest.raises(QueryDenied):
        await build(gate_timeout_s=0.02).query(q())
    assert state["suppressed"] is True


@pytest.mark.asyncio
async def test_stalled_final_policy_does_not_release_already_read_rows(setup, monkeypatch):
    _, _, state, _, build = setup
    original_query = reviewed.query_view
    read = []

    def query_then_stall(*args, **kwargs):
        result = original_query(*args, **kwargs)
        read.append(result)
        state["effect"] = "hang"
        return result

    monkeypatch.setattr(reviewed, "query_view", query_then_stall)
    with pytest.raises(QueryDenied):
        await build(gate_timeout_s=0.02).query(q())
    assert len(read) == 1 and state["cancelled"] is True


@pytest.mark.asyncio
async def test_policy_revoked_after_final_check_only_blocks_later_request(setup):
    _, _, state, _, build = setup
    state["revoke_after_final"] = True
    guard = build()
    result = await guard.query(q(("item",)))
    assert len(result.rows) == 3 and state["effect"] == "deny"
    with pytest.raises(QueryDenied):
        await guard.query(q(("item",)))


@pytest.mark.asyncio
async def test_expiry_between_checks_is_denied_after_read(setup):
    _, _, _, _, build = setup
    now = time.time()
    times = iter([now, now, now + 301])
    with pytest.raises(QueryDenied):
        await build(clock=lambda: next(times)).query(q())


@pytest.mark.asyncio
async def test_expiry_during_initial_policy_check_prevents_any_database_read(setup, monkeypatch):
    _, _, _, _, build = setup
    now = time.time()
    times = iter([now, now + 301])
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: pytest.fail("expired identity must not access data"))
    with pytest.raises(QueryDenied):
        await build(clock=lambda: next(times)).query(q())


@pytest.mark.asyncio
async def test_source_schema_change_is_rejected_even_when_view_sql_is_unchanged(setup):
    path, _, _, _, build = setup
    with sqlite3.connect(path) as db:
        before = db.execute("SELECT sql FROM sqlite_master WHERE name='strict_report'").fetchone()[0]
        db.execute("ALTER TABLE synthetic ADD COLUMN changed TEXT")
        assert db.execute("SELECT sql FROM sqlite_master WHERE name='strict_report'").fetchone()[0] == before
    with pytest.raises(QueryDenied):
        await build().query(q())


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "view"])
async def test_schema_change_after_read_is_rejected_before_return(setup, monkeypatch, change):
    path, _, _, _, build = setup
    original_query = reviewed.query_view
    read = []

    def query_then_change(*args, **kwargs):
        result = original_query(*args, **kwargs)
        read.append(result)
        with sqlite3.connect(path) as db:
            if change == "source":
                db.execute("ALTER TABLE synthetic ADD COLUMN changed_after_read TEXT")
            else:
                db.execute('DROP VIEW "strict_report"')
                db.execute('CREATE VIEW "strict_report" AS SELECT tenant, item, secret, category, amount FROM synthetic WHERE 1=0')
        return result

    monkeypatch.setattr(reviewed, "query_view", query_then_change)
    with pytest.raises(QueryDenied):
        await build().query(q())
    assert len(read) == 1 and len(read[0].rows) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "select_sql",
    [
        "SELECT 'tenant-a' AS tenant, item, secret, category, amount FROM synthetic",
        "SELECT a.tenant, a.item, a.secret, a.category, a.amount FROM synthetic a JOIN synthetic b ON a.item=b.item",
        "SELECT tenant, MIN(item) AS item, secret, category, amount FROM synthetic GROUP BY tenant",
    ],
)
async def test_literal_join_or_aggregate_is_not_a_direct_projection(setup, select_sql):
    path, _, state, _, build = setup
    with sqlite3.connect(path) as db:
        db.execute('DROP VIEW "strict_report"')
        sql = 'CREATE VIEW "strict_report" AS ' + select_sql
        db.execute(sql)
    state["binding"] = replace(state["binding"], view_sha256=hashlib.sha256(sql.encode()).hexdigest())
    with pytest.raises(QueryDenied):
        await build().query(q())
    assert "policy" not in state["calls"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant_definition", ["tenant INTEGER", "tenant TEXT GENERATED ALWAYS AS ('tenant-a') VIRTUAL"])
async def test_source_tenant_column_must_be_plain_text_not_generated(setup, tenant_definition):
    path, rule, state, _, build = setup
    with sqlite3.connect(path) as db:
        db.execute('DROP VIEW "strict_report"')
        db.execute(f"CREATE TABLE altered_source (item TEXT, secret TEXT, category TEXT, amount INTEGER, {tenant_definition})")
        sql = canonical_view_sql("strict_report", "altered_source", "tenant", rule.columns)
        db.execute(sql)
        source_sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='altered_source'").fetchone()[0]
    binding = replace(
        state["binding"],
        source_table="altered_source",
        view_sha256=hashlib.sha256(sql.encode()).hexdigest(),
        source_sha256=hashlib.sha256(source_sql.encode()).hexdigest(),
    )
    state["binding"] = binding
    state["policy_rule"] = replace(state["policy_rule"], view_sha256=binding.view_sha256, source_sha256=binding.source_sha256)
    with pytest.raises(QueryDenied):
        await build().query(q())
    assert state["calls"] == ["identity", "binding", "policy"]


@pytest.mark.asyncio
async def test_correctly_shaped_but_mislabeled_source_row_remains_outside_syntactic_proof(setup):
    path, _, _, _, build = setup
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO synthetic VALUES ('tenant-a', 'source-mislabel', 'x', 'open', 4)")
    result = await build().query(q(("item",)))
    assert ("source-mislabel",) in result.rows

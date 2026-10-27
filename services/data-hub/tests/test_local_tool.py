from dataclasses import replace

import pytest
from test_guarded import signed_idp as signed_idp
from test_query import sample as sample
from test_reviewed import setup as setup

import data_hub.reviewed as reviewed
from data_hub.local_tool import LocalDataQuery
from data_hub.query import QueryDenied


def arguments():
    return {"view_id": "orders", "version": "v1", "columns": ["item", "secret"], "filters": [], "limit": 10}


@pytest.mark.asyncio
async def test_tool_returns_guarded_masked_tenant_result(setup):
    _, _, _, _, build = setup
    result = await LocalDataQuery(build()).invoke(arguments())
    assert result["columns"] == ["item", "secret"]
    assert set(map(tuple, result["rows"])) == {("a1", "[REDACTED]"), ("a2", "[REDACTED]"), ("a3", "[REDACTED]")}
    assert result["truncated"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"sql": "select * from synthetic"},
        {"tenant": "tenant-b"},
        {"subject": "alice"},
        {"limit": True},
        {"columns": "item"},
        {"filters": [{"column": "item", "value": True}]},
        {"filters": [{"column": "secret", "value": "private"}]},
        {"view_id": "synthetic"},
    ],
)
async def test_tool_rejects_authority_or_malformed_input(setup, change):
    _, _, _, _, build = setup
    request = arguments() | change
    with pytest.raises(QueryDenied, match="^Restricted query denied$"):
        await LocalDataQuery(build()).invoke(request)


@pytest.mark.asyncio
async def test_tool_does_not_return_result_when_revoked_in_flight(setup, monkeypatch):
    _, _, state, _, build = setup
    original = reviewed.query_view

    def change_after_read(*args, **kwargs):
        result = original(*args, **kwargs)
        state["binding"] = replace(state["binding"], generation=2, state="revoked")
        return result

    monkeypatch.setattr(reviewed, "query_view", change_after_read)
    with pytest.raises(QueryDenied):
        await LocalDataQuery(build()).invoke(arguments())


@pytest.mark.asyncio
async def test_tool_uses_persisted_regrant_and_rejects_inflight_change(tmp_path, setup, monkeypatch):
    from test_versions import grant, regrant

    from data_hub.reviewed import ReviewedQuery
    from data_hub.versions import VersionedProviders, VersionLedger

    path, _, state, _, build = setup
    ledger = VersionLedger.create(tmp_path / "tool-versions.db")
    manifest, policy = grant(ledger)
    providers = VersionedProviders(
        ledger,
        lambda record: replace(state["binding"], generation=record.revision),
        lambda record, current: replace(state["policy_rule"], generation=current.revision),
    )
    old = build()
    guard = ReviewedQuery(path, old._registry, old._profile, old._identity, providers.binding, providers.policy, gate_timeout_s=1)
    original = reviewed.query_view

    def reauthorize(*args, **kwargs):
        result = original(*args, **kwargs)
        regrant(ledger, manifest, policy)
        monkeypatch.setattr(reviewed, "query_view", original)
        return result

    monkeypatch.setattr(reviewed, "query_view", reauthorize)
    with pytest.raises(QueryDenied):
        await LocalDataQuery(guard).invoke(arguments())
    assert len((await LocalDataQuery(guard).invoke(arguments()))["rows"]) == 3


@pytest.mark.asyncio
async def test_tool_honors_token_identity_switch_during_read(setup, monkeypatch):
    _, _, state, token, build = setup
    original = reviewed.query_view

    def change_identity(*args, **kwargs):
        result = original(*args, **kwargs)
        state["token"] = token(_typ="at+jwt", sub="different-user")
        return result

    monkeypatch.setattr(reviewed, "query_view", change_identity)
    with pytest.raises(QueryDenied, match="^Restricted query denied$"):
        await LocalDataQuery(build()).invoke(arguments())


@pytest.mark.asyncio
async def test_tool_propagates_limit_and_truncation(setup):
    _, _, _, _, build = setup
    result = await LocalDataQuery(build()).invoke(arguments() | {"limit": 1})
    assert len(result["rows"]) == 1
    assert result["truncated"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [b"synthetic binary", float("inf"), float("nan")])
async def test_tool_rejects_unserializable_results_without_leaking_values(setup, monkeypatch, value):
    _, _, _, _, build = setup
    original = reviewed.query_view

    def alter_result(*args, **kwargs):
        result = original(*args, **kwargs)
        return replace(result, rows=((value, "[REDACTED]"),))

    monkeypatch.setattr(reviewed, "query_view", alter_result)
    with pytest.raises(QueryDenied, match="^Restricted query denied$"):
        await LocalDataQuery(build()).invoke(arguments())


@pytest.mark.asyncio
async def test_tool_hides_internal_errors(setup, monkeypatch):
    _, _, _, _, build = setup

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic confidential internal detail")

    monkeypatch.setattr(reviewed, "query_view", fail)
    with pytest.raises(QueryDenied, match="^Restricted query denied$") as error:
        await LocalDataQuery(build()).invoke(arguments())
    assert error.value.__cause__ is None

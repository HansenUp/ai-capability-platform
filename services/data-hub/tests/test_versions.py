import shutil
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from test_guarded import signed_idp as signed_idp
from test_query import q
from test_query import sample as sample
from test_reviewed import setup as setup

import data_hub.reviewed as reviewed
from data_hub.query import QueryDenied
from data_hub.reviewed import ReviewedQuery
from data_hub.versions import VersionDenied, VersionedProviders, VersionLedger


def command():
    return str(uuid.uuid4())


def append(ledger, kind, expected, state="active", payload="synthetic", manifest=None, cmd=None):
    return ledger.append(
        kind,
        "tenant-a",
        "orders",
        expected=expected,
        state=state,
        payload=payload if state == "active" else "",
        command=cmd or command(),
        manifest=manifest,
    )


def grant(ledger):
    manifest = append(ledger, "manifest", 0)
    policy = append(ledger, "policy", 0, manifest=(manifest.revision, manifest.digest))
    return manifest, policy


def regrant(ledger, manifest, policy):
    revoked_manifest = append(ledger, "manifest", manifest.revision, "revoked")
    append(ledger, "policy", policy.revision, "revoked")
    new_manifest = append(ledger, "manifest", revoked_manifest.revision)
    new_policy = append(ledger, "policy", policy.revision + 1, manifest=(new_manifest.revision, new_manifest.digest))
    return new_manifest, new_policy


def test_persistence_revocation_regrant_and_stale_policy(tmp_path):
    ledger = VersionLedger.create(tmp_path / "versions.db")
    m, p = grant(ledger)
    assert VersionLedger(ledger.path).pair("tenant-a", "orders") == (m, p)
    append(ledger, "manifest", 1, "revoked")
    with pytest.raises(VersionDenied):
        ledger.pair("tenant-a", "orders")
    m3 = append(ledger, "manifest", 2)
    with pytest.raises(VersionDenied):
        ledger.pair("tenant-a", "orders")
    append(ledger, "policy", 1, "revoked")
    p3 = append(ledger, "policy", 2, manifest=(m3.revision, m3.digest))
    assert VersionLedger(ledger.path).pair("tenant-a", "orders") == (m3, p3)
    assert (m3.revision, p3.revision) == (3, 3)
    with pytest.raises(VersionDenied):
        ledger.pair("tenant-b", "orders")


def test_cas_command_replay_and_wrong_manifest_fail_atomically(tmp_path):
    ledger = VersionLedger.create(tmp_path / "versions.db")
    first = command()
    m = append(ledger, "manifest", 0, cmd=first)
    with pytest.raises(VersionDenied):
        append(ledger, "manifest", 1, cmd=first)
    with pytest.raises(VersionDenied):
        append(ledger, "manifest", 0)
    with pytest.raises(VersionDenied):
        append(ledger, "policy", 0, manifest=(1, "0" * 64))
    p = append(ledger, "policy", 0, manifest=(m.revision, m.digest))
    assert ledger.pair("tenant-a", "orders") == (m, p)
    with pytest.raises(FileExistsError):
        VersionLedger.create(ledger.path)
    assert ledger.pair("tenant-a", "orders") == (m, p)


def test_concurrent_claims_have_one_winner(tmp_path):
    ledger = VersionLedger.create(tmp_path / "versions.db")
    barrier = Barrier(2)
    errors = []

    def work():
        barrier.wait()
        try:
            return append(ledger, "manifest", 0)
        except VersionDenied as exc:
            errors.append(repr(exc.__context__))
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: work(), range(2)))
    assert sum(item is not None for item in outcomes) == 1, errors
    with sqlite3.connect(ledger.path) as db:
        assert db.execute("SELECT count(*) FROM events").fetchone() == (1,)


def test_uncoordinated_event_tampering_detected(tmp_path):
    ledger = VersionLedger.create(tmp_path / "versions.db")
    grant(ledger)
    with sqlite3.connect(ledger.path) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE events SET payload='changed'")
        db.execute("DROP TRIGGER events_no_update")
        db.execute("UPDATE events SET payload='changed' WHERE kind='manifest'")
    with pytest.raises(VersionDenied):
        ledger.pair("tenant-a", "orders")


def test_whole_database_rollback_is_not_detectable_without_external_anchor(tmp_path):
    ledger = VersionLedger.create(tmp_path / "versions.db")
    m, p = grant(ledger)
    snapshot = tmp_path / "snapshot.db"
    shutil.copyfile(ledger.path, snapshot)
    regrant(ledger, m, p)
    assert ledger.pair("tenant-a", "orders")[0].revision == 3
    shutil.copyfile(snapshot, ledger.path)
    assert ledger.pair("tenant-a", "orders") == (m, p)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["manifest", "policy"])
async def test_inflight_regrant_rejected_then_new_query_allowed(tmp_path, setup, monkeypatch, change):
    path, _, state, _, build = setup
    ledger = VersionLedger.create(tmp_path / "versions.db")
    m, p = grant(ledger)
    provider = VersionedProviders(
        ledger,
        lambda record: replace(state["binding"], generation=record.revision),
        lambda record, manifest: replace(state["policy_rule"], generation=manifest.revision),
    )
    old = build()
    guard = ReviewedQuery(path, old._registry, old._profile, old._identity, provider.binding, provider.policy, gate_timeout_s=1)
    original = reviewed.query_view
    calls = []

    def read_then_change(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result)
        if change == "manifest":
            regrant(ledger, m, p)
        else:
            append(ledger, "policy", 1, "revoked")
            append(ledger, "policy", 2, manifest=(m.revision, m.digest))
        monkeypatch.setattr(reviewed, "query_view", original)
        return result

    monkeypatch.setattr(reviewed, "query_view", read_then_change)
    with pytest.raises(QueryDenied):
        await guard.query(q())
    assert len(calls) == 1
    assert len((await guard.query(q())).rows) == 3


def test_independent_authority_substitute_rejects_stale_or_unavailable_state(tmp_path):
    ledger = VersionLedger.create(tmp_path / "versions.db")
    old_manifest, old_policy = grant(ledger)
    backup = tmp_path / "snapshot.db"
    shutil.copyfile(ledger.path, backup)
    latest_manifest, latest_policy = regrant(ledger, old_manifest, old_policy)
    authority = {
        "heads": ((latest_manifest.revision, latest_manifest.digest), (latest_policy.revision, latest_policy.digest)),
        "available": True,
    }

    def checked_pair():
        if not authority["available"]:
            raise VersionDenied()
        manifest, policy = ledger.pair("tenant-a", "orders")
        observed = ((manifest.revision, manifest.digest), (policy.revision, policy.digest))
        if observed != authority["heads"]:
            raise VersionDenied()
        return manifest, policy

    assert checked_pair() == (latest_manifest, latest_policy)
    shutil.copyfile(backup, ledger.path)
    assert ledger.pair("tenant-a", "orders") == (old_manifest, old_policy)
    with pytest.raises(VersionDenied):
        checked_pair()
    authority["available"] = False
    with pytest.raises(VersionDenied):
        checked_pair()


def test_concurrent_separate_ledger_instances_have_one_winner(tmp_path):
    path = tmp_path / "versions.db"
    first = VersionLedger.create(path)
    second = VersionLedger(path)
    barrier = Barrier(2)

    def work(ledger):
        barrier.wait()
        try:
            return append(ledger, "manifest", 0)
        except VersionDenied:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(work, (first, second)))
    assert sum(item is not None for item in outcomes) == 1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM events").fetchone() == (1,)

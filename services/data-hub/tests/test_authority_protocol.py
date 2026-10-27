"""Pure authority model: protocol tests, not a production monotonic anchor."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Barrier, Lock

import pytest


class AuthorityDenied(Exception):
    pass


class ResultUnknown(Exception):
    pass


@dataclass(frozen=True)
class Snapshot:
    revision: int
    manifest: str
    policy: str
    active: bool


class FakeAuthority:
    """One isolated authoritative transaction; no claims about real consensus."""

    def __init__(self):
        self.lock = Lock()
        self.current = Snapshot(0, "", "", False)
        self.commands = {}
        self.available = True

    def read(self):
        with self.lock:
            if not self.available:
                raise AuthorityDenied()
            return self.current

    def publish(self, expected, command, manifest, policy, active, fault=None):
        with self.lock:
            if not self.available or expected != self.current.revision or command in self.commands:
                raise AuthorityDenied()
            if not command or not manifest or not policy or type(active) is not bool:
                raise AuthorityDenied()
            if fault == "before":
                raise ResultUnknown()
            result = Snapshot(expected + 1, manifest, policy, active)
            self.current = result
            self.commands[command] = result
            if fault == "after":
                raise ResultUnknown()
            return result

    def receipt(self, command):
        with self.lock:
            if not self.available:
                raise AuthorityDenied()
            return self.commands.get(command)


def checked_snapshot(authority, cached):
    """Cache is never authority; failure or disagreement must deny."""
    actual = authority.read()
    if not actual.active or cached != actual:
        raise AuthorityDenied()
    return actual


def test_committed_response_loss_recovers_by_read_not_reexecution():
    authority = FakeAuthority()
    with pytest.raises(ResultUnknown):
        authority.publish(0, "cmd-1", "m1", "p1", True, fault="after")
    committed = authority.receipt("cmd-1")
    assert committed == Snapshot(1, "m1", "p1", True)
    with pytest.raises(AuthorityDenied):
        authority.publish(1, "cmd-1", "m1", "p1", True)
    assert authority.read() == committed


def test_before_commit_has_no_partial_authority_state():
    authority = FakeAuthority()
    with pytest.raises(ResultUnknown):
        authority.publish(0, "cmd-1", "m1", "p1", True, fault="before")
    assert authority.read() == Snapshot(0, "", "", False)
    assert authority.receipt("cmd-1") is None
    with pytest.raises(AuthorityDenied):
        checked_snapshot(authority, Snapshot(1, "m1", "p1", True))


def test_restore_old_cache_and_missing_authority_fail_closed():
    authority = FakeAuthority()
    old = authority.publish(0, "one", "m1", "p1", True)
    authority.publish(1, "revoke", "m1", "p1", False)
    current = authority.publish(2, "grant", "m1", "p1", True)
    assert current.revision == 3 and checked_snapshot(authority, current) == current
    with pytest.raises(AuthorityDenied):
        checked_snapshot(authority, old)
    authority.available = False
    with pytest.raises(AuthorityDenied):
        checked_snapshot(authority, current)


def test_competing_expected_version_has_one_committed_winner():
    authority = FakeAuthority()
    barrier = Barrier(2)

    def publish(command):
        barrier.wait()
        try:
            return authority.publish(0, command, "m1", "p1", True)
        except AuthorityDenied:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(publish, ("one", "two")))
    assert sum(outcome is not None for outcome in outcomes) == 1
    assert authority.read().revision == 1
    assert len(authority.commands) == 1


def test_authority_rollback_itself_has_no_independent_proof():
    authority = FakeAuthority()
    old = authority.publish(0, "one", "m1", "p1", True)
    authority.publish(1, "two", "m2", "p2", True)
    authority.current = old  # A compromised or restored authority defeats its own check.
    assert checked_snapshot(authority, old) == old


def test_t1_t2_discards_result_after_revoke_and_same_content_regrant():
    authority = FakeAuthority()
    original = authority.publish(0, "one", "m1", "p1", True)
    initial = checked_snapshot(authority, original)
    synthetic_result = ("masked-row",)
    authority.publish(1, "revoke", "m1", "p1", False)
    latest = authority.publish(2, "regrant", "m1", "p1", True)
    with pytest.raises(AuthorityDenied):
        checked_snapshot(authority, initial)
    assert latest.revision != initial.revision and synthetic_result == ("masked-row",)
    assert checked_snapshot(authority, latest) == latest


def test_recovery_receipt_must_match_command_scope_and_result_exactly():
    receipt = {"command": "c1", "tenant": "tenant-a", "view": "orders", "revision": 4, "digest": "known"}

    def confirm(expected, observed):
        if set(observed) != set(expected) or observed != expected:
            raise AuthorityDenied()
        return observed

    assert confirm(receipt, dict(receipt)) == receipt
    for field, value in (("command", "c2"), ("tenant", "tenant-b"), ("view", "other"), ("revision", 3), ("digest", "other")):
        with pytest.raises(AuthorityDenied):
            confirm(receipt, receipt | {field: value})
    with pytest.raises(AuthorityDenied):
        confirm(receipt, {key: value for key, value in receipt.items() if key != "digest"})


def test_stale_response_from_authority_cannot_be_detected_without_freshness_proof():
    authority = FakeAuthority()
    old = authority.publish(0, "one", "m1", "p1", True)
    authority.publish(1, "two", "m2", "p2", True)

    # A replaying or stale responder is outside the trusted linearizable authority assumption.
    class StaleAuthority:
        def read(self):
            return old

    assert checked_snapshot(StaleAuthority(), old) == old

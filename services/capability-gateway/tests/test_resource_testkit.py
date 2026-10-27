from __future__ import annotations

import pytest
from resource_testkit import ResourceLedger, preserve_errors


def test_names_are_independent_and_foreign_or_duplicate_registration_is_refused():
    first, second = ResourceLedger("es-test"), ResourceLedger("es-test")
    assert first.namespace != second.namespace
    with pytest.raises(ValueError):
        first.created(second.namespace + "-index", "uuid")
    name = first.namespace + "-index"
    first.created(name, "uuid")
    with pytest.raises(ValueError):
        first.created(name, "uuid")


@pytest.mark.parametrize("suffix", ["*", "index,foreign", "index/path", "index?x=y"])
def test_cleanup_targets_cannot_contain_wildcards_or_request_syntax(suffix):
    ledger = ResourceLedger("es-test")
    with pytest.raises(ValueError):
        ledger.created(ledger.namespace + "-" + suffix, "uuid")
    assert ledger.owned == {}


def test_cleanup_is_exact_and_preserves_foreign_and_replaced_resources():
    ledger = ResourceLedger("minio-test")
    owned, replaced = ledger.namespace + "-owned", ledger.namespace + "-replaced"
    objects = {owned: "one", replaced: "replacement", "foreign": "other"}
    ledger.created(owned, "one")
    ledger.created(replaced, "original")
    with pytest.raises(ExceptionGroup, match="unresolved"):
        ledger.cleanup(objects.get, objects.pop)
    assert objects == {replaced: "replacement", "foreign": "other"}
    assert ledger.owned == {replaced: "original"}


def test_failed_cleanup_keeps_ownership_and_attempts_other_exact_resources():
    ledger = ResourceLedger("es-test")
    failed, successful = ledger.namespace + "-failed", ledger.namespace + "-successful"
    ledger.created(failed, "one")
    ledger.created(successful, "two")
    calls = []

    def delete(name):
        calls.append(name)
        if name == failed:
            raise OSError("cleanup failure")

    with pytest.raises(ExceptionGroup):
        ledger.cleanup(lambda name: ledger.owned[name], delete)
    assert calls == [failed, successful]
    assert ledger.owned == {failed: "one"}


def test_primary_and_cleanup_errors_are_both_preserved():
    primary = AssertionError("original assertion")
    cleanup_error = OSError("cleanup failure")

    def cleanup():
        raise cleanup_error

    with pytest.raises(BaseExceptionGroup) as result:
        preserve_errors(primary, cleanup)
    assert result.value.exceptions == (primary, cleanup_error)
    with pytest.raises(AssertionError) as result:
        preserve_errors(primary, lambda: None)
    assert result.value is primary


def test_unconfirmed_creation_is_never_deleted_and_missing_owned_resource_is_retired():
    ledger = ResourceLedger("minio-test")
    name = ledger.namespace + "-confirmed"
    ledger.created(name, "identity")
    deleted = []
    ledger.cleanup(lambda name: None, deleted.append)
    assert deleted == []
    assert ledger.owned == {}

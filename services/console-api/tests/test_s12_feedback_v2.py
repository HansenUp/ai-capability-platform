import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from console_api.feedback_v2 import summarize_v2
from console_api.trace_metrics import SourceUnavailable

DONE = datetime(2026, 10, 22, tzinfo=UTC)


def sample(revision=1, value=1, observed=True):
    score = {
        "id": f"score-{revision}",
        "name": "user_feedback",
        "dataType": "NUMERIC",
        "traceId": "trace",
        "timestamp": DONE.isoformat(),
        "value": value,
        "metadata": {"run_id": "run", "score_revision": revision},
    }
    digest = hashlib.sha256(json.dumps(score, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    proof = {
        "score_id": score["id"],
        "run_id": "run",
        "score_revision": revision,
        "observed": observed,
        "payload_hash": digest,
        "trace_id": "trace",
    }
    return proof, score


def test_revision_order_not_updated_time_or_array_order():
    first, old = sample()
    second, new = sample(2, -1)
    old["updatedAt"] = "2099-01-01T00:00:00Z"
    result = summarize_v2({"run": DONE}, [second, first], {new["id"]: new, old["id"]: old})
    assert result["satisfaction"] == 0 and result["negative_runs"] == 1 and result["feedback_coverage"] == 1


@pytest.mark.parametrize("mode", ["pending", "missing", "neutral", "tampered", "legacy"])
def test_latest_never_falls_back_and_legacy_never_upgrades(mode):
    first, old = sample()
    second, new = sample(2, 0 if mode == "neutral" else -1, mode != "pending")
    scores = {old["id"]: old, new["id"]: new}
    if mode == "missing":
        del scores[new["id"]]
    if mode == "tampered":
        new["value"] = 1
    if mode == "legacy":
        legacy = deepcopy(old) | {"id": "legacy", "metadata": {"run_id": "run", "verified": True}}
        scores["legacy"] = legacy
    result = summarize_v2({"run": DONE}, [first, second], scores)
    assert result["positive_runs"] == 0 and result["satisfaction"] is None
    assert result["feedback_coverage"] == (0 if mode == "neutral" else None)
    if mode == "legacy":
        assert result["unverified_runs"] == 1 and result["negative_runs"] == 1


@pytest.mark.parametrize("value", [True, 0.5, "1", 2])
def test_non_protocol_values_rejected_even_with_matching_hash(value):
    proof, score = sample(value=value)
    result = summarize_v2({"run": DONE}, [proof], {score["id"]: score})
    assert result["conflicting_runs"] == 1 and result["feedback_coverage"] is None


def test_no_sample_no_feedback_and_revision_integrity():
    assert summarize_v2({}, [], {})["feedback_coverage"] is None
    assert summarize_v2({"run": DONE}, [], {})["feedback_coverage"] == 0
    proof, score = sample(2)
    with pytest.raises(SourceUnavailable):
        summarize_v2({"run": DONE}, [proof], {score["id"]: score})
    proof, score = sample()
    with pytest.raises(SourceUnavailable):
        summarize_v2({"run": DONE}, [proof, proof], {score["id"]: score})


@pytest.mark.parametrize("field", ["traceId", "metadata", "timestamp", "comment"])
def test_invalid_historical_revision_is_not_hidden_by_valid_latest(field):
    first, old = sample()
    second, latest = sample(2, -1)
    old[field] = {
        "traceId": "foreign-trace",
        "metadata": {"run_id": "foreign-run", "score_revision": 1},
        "timestamp": "2026-10-21T23:59:59+00:00",
        "comment": "unexpected synthetic comment",
    }[field]
    first["payload_hash"] = hashlib.sha256(
        json.dumps(
            {key: old[key] for key in ("id", "name", "dataType", "traceId", "timestamp", "value", "metadata")},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    result = summarize_v2({"run": DONE}, [first, second], {old["id"]: old, latest["id"]: latest})
    assert result["conflicting_runs"] == 1
    assert result["positive_runs"] == result["negative_runs"] == result["verified_feedback_runs"] == 0
    assert result["satisfaction"] is None and result["feedback_coverage"] is None

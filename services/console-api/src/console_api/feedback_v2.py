"""权威修订聚合；不接受调用者提供的可信开关。"""

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from typing import Any

from console_api.trace_metrics import SourceUnavailable, timestamp

PAYLOAD_KEYS = ("id", "name", "dataType", "traceId", "timestamp", "value", "metadata")


def summarize_v2(completed: dict[str, datetime], proofs: Any, scores: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(proofs, list) or len(proofs) > 2000:
        raise SourceUnavailable
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    ids: set[str] = set()
    for proof in proofs:
        if (
            not isinstance(proof, dict)
            or proof.get("run_id") not in completed
            or not isinstance(proof.get("score_id"), str)
            or not proof["score_id"]
            or proof["score_id"] in ids
            or type(proof.get("score_revision")) is not int
            or proof["score_revision"] < 1
            or type(proof.get("observed")) is not bool
            or not isinstance(proof.get("payload_hash"), str)
            or len(proof["payload_hash"]) != 64
            or not isinstance(proof.get("trace_id"), str)
            or not proof["trace_id"]
        ):
            raise SourceUnavailable
        ids.add(proof["score_id"])
        grouped[proof["run_id"]].append(proof)
    unverified = {row["metadata"]["run_id"] for key, row in scores.items() if key not in ids}
    counts = dict.fromkeys(("positive_runs", "negative_runs", "neutral_runs", "pending_runs", "conflicting_runs"), 0)
    for run_id, revisions in grouped.items():
        revisions.sort(key=lambda row: row["score_revision"])
        if [row["score_revision"] for row in revisions] != list(range(1, len(revisions) + 1)):
            raise SourceUnavailable
        conflicting = False
        for proof in revisions:
            source = scores.get(proof["score_id"])
            if source is None:
                continue
            digest = hashlib.sha256(
                json.dumps({key: source.get(key) for key in PAYLOAD_KEYS}, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            ).hexdigest()
            if (
                digest != proof["payload_hash"]
                or source.get("traceId") != proof["trace_id"]
                or source.get("metadata", {}).get("run_id") != run_id
                or source.get("comment")
                or timestamp(source.get("timestamp")) < completed[run_id]
            ):
                conflicting = True
        if conflicting:
            counts["conflicting_runs"] += 1
            continue
        latest = revisions[-1]
        if not latest["observed"] or latest["score_id"] not in scores:
            counts["pending_runs"] += 1
            continue
        value = scores[latest["score_id"]].get("value")
        if type(value) is not int or value not in (-1, 0, 1) or scores[latest["score_id"]].get("dataType") != "NUMERIC":
            counts["conflicting_runs"] += 1
            continue
        counts["positive_runs" if value == 1 else "negative_runs" if value == -1 else "neutral_runs"] += 1
    binary = counts["positive_runs"] + counts["negative_runs"]
    incomplete = bool(unverified or counts["pending_runs"] or counts["conflicting_runs"])
    return {
        **counts,
        "unverified_runs": len(unverified),
        "completed_runs": len(completed),
        "verified_feedback_runs": binary,
        "satisfaction": counts["positive_runs"] / binary if binary and not incomplete else None,
        "feedback_coverage": binary / len(completed) if completed and not incomplete else None,
        "reason": "INCOMPLETE_PROVENANCE" if incomplete else "NO_SAMPLE" if not completed else "NO_BINARY_FEEDBACK" if not binary else None,
    }

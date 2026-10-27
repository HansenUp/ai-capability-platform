"""反馈计算契约；可信输入只供内部计算及隔离测试，不提供 HTTP 信任标记。"""

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Rating:
    score_id: str
    run_id: str
    revised_at: datetime | None
    value: Any
    data_type: str
    trusted: bool


def summarize(completed: dict[str, datetime], ratings: list[Rating]) -> dict[str, Any]:
    grouped: dict[str, list[Rating]] = defaultdict(list)
    seen: dict[str, Rating] = {}
    conflicts: set[str] = set()
    for row in ratings:
        if row.run_id not in completed:
            continue
        previous = seen.get(row.score_id)
        if previous is not None:
            if previous != row:
                conflicts.update((previous.run_id, row.run_id))
            continue
        seen[row.score_id] = row
        grouped[row.run_id].append(row)
    counts = {
        key: 0
        for key in (
            "positive_runs",
            "negative_runs",
            "unverified_runs",
            "non_binary_runs",
            "invalid_score_runs",
            "conflicting_runs",
            "pre_terminal_runs",
        )
    }
    for run_id in set(grouped) | conflicts:
        rows = grouped[run_id]
        if any(not row.trusted for row in rows):
            counts["unverified_runs"] += 1
        if run_id in conflicts or any(row.revised_at is None or row.revised_at.tzinfo is None for row in rows):
            counts["conflicting_runs"] += 1
            continue
        eligible = [row for row in rows if row.revised_at is not None and row.revised_at >= completed[run_id]]
        if len(eligible) != len(rows):
            counts["pre_terminal_runs"] += 1
        if not eligible:
            continue
        last_time = max(row.revised_at for row in eligible if row.revised_at is not None)
        latest = [row for row in eligible if row.revised_at == last_time]
        if len(latest) != 1:
            counts["conflicting_runs"] += 1
            continue
        row = latest[0]
        if row.data_type != "NUMERIC" or isinstance(row.value, bool) or not isinstance(row.value, (int, float)):
            counts["invalid_score_runs"] += 1
        elif not -1 <= row.value <= 1 or not math.isfinite(row.value):
            counts["invalid_score_runs"] += 1
        elif row.value not in (-1, 1):
            counts["non_binary_runs"] += 1
        elif row.trusted and all(item.trusted for item in rows):
            counts["positive_runs" if row.value == 1 else "negative_runs"] += 1
    binary = counts["positive_runs"] + counts["negative_runs"]
    incomplete = bool(counts["unverified_runs"] or counts["conflicting_runs"] or counts["invalid_score_runs"])
    return {
        **counts,
        "completed_runs": len(completed),
        "verified_feedback_runs": binary,
        "satisfaction": counts["positive_runs"] / binary if binary and not incomplete else None,
        "feedback_coverage": binary / len(completed) if completed and not incomplete else None,
        "reason": "UNVERIFIABLE_OR_CONFLICTING"
        if incomplete
        else "NO_SAMPLE"
        if not completed
        else "NO_BINARY_FEEDBACK"
        if not binary
        else None,
    }

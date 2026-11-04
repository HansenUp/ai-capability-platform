from copy import deepcopy

import pytest

from data_hub.order_count import arguments, collect_counts
from data_hub.order_metrics import FIELDS

WINDOW = {"since": "2026-10-01T00:00:00Z", "until": "2026-10-02T00:00:00Z", "status": "all"}
ROWS = [
    ["A", WINDOW["since"], "completed", 1200, "CNY", "invalid-update"],
    ["B", "2026-10-01T08:00:00Z", "pending", None, "USD", "invalid-update"],
    ["C", WINDOW["until"], "cancelled", 1, "CNY", "invalid-update"],
]


async def count(rows, args=None, truncated=False):
    async def reader():
        return {"columns": list(FIELDS), "rows": rows, "truncated": truncated, "view_version": "v1"}

    return await collect_counts(reader, args or WINDOW, lambda: 1)


@pytest.mark.asyncio
async def test_exact_cohort_status_dedup_and_minimal_output():
    result = await count(ROWS + [ROWS[0]])
    assert result["order_count"] == 2
    assert result["statuses"] == {"pending": 1, "completed": 1, "cancelled": 0}
    filtered = await count(ROWS, WINDOW | {"status": "completed"})
    assert filtered["order_count"] == 1 and filtered["statuses"]["pending"] == 0
    assert set(filtered) == {
        "profile",
        "since",
        "until",
        "status_filter",
        "view_version",
        "snapshot_guaranteed",
        "read_started",
        "read_finished",
        "complete",
        "reason",
        "order_count",
        "statuses",
    }
    assert not any(key in str(filtered) for key in ["amount_minor", "currency", "order_id", "invalid-update"])
    assert (await count([], WINDOW | {"status": "cancelled"}))["order_count"] == 0
    assert (await count(ROWS, WINDOW | {"status": "cancelled"}))["order_count"] == 0


@pytest.mark.parametrize(
    "change",
    [
        {"since": "2026-02-30T00:00:00Z"},
        {"since": "2026-10-01"},
        {"until": WINDOW["since"]},
        {"until": "2026-11-02T00:00:00Z"},
        {"since": "2026-10-01T24:00:00Z"},
        {"status": "paid"},
        {"tenant": "foreign"},
        {"limit": 10},
        {"since": "2026-10-01T00:00:00+14:30"},
        {"since": "2026-10-01T00:00:00+00:60"},
    ],
)
def test_invalid_scope_rejected(change):
    with pytest.raises(ValueError):
        arguments(WINDOW | change)


@pytest.mark.asyncio
async def test_timezone_instant_and_31_day_boundary():
    result = await count(ROWS, WINDOW | {"since": "2026-10-01T08:00:00+08:00"})
    assert result["order_count"] == 2
    arguments(WINDOW | {"until": "2026-11-01T00:00:00Z"})


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["conflict", "date", "status", "truncated", "over-limit"])
async def test_incomplete_is_unknown_not_zero(failure):
    rows = deepcopy(ROWS)
    if failure == "conflict":
        row = rows[0].copy()
        row[2] = "pending"
        rows.append(row)
    elif failure == "date":
        rows[2][1] = "invalid"  # Even invalid out-of-window evidence cannot be ignored.
    elif failure == "status":
        rows[2][2] = "unknown"
    elif failure == "over-limit":
        rows = [rows[0]] * 101
    result = await count(rows, truncated=failure == "truncated")
    assert result["complete"] is False and result["order_count"] is None and result["statuses"] is None


@pytest.mark.asyncio
async def test_revocation_or_source_change_discards_aggregate():
    state = {"calls": 0, "revision": 1}

    async def revoked():
        state["calls"] += 1
        if state["calls"] == 2:
            raise ValueError("revoked")
        return {"columns": list(FIELDS), "rows": ROWS, "truncated": False, "view_version": "v1"}

    with pytest.raises(ValueError, match="revoked"):
        await collect_counts(revoked, WINDOW, lambda: state["revision"])
    assert state["calls"] == 2
    state["calls"] = 0

    async def changed():
        state["calls"] += 1
        state["revision"] = state["calls"]
        return {"columns": list(FIELDS), "rows": ROWS, "truncated": False, "view_version": "v1"}

    with pytest.raises(ValueError, match="changed"):
        await collect_counts(changed, WINDOW, lambda: state["revision"])


@pytest.mark.asyncio
async def test_complete_source_limit_before_window_filter():
    rows = []
    for n in range(100):
        row = ROWS[0].copy()
        row[0] = f"order-{n}"
        rows.append(row)
    assert (await count(rows))["order_count"] == 100
    row = ROWS[2].copy()
    row[0] = "outside-101"
    # Even an extra row outside the selected cohort makes the source incomplete.
    result = await count(rows + [row])
    assert result["order_count"] is None and not result["complete"]

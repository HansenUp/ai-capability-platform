from copy import deepcopy

import pytest

from data_hub.order_groups import arguments, collect_groups, grouped
from data_hub.order_metrics import FIELDS

ARGS = {"since": "2026-10-01T00:00:00Z", "until": "2026-10-02T00:00:00Z", "status": "all", "metric": "status_share", "group_by": "status"}
ROWS = [
    ["A", ARGS["since"], "completed", None, "invalid", "invalid"],
    ["B", "2026-10-01T08:00:00Z", "pending", 7, "CNY", "invalid"],
    ["C", "2026-10-01T09:00:00Z", "cancelled", 8, "USD", "invalid"],
    ["D", ARGS["until"], "completed", 999, "CNY", "invalid"],
]


def source(rows, truncated=False):
    return {"columns": list(FIELDS), "rows": rows, "truncated": truncated, "view_version": "v1"}


def test_full_window_dedup_statuses_exact_denominator_and_no_money():
    result = grouped(source(ROWS + [ROWS[0]]), ARGS)
    assert result["complete"] and result["total_order_count"] == 3
    assert [g["key"]["status"] for g in result["groups"]] == ["pending", "completed", "cancelled"]
    assert all(g["order_count"] == g["share_numerator"] == 1 and g["share_denominator"] == 3 for g in result["groups"])
    assert sum(g["share_numerator"] for g in result["groups"]) == result["total_order_count"]
    assert not any(name in str(result) for name in ["order_id", "currency", "amount_minor", "total_minor"])
    same = grouped(source(ROWS), ARGS | {"since": "2026-10-01T08:00:00+08:00"})
    assert same["groups"] == result["groups"]


def test_zero_category_empty_window_and_single_status():
    result = grouped(source(ROWS[:2]), ARGS)
    absent = result["groups"][2]
    assert absent["order_count"] == absent["share_numerator"] == 0 and absent["share_denominator"] == 2
    empty = grouped(source([]), ARGS)
    assert empty["total_order_count"] == 0 and empty["complete"] and len(empty["groups"]) == 3
    assert all(g["order_count"] == 0 and g["share_numerator"] is None and g["share_denominator"] is None for g in empty["groups"])
    one = grouped(source(ROWS[:1]), ARGS)
    assert one["groups"][1]["share_numerator"] == one["groups"][1]["share_denominator"] == 1


@pytest.mark.parametrize(
    "change",
    [
        {"status": "completed"},
        {"status": "pending"},
        {"status": "cancelled"},
        {"group_by": "currency"},
        {"group_by": "status_currency"},
        {"denominator": 5},
        {"tenant": "other"},
    ],
)
def test_cannot_narrow_or_override_denominator(change):
    with pytest.raises(ValueError):
        arguments(ARGS | change)


@pytest.mark.parametrize("failure", ["truncated", "conflict", "date", "status", "over_limit"])
def test_incomplete_source_never_provides_shares(failure):
    rows = deepcopy(ROWS)
    if failure == "conflict":
        row = rows[0].copy()
        row[2] = "pending"
        rows.append(row)
    elif failure == "date":
        rows[-1][1] = "invalid"
    elif failure == "status":
        rows[-1][2] = "unknown"
    elif failure == "over_limit":
        rows = [rows[0]] * 101
    result = grouped(source(rows, failure == "truncated"), ARGS)
    assert result["complete"] is False and result["groups"] == [] and result["total_order_count"] is None


def test_complete_100_source_rows():
    rows = []
    for n in range(100):
        row = ROWS[0].copy()
        row[0] = str(n)
        rows.append(row)
    result = grouped(source(rows), ARGS)
    assert result["total_order_count"] == 100 and result["groups"][1]["share_denominator"] == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "revision", "revoked"])
async def test_rechecks_drop_precomputed_shares(change):
    state = {"calls": 0, "revision": 1}

    async def reader():
        state["calls"] += 1
        if state["calls"] == 2:
            if change == "revoked":
                raise ValueError("revoked")
            if change == "revision":
                state["revision"] = 2
            if change == "source":
                return source([])
        return source(ROWS)

    with pytest.raises(ValueError):
        await collect_groups(reader, ARGS, lambda: state["revision"])
    assert state["calls"] == 2

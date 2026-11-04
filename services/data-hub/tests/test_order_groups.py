from copy import deepcopy

import pytest

from data_hub.order_groups import arguments, collect_groups, grouped
from data_hub.order_metrics import FIELDS

ARGS = {
    "since": "2026-10-01T00:00:00Z",
    "until": "2026-10-02T00:00:00Z",
    "status": "completed",
    "metric": "completed_amount",
    "group_by": "currency",
}
ROWS = [
    ["A", ARGS["since"], "completed", 12000, "CNY", "invalid"],
    ["B", ARGS["since"], "completed", 500, "USD", "invalid"],
    ["C", ARGS["since"], "pending", 999, "CNY", "invalid"],
    ["D", ARGS["until"], "completed", 99999, "CNY", "invalid"],
]


def source(rows, truncated=False):
    return {"columns": list(FIELDS), "rows": rows, "truncated": truncated, "view_version": "v1"}


def test_currency_separation_duplicate_window_and_no_details():
    result = grouped(source(ROWS + [ROWS[0]]), ARGS)
    assert result["complete"]
    assert [g["key"] for g in result["groups"]] == [{"currency": "CNY"}, {"currency": "USD"}]
    assert [g["total_minor"] for g in result["groups"]] == ["12000", "500"]
    assert all(g["order_count"] == 1 and g["amount_complete"] for g in result["groups"])
    assert not any(k in str(result) for k in ["order_id", "updated_at", "amount_minor", "99999"])
    assert "total_minor" not in result  # No cross-currency grand total.


@pytest.mark.parametrize("amount", [None, True, -1, 1.5, "100"])
def test_missing_amount_is_unknown_with_known_subtotal(amount):
    row = ROWS[0].copy()
    row[0] = "MISSING"
    row[3] = amount
    group = grouped(source([ROWS[0], row]), ARGS)["groups"][0]
    assert group["total_minor"] is None and group["known_minor"] == "12000"
    assert group["valid_amount_count"] == 1 and group["order_count"] == 2
    assert group["amount_coverage"] == 0.5 and group["amount_complete"] is False
    unknown = grouped(source([row]), ARGS)["groups"][0]
    assert unknown["known_minor"] is None and unknown["total_minor"] is None


def test_exact_integer_zero_and_empty():
    row = ROWS[0].copy()
    row[3] = 10**20 + 1
    assert grouped(source([row]), ARGS)["groups"][0]["total_minor"] == str(10**20 + 1)
    row[3] = 0
    g = grouped(source([row]), ARGS)["groups"][0]
    assert g["total_minor"] == "0" and g["amount_complete"]
    assert grouped(source([]), ARGS)["groups"] == []


@pytest.mark.parametrize("dimension,expected", [("status", 2), ("currency", 2), ("status_currency", 3)])
def test_count_grouping(dimension, expected):
    result = grouped(source(ROWS), ARGS | {"metric": "order_count", "status": "all", "group_by": dimension})
    assert len(result["groups"]) == expected
    assert sum(g["order_count"] for g in result["groups"]) == 3
    assert all(set(g) == {"key", "order_count"} for g in result["groups"])


def test_currency_invalid_only_blocks_dependent_selected_grouping():
    row = ROWS[0].copy()
    row[4] = "EUR"
    assert grouped(source([row]), ARGS)["complete"] is False
    assert grouped(source([row]), ARGS | {"metric": "order_count", "group_by": "status"})["complete"]
    row[2] = "pending"
    assert grouped(source([row]), ARGS)["complete"]  # Excluded state does not create a currency group.
    assert grouped(source([row]), ARGS)["groups"] == []


@pytest.mark.parametrize(
    "change",
    [
        {"metric": "revenue"},
        {"group_by": "customer"},
        {"metric": "completed_amount", "group_by": "status"},
        {"metric": "completed_amount", "status": "all"},
        {"tenant": "foreign"},
        {"sql": "SELECT *"},
        {"since": "2026-10-01"},
        {"until": "2026-11-02T00:00:00Z"},
    ],
)
def test_unsupported_scope_rejected(change):
    with pytest.raises(ValueError):
        arguments(ARGS | change)


@pytest.mark.parametrize("failure", ["conflict", "status", "date", "truncated", "over-limit"])
def test_source_incomplete_cannot_publish_partial_groups(failure):
    rows = deepcopy(ROWS)
    if failure == "conflict":
        row = rows[0].copy()
        row[3] = 99
        rows.append(row)
    elif failure == "status":
        rows[3][2] = "unknown"
    elif failure == "date":
        rows[3][1] = "invalid"
    elif failure == "over-limit":
        rows = [rows[0]] * 101
    result = grouped(source(rows, truncated=failure == "truncated"), ARGS)
    assert not result["complete"] and result["groups"] == [] and result["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoked", "source", "revision"])
async def test_delivery_rechecks_do_not_release_old_groups(change):
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

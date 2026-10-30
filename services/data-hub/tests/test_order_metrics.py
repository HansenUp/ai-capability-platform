from copy import deepcopy

import pytest

from data_hub.order_metrics import FIELDS, aggregate, collect, window

WINDOW = {"since": "2026-10-01T00:00:00Z", "until": "2026-10-02T00:00:00Z"}
ROW = ["A", "2026-10-01T00:00:00Z", "completed", 1200, "CNY", "2026-10-02T00:00:00Z"]


def source(rows, truncated=False):
    return dict(columns=list(FIELDS), rows=rows, truncated=truncated, view_version="v1")


def test_cohort_currency_and_duplicate():
    usd = ROW.copy()
    usd[0] = "B"
    usd[3] = 500
    usd[4] = "USD"
    outside = ROW.copy()
    outside[0] = "C"
    outside[1] = WINDOW["until"]
    out = aggregate(source([ROW, ROW, usd, outside]), WINDOW)
    assert out["order_count"] == 2
    assert out["statuses"] == dict(pending=0, completed=2, cancelled=0)
    assert [g["total_minor"] for g in out["currencies"]] == ["1200", "500"]
    assert out["snapshot_guaranteed"] is False
    assert "order_id" not in str(out)


@pytest.mark.parametrize("amount", [None, True, 1.5, -1, "200"])
def test_missing_not_zero(amount):
    row = ROW.copy()
    row[3] = amount
    row[5] = "invalid"
    out = aggregate(source([row]), WINDOW)
    assert out["currencies"][0]["total_minor"] is None
    assert out["currencies"][0]["coverage"] == 0
    assert out["unknown_updated"] == 1


def test_zero_empty_and_statuses():
    row = ROW.copy()
    row[3] = 0
    assert aggregate(source([row]), WINDOW)["currencies"][0]["total_minor"] == "0"
    empty = aggregate(source([]), WINDOW)
    assert empty["order_count"] == 0 and empty["currencies"] == []
    row[2] = "cancelled"
    assert aggregate(source([row]), WINDOW)["statuses"]["cancelled"] == 1
    assert aggregate(source([row]), WINDOW)["currencies"] == []


@pytest.mark.parametrize("index,value", [(0, None), (1, "bad"), (2, "unknown")])
def test_invalid_scope_blocks_all(index, value):
    row = ROW.copy()
    row[index] = value
    out = aggregate(source([row]), WINDOW)
    assert not out["complete"] and out["order_count"] is None and out["currencies"] == []


def test_conflict_truncation_and_currency():
    row = ROW.copy()
    row[3] = 3
    assert not aggregate(source([ROW, row]), WINDOW)["complete"]
    assert not aggregate(source([ROW], True), WINDOW)["complete"]
    assert aggregate(source([ROW] * 100), WINDOW)["complete"]
    assert not aggregate(source([ROW] * 101), WINDOW)["complete"]
    row[4] = "OTHER"
    out = aggregate(source([row]), WINDOW)
    assert out["currencies"] == [] and out["reason"]


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        WINDOW | {"tenant": "other"},
        WINDOW | {"since": "2026-10-01"},
        WINDOW | {"since": WINDOW["until"]},
        WINDOW | {"until": "2026-11-02T00:00:00Z"},
    ],
)
def test_invalid_window(arguments):
    with pytest.raises(ValueError):
        window(arguments)


def test_timezone_and_exact_integers():
    args = WINDOW | {"since": "2026-10-01T08:00:00+08:00"}
    row = ROW.copy()
    row[3] = 10**20
    assert aggregate(source([row]), args)["currencies"][0]["total_minor"] == str(10**20)


@pytest.mark.asyncio
async def test_changed_authority_or_source_closes():
    state = {"revision": 1, "calls": 0}

    async def reader():
        state["calls"] += 1
        if state["calls"] == 2:
            state["revision"] = 2
        return source([ROW])

    with pytest.raises(ValueError, match="changed"):
        await collect(reader, WINDOW, lambda: state["revision"])
    assert state["calls"] == 2

    async def denied():
        raise ValueError("revoked")

    with pytest.raises(ValueError, match="revoked"):
        await collect(denied, WINDOW, lambda: 1)
    first = True

    async def changed():
        nonlocal first
        result = source([ROW]) if first else source([])
        first = False
        return deepcopy(result)

    with pytest.raises(ValueError, match="changed"):
        await collect(changed, WINDOW, lambda: 1)


def test_equal_numeric_values_with_different_types_are_conflicts():
    left, right = ROW.copy(), ROW.copy()
    left[3], right[3] = 1, True
    result = aggregate(source([left, right]), WINDOW)
    assert result["order_count"] is None and result["reason"] == "订单ID冲突"

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from test_order_daily_status import ARGS, ROWS, source

from data_hub.order_groups import arguments, collect_groups, grouped

SHARES = ARGS | {"metric": "status_share"}


@pytest.mark.parametrize("zone,totals", [("UTC", [4]), ("UTC+08:00", [1, 3])])
def test_daily_shares_have_per_day_all_status_denominator_and_three_categories(zone, totals):
    result = grouped(source(ROWS + [ROWS[0]]), SHARES | {"bucket_timezone": zone})
    assert result["complete"] and result["total_order_count"] == 4
    groups = result["groups"]
    assert len(groups) == len(totals) * 3
    for offset, n in enumerate(totals):
        day = groups[offset * 3 : offset * 3 + 3]
        assert [g["key"]["status"] for g in day] == ["pending", "completed", "cancelled"]
        assert sum(g["order_count"] for g in day) == n
        assert all(g["share_numerator"] == g["order_count"] and g["share_denominator"] == n for g in day)
        assert len({(g["key"]["day"], g["bucket_since"], g["bucket_until"], g["partial_bucket"]) for g in day}) == 1
    assert not any(k in str(result) for k in ("order_id", "amount_minor", "total_minor", "updated_at"))


def test_missing_status_is_zero_over_nonzero_day_and_empty_dates_are_absent():
    out = grouped(source(ROWS[:1]), SHARES)
    assert [g["share_numerator"] for g in out["groups"]] == [0, 1, 0]
    assert [g["share_denominator"] for g in out["groups"]] == [1, 1, 1]
    assert all(g["key"]["day"] == "2026-10-01" for g in out["groups"])
    for rows in ([], ROWS):
        empty = grouped(source(rows), SHARES | dict(since="2026-10-03T00:00:00Z", until="2026-10-04T00:00:00Z"))
        assert empty["complete"] and empty["total_order_count"] == 0 and empty["groups"] == []


def test_midnight_end_and_partial_day_denominator_match_daily_count():
    args = SHARES | dict(since="2026-10-01T16:00:00Z", until="2026-10-02T16:00:00Z")
    out = grouped(source(ROWS), args)
    assert out["total_order_count"] == 4 and all(not g["partial_bucket"] for g in out["groups"])
    narrow = args | {"until": "2026-10-01T16:00:00.001Z"}
    out = grouped(source(ROWS), narrow)
    assert out["total_order_count"] == 1 and [g["order_count"] for g in out["groups"]] == [0, 1, 0]
    assert all(g["partial_bucket"] for g in out["groups"])
    assert out["total_order_count"] == sum(
        g["order_count"] for g in grouped(source(ROWS), narrow | {"metric": "order_count", "group_by": "day"})["groups"]
    )


@pytest.mark.parametrize("fault", ["truncated", "over_limit", "conflict", "invalid_created", "invalid_status"])
def test_daily_share_source_fault_returns_no_partial_denominators(fault):
    rows = deepcopy(ROWS)
    if fault == "over_limit":
        rows = [[str(i), *ROWS[0][1:]] for i in range(101)]
    elif fault == "conflict":
        rows.append(["END", *ROWS[-1][1:3], True, *ROWS[-1][4:]])
    elif fault == "invalid_created":
        rows[-1][1] = "invalid"
    elif fault == "invalid_status":
        rows[-1][2] = "unknown"
    out = grouped(source(rows, fault == "truncated"), SHARES)
    assert not out["complete"] and out["groups"] == [] and out["total_order_count"] is None


@pytest.mark.parametrize(
    "change",
    [
        {"status": "pending"},
        {"status": "completed"},
        {"status": "cancelled"},
        {"group_by": "day"},
        {"group_by": "day_currency"},
        {"bucket_timezone": "Asia/Shanghai"},
        {"fill_zero": True},
    ],
)
def test_daily_shares_cannot_narrow_denominator_or_expand_scope(change):
    with pytest.raises(ValueError):
        arguments(SHARES | change)


def test_32_local_dates_return_96_groups_with_exact_daily_denominators():
    args = SHARES | dict(since="2026-10-01T01:00:00Z", until="2026-11-01T01:00:00Z", bucket_timezone="UTC")
    rows = [
        [
            str(i),
            args["since"] if i == 0 else (datetime(2026, 10, 1, tzinfo=UTC) + timedelta(days=i)).isoformat(),
            "completed",
            None,
            None,
            "invalid",
        ]
        for i in range(32)
    ]
    rows.extend([[str(i), *rows[0][1:]] for i in range(32, 100)])
    out = grouped(source(rows), args)
    assert out["complete"] and out["total_order_count"] == 100 and len(out["groups"]) == 96
    assert [g["share_denominator"] for g in out["groups"][:3]] == [69, 69, 69]
    assert all(g["share_denominator"] == 1 for g in out["groups"][3:])


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "revision", "revoked"])
async def test_daily_share_authorization_and_source_rechecked(change):
    calls = 0

    async def read():
        nonlocal calls
        calls += 1
        if calls == 2 and change == "revoked":
            raise PermissionError("revoked")
        return source(ROWS if calls == 1 or change != "source" else [])

    with pytest.raises((ValueError, PermissionError)):
        await collect_groups(read, SHARES, lambda: calls if change == "revision" else "p1")
    assert calls == 2

from copy import deepcopy

import pytest

from data_hub.order_groups import arguments, collect_groups, grouped
from data_hub.order_metrics import FIELDS

ARGS = dict(
    since="2026-10-01T00:00:00Z",
    until="2026-10-02T00:00:00Z",
    status="all",
    metric="order_count",
    group_by="day_status",
    bucket_timezone="UTC+08:00",
)
ROWS = [
    ["A", "2026-10-01T15:59:59.999Z", "completed", None, "EUR", "invalid"],
    ["B", "2026-10-01T16:00:00Z", "completed", True, None, "invalid"],
    ["C", "2026-10-01T16:00:00.001Z", "pending", -1, "CNY", "invalid"],
    ["D", "2026-10-01T16:00:00.001+00:00", "cancelled", 0, "USD", "invalid"],
    ["END", ARGS["until"], "completed", 999, "CNY", "invalid"],
]


def source(rows, truncated=False):
    return dict(columns=list(FIELDS), rows=rows, truncated=truncated, view_version="v1")


@pytest.mark.parametrize("zone", ["UTC", "UTC+08:00"])
@pytest.mark.parametrize("status", ["all", "completed", "pending", "cancelled"])
def test_date_status_partition_matches_daily_counts_and_has_no_money_dependency(zone, status):
    args = ARGS | dict(bucket_timezone=zone, status=status)
    out = grouped(source(ROWS + [ROWS[0]]), args)
    assert out["complete"] and out["bucket_timezone"] == zone
    expected = []
    for day, statuses in (
        [("2026-10-01", [("cancelled", 1), ("completed", 2), ("pending", 1)])]
        if zone == "UTC"
        else [("2026-10-01", [("completed", 1)]), ("2026-10-02", [("cancelled", 1), ("completed", 1), ("pending", 1)])]
    ):
        expected.extend((day, s, n) for s, n in statuses if status in ("all", s))
    assert [(g["key"]["day"], g["key"]["status"], g["order_count"]) for g in out["groups"]] == expected
    daily = grouped(source(ROWS), args | {"group_by": "day"})["groups"]
    for g in daily:
        matching = [s for s in out["groups"] if s["key"]["day"] == g["key"]["day"]]
        assert sum(s["order_count"] for s in matching) == g["order_count"]
        assert all(all(s[k] == g[k] for k in ("bucket_since", "bucket_until", "partial_bucket")) for s in matching)
    assert all(set(g) == {"key", "order_count", "bucket_since", "bucket_until", "partial_bucket"} for g in out["groups"])
    assert not any(k in str(out) for k in ("order_id", "amount_minor", "updated_at", "share_denominator", "total_minor"))


def test_sparse_statuses_empty_days_and_full_local_day():
    out = grouped(source(ROWS[:1]), ARGS)
    assert len(out["groups"]) == 1 and out["groups"][0]["key"] == {"day": "2026-10-01", "status": "completed"}
    assert grouped(source(ROWS[:1]), ARGS | {"status": "pending"})["groups"] == []
    assert grouped(source(ROWS), ARGS | dict(since="2026-10-03T00:00:00Z", until="2026-10-04T00:00:00Z"))["groups"] == []
    assert grouped(source([]), ARGS)["groups"] == []
    full = grouped(source(ROWS), ARGS | dict(since="2026-10-01T16:00:00Z", until="2026-10-02T16:00:00Z"))["groups"]
    assert all(g["key"]["day"] == "2026-10-02" and not g["partial_bucket"] for g in full)
    assert sum(g["order_count"] for g in full) == 4


@pytest.mark.parametrize("fault", ["truncated", "over_limit", "conflict", "invalid_created", "invalid_status"])
def test_whole_source_fault_never_returns_partial_date_status_groups(fault):
    rows = deepcopy(ROWS)
    if fault == "over_limit":
        rows = [[str(i), *ROWS[0][1:]] for i in range(101)]
    elif fault == "conflict":
        rows.append(["END", *ROWS[-1][1:3], True, *ROWS[-1][4:]])
    elif fault == "invalid_created":
        rows[-1][1] = "invalid"
    elif fault == "invalid_status":
        rows[-1][2] = "unknown"
    out = grouped(source(rows, fault == "truncated"), ARGS)
    assert not out["complete"] and out["groups"] == [] and out["reason"]


@pytest.mark.parametrize(
    "change",
    [
        {"metric": "completed_amount", "status": "completed"},
        {"metric": "completed_average", "status": "completed"},
        {"metric": "status_share", "status": "completed"},
        {"group_by": "day_status_currency"},
        {"bucket_timezone": "Asia/Shanghai"},
        {"status": "paid"},
        {"fill_zero": True},
    ],
)
def test_count_only_strict_scope(change):
    with pytest.raises(ValueError):
        arguments(ARGS | change)


def test_timezone_required_and_100_rows_can_form_96_date_status_groups():
    from datetime import UTC, datetime, timedelta

    missing = dict(ARGS)
    del missing["bucket_timezone"]
    with pytest.raises(ValueError):
        arguments(missing)
    args = ARGS | dict(since="2026-10-01T01:00:00Z", until="2026-11-01T01:00:00Z", bucket_timezone="UTC")
    rows = []
    for i in range(32):
        created = args["since"] if i == 0 else (datetime(2026, 10, 1, tzinfo=UTC) + timedelta(days=i)).isoformat()
        rows.extend([[f"{i}-{s}", created, s, None, None, "invalid"] for s in ("pending", "completed", "cancelled")])
    rows.extend([[str(i), *rows[0][1:]] for i in range(4)])
    out = grouped(source(rows), args)
    assert out["complete"] and len(out["groups"]) == 96 and sum(g["order_count"] for g in out["groups"]) == 100
    assert all(g["partial_bucket"] for g in out["groups"][:3] + out["groups"][-3:])


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "revision", "revoked"])
async def test_daily_status_double_read_fails_on_change_or_revocation(change):
    calls = 0

    async def read():
        nonlocal calls
        calls += 1
        if calls == 2 and change == "revoked":
            raise PermissionError("revoked")
        return source(ROWS if calls == 1 or change != "source" else [])

    with pytest.raises((ValueError, PermissionError)):
        await collect_groups(read, ARGS, lambda: calls if change == "revision" else "p1")
    assert calls == 2

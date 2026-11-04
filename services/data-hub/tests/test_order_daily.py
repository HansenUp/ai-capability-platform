from copy import deepcopy

import pytest

from data_hub.order_groups import arguments, collect_groups, grouped
from data_hub.order_metrics import FIELDS

ARGS = dict(
    since="2026-10-01T00:00:00Z",
    until="2026-10-02T00:00:00Z",
    status="all",
    metric="order_count",
    group_by="day",
    bucket_timezone="UTC+08:00",
)


def row(name, created, amount=100, currency="CNY", status="completed"):
    return [name, created, status, amount, currency, "invalid"]


def source(rows, truncated=False):
    return dict(columns=list(FIELDS), rows=rows, truncated=truncated, view_version="v1")


ROWS = [
    row("before", "2026-10-01T15:59:59.999Z"),
    row("midnight", "2026-10-01T16:00:00Z"),
    row("after", "2026-10-01T16:00:00.001Z"),
    row("excluded", ARGS["until"]),
]


def test_midnight_timezone_dedup_and_exclusive_end():
    output = grouped(source(ROWS + [ROWS[0]]), ARGS)
    assert output["complete"] and output["bucket_timezone"] == "UTC+08:00"
    assert [(g["key"], g["order_count"]) for g in output["groups"]] == [({"day": "2026-10-01"}, 1), ({"day": "2026-10-02"}, 2)]
    assert [(g["bucket_since"], g["bucket_until"], g["partial_bucket"]) for g in output["groups"]] == [
        ("2026-10-01T00:00:00+00:00", "2026-10-01T16:00:00+00:00", True),
        ("2026-10-01T16:00:00+00:00", "2026-10-02T00:00:00+00:00", True),
    ]
    utc = grouped(source(ROWS), ARGS | {"bucket_timezone": "UTC"})["groups"]
    assert len(utc) == 1 and utc[0]["order_count"] == 3 and not utc[0]["partial_bucket"]
    assert not any(k in str(output) for k in ("order_id", "amount_minor", "updated_at"))


def test_full_local_day_sparse_empty_and_clipped_window():
    args = ARGS | dict(since="2026-10-01T16:00:00Z", until="2026-10-02T16:00:00Z")
    g = grouped(source(ROWS), args)["groups"][0]
    assert g["key"]["day"] == "2026-10-02" and not g["partial_bucket"] and g["order_count"] == 3
    assert grouped(source([]), args)["groups"] == []
    clipped = grouped(source(ROWS), args | {"until": "2026-10-01T16:00:00.001Z"})["groups"][0]
    assert clipped["partial_bucket"] and clipped["order_count"] == 1
    assert clipped["bucket_until"] == "2026-10-01T16:00:00.001000+00:00"


@pytest.mark.parametrize("metric", ["completed_amount", "completed_average"])
@pytest.mark.parametrize("missing", [None, True, -1, 1.5, "100"])
def test_amount_integrity_per_day_currency_and_exact_zero(metric, missing):
    rows = [
        row("a", ROWS[0][1], 10**20 + 1),
        row("usd", ROWS[0][1], 0, "USD"),
        row("b", ROWS[1][1], 200),
        row("missing", ROWS[1][1], missing),
    ]
    args = ARGS | dict(metric=metric, group_by="day_currency", status="completed")
    groups = grouped(source(rows), args)["groups"]
    assert [g["total_minor"] for g in groups] == [str(10**20 + 1), "0", None]
    last = groups[-1]
    assert last["known_minor"] == "200" and last["amount_coverage"] == 0.5 and not last["amount_complete"]
    if metric == "completed_average":
        assert groups[0]["average_numerator_minor"] == str(10**20 + 1) and groups[0]["average_denominator"] == 1
        assert last["average_numerator_minor"] is None and last["average_denominator"] is None
    unknown = grouped(source([rows[-1]]), args)["groups"][0]
    assert unknown["known_minor"] is None and unknown["total_minor"] is None


def test_currency_only_blocks_selected_money_and_status_selection():
    rows = [row("bad", ROWS[0][1], None, "EUR", "pending"), ROWS[1]]
    assert grouped(source(rows), ARGS)["complete"]
    money = ARGS | dict(metric="completed_amount", status="completed", group_by="day_currency")
    assert grouped(source(rows), money)["complete"]
    rows[0][-4] = "completed"
    result = grouped(source(rows), money)
    assert not result["complete"] and result["groups"] == []


@pytest.mark.parametrize("fault", ["truncated", "over_limit", "conflict", "invalid_created", "invalid_status"])
def test_source_integrity_fail_closed_even_outside_window(fault):
    rows = deepcopy(ROWS)
    if fault == "over_limit":
        rows = [row(str(i), ARGS["since"]) for i in range(101)]
    elif fault == "conflict":
        rows.append(ROWS[0][:3] + [True] + ROWS[0][4:])
    elif fault == "invalid_created":
        rows[-1][1] = "invalid"
    elif fault == "invalid_status":
        rows[-1][2] = "unknown"
    out = grouped(source(rows, fault == "truncated"), ARGS)
    assert not out["complete"] and out["groups"] == []


def test_elapsed_31_days_can_intersect_32_dates_and_100_source_rows():
    args = ARGS | dict(since="2026-10-01T01:00:00Z", until="2026-11-01T01:00:00Z", bucket_timezone="UTC")
    rows = [row(str(i), args["since"]) for i in range(99)] + [row("last", "2026-11-01T00:00:00Z")]
    out = grouped(source(rows), args)
    assert out["complete"] and [g["key"]["day"] for g in out["groups"]] == ["2026-10-01", "2026-11-01"]
    assert sum(g["order_count"] for g in out["groups"]) == 100


@pytest.mark.parametrize(
    "change",
    [
        {"bucket_timezone": None},
        {"bucket_timezone": "Asia/Shanghai"},
        {"bucket_timezone": "America/New_York"},
        {"bucket_timezone": "UTC+09:00"},
        {"bucket_timezone": []},
        {"metric": "status_share"},
        {"group_by": "day_currency"},
        {"metric": "completed_amount"},
        {"metric": "completed_amount", "group_by": "day_currency"},
        {"extra": "sql"},
    ],
)
def test_restricted_daily_arguments(change):
    with pytest.raises(ValueError):
        arguments(ARGS | change)


def test_missing_timezone_and_timezone_on_legacy_query_rejected():
    value = dict(ARGS)
    del value["bucket_timezone"]
    with pytest.raises(ValueError):
        arguments(value)
    with pytest.raises(ValueError):
        arguments(ARGS | {"group_by": "status"})


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "revision", "revoked"])
async def test_two_authorized_reads_and_revocation(change):
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


@pytest.mark.parametrize(
    "zone,created", [("UTC", "9999-12-31T12:00:00Z"), ("UTC+08:00", "9999-12-31T20:00:00Z"), ("UTC+08:00", "0001-01-01T01:00:00Z")]
)
def test_unrepresentable_calendar_edges_fail_closed(zone, created):
    day = created[:10]
    args = ARGS | dict(since=day + "T00:00:00Z", until=day + "T23:59:59Z", bucket_timezone=zone)
    out = grouped(source([row("edge", created)]), args)
    assert not out["complete"] and out["groups"] == [] and out["reason"]


def test_31_elapsed_days_returns_32_sparse_calendar_buckets():
    from datetime import UTC, datetime, timedelta

    start = datetime(2026, 10, 1, tzinfo=UTC)
    args = ARGS | dict(since="2026-10-01T01:00:00Z", until="2026-11-01T01:00:00Z", bucket_timezone="UTC")
    rows = [row("first", args["since"])] + [row(str(i), (start + timedelta(days=i)).isoformat()) for i in range(1, 32)]
    groups = grouped(source(rows), args)["groups"]
    assert len(groups) == 32 and all(g["order_count"] == 1 for g in groups)
    assert groups[0]["partial_bucket"] and groups[-1]["partial_bucket"]
    assert not any(g["partial_bucket"] for g in groups[1:-1])

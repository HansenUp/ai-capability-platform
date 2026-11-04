from copy import deepcopy

import pytest

from data_hub.order_compare import arguments, collect_compare, compared
from data_hub.order_metrics import FIELDS

ARGS = dict(
    baseline_since="2026-10-01T00:00:00Z",
    baseline_until="2026-10-02T00:00:00Z",
    current_since="2026-10-02T00:00:00Z",
    current_until="2026-10-03T00:00:00Z",
    status="all",
)
ROWS = [
    ["A", ARGS["baseline_since"], "completed", None, "EUR", "invalid"],
    ["B", "2026-10-01T23:59:59.999Z", "pending", True, None, "invalid"],
    ["C", ARGS["current_since"], "completed", -1, "USD", "invalid"],
    ["D", ARGS["current_until"], "cancelled", 1, "CNY", "invalid"],
]


def source(rows, truncated=False):
    return dict(columns=list(FIELDS), rows=rows, truncated=truncated, view_version="v1")


@pytest.mark.parametrize("status,baseline,current", [("all", 2, 1), ("completed", 1, 1), ("pending", 1, 0), ("cancelled", 0, 0)])
def test_shared_status_filter_boundaries_dedup_and_no_money_dependency(status, baseline, current):
    out = compared(source(ROWS + [ROWS[0]]), ARGS | {"status": status})
    assert out["complete"] and out["baseline_count"] == baseline and out["current_count"] == current
    assert out["delta_count"] == current - baseline
    assert (out["relative_numerator"], out["relative_denominator"]) == ((current - baseline, baseline) if baseline else (None, None))
    assert not any(k in str(out) for k in ("order_id", "amount_minor", "currencies", "statuses", "updated_at"))


@pytest.mark.parametrize("baseline,current", [(0, 0), (0, 1), (3, 0), (1, 3), (2, 2)])
def test_exact_signed_change_and_zero_baseline_unknown(baseline, current):
    rows = [[f"b{i}", *ROWS[0][1:]] for i in range(baseline)] + [[f"c{i}", *ROWS[2][1:]] for i in range(current)]
    out = compared(source(rows), ARGS)
    assert out["complete"] and out["delta_count"] == current - baseline
    assert out["relative_numerator"] == (current - baseline if baseline else None)
    assert out["relative_denominator"] == (baseline or None)


def test_offsets_equal_duration_gap_and_31_day_limit_with_100_source_rows():
    offset = ARGS | dict(baseline_since="2026-10-01T08:00:00+08:00", baseline_until="2026-10-02T08:00:00+08:00")
    assert compared(source(ROWS), offset) == compared(source(ROWS), ARGS)
    args = ARGS | dict(
        baseline_since="2026-08-01T00:00:00Z",
        baseline_until="2026-09-01T00:00:00Z",
        current_since="2026-10-01T00:00:00Z",
        current_until="2026-11-01T00:00:00Z",
    )
    rows = [[str(i), args["baseline_since"] if i < 40 else args["current_since"], "pending", None, None, "invalid"] for i in range(100)]
    out = compared(source(rows), args)
    assert out["complete"] and (out["baseline_count"], out["current_count"], out["delta_count"]) == (40, 60, 20)
    assert out["relative_numerator"] == 20 and out["relative_denominator"] == 40


@pytest.mark.parametrize(
    "change",
    [
        {"current_until": "2026-10-04T00:00:00Z"},
        {"current_since": "2026-10-01T12:00:00Z", "current_until": "2026-10-02T12:00:00Z"},
        {"current_since": "2026-09-30T00:00:00Z", "current_until": "2026-10-01T00:00:00Z"},
        {"baseline_since": "2026-09-01T00:00:00Z", "baseline_until": "2026-10-03T00:00:00Z"},
        {"baseline_until": ARGS["baseline_since"]},
        {"baseline_since": "2026-02-30T00:00:00Z"},
        {"current_until": "2026-10-03T00:00:00"},
        {"status": "paid"},
        {"current_status": "pending"},
        {"metric": "completed_amount"},
    ],
)
def test_restricted_explicit_equal_chronological_windows(change):
    with pytest.raises(ValueError):
        arguments(ARGS | change)


def test_missing_parameter_rejected():
    value = dict(ARGS)
    del value["status"]
    with pytest.raises(ValueError):
        arguments(value)


@pytest.mark.parametrize("fault", ["truncated", "over_limit", "conflict", "invalid_created", "invalid_status", "columns"])
def test_invalid_full_source_releases_neither_window(fault):
    rows = deepcopy(ROWS)
    if fault == "over_limit":
        rows = [[str(i), *ROWS[0][1:]] for i in range(101)]
    elif fault == "conflict":
        rows.append(["D", *ROWS[-1][1:3], True, *ROWS[-1][4:]])
    elif fault == "invalid_created":
        rows[-1][1] = "invalid"
    elif fault == "invalid_status":
        rows[-1][2] = "unknown"
    result = source(rows, fault == "truncated")
    if fault == "columns":
        result["columns"] = list(reversed(FIELDS))
    out = compared(result, ARGS)
    assert not out["complete"] and out["reason"]
    assert all(out[k] is None for k in ("baseline_count", "current_count", "delta_count", "relative_numerator", "relative_denominator"))


@pytest.mark.asyncio
async def test_both_windows_use_one_source_followed_by_authorized_recheck():
    calls = 0

    async def read():
        nonlocal calls
        calls += 1
        return source(ROWS)

    out = await collect_compare(read, ARGS, lambda: "p1")
    assert calls == 2 and out["complete"] and out["read_started"] <= out["read_finished"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "revision", "revoked", "in_place_source", "in_place_revision"])
async def test_recheck_rejects_revocation_changes_and_mutable_provider_aliases(change):
    calls = 0
    original = source(deepcopy(ROWS))
    revision = {"v": 1}

    async def read():
        nonlocal calls
        calls += 1
        if calls == 2:
            if change == "revoked":
                raise PermissionError("revoked")
            if change in ("revision", "in_place_revision"):
                revision["v"] = 2
            if change == "source":
                return source([])
            if change == "in_place_source":
                original["rows"].clear()
        return original

    with pytest.raises((ValueError, PermissionError)):
        await collect_compare(read, ARGS, lambda: revision if change == "in_place_revision" else revision["v"])
    assert calls == 2


def test_unrepresentable_utc_arguments_and_source_fail_closed():
    with pytest.raises(ValueError):
        arguments(ARGS | dict(baseline_since="0001-01-01T00:00:00+14:00", baseline_until="0001-01-02T00:00:00+14:00"))
    rows = deepcopy(ROWS)
    rows[-1][1] = "0001-01-01T00:00:00+14:00"
    out = compared(source(rows), ARGS)
    assert not out["complete"] and out["baseline_count"] is None and out["current_count"] is None and out["reason"]

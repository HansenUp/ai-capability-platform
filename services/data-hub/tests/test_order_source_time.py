"""来源时区分量须有效；未知更新时间不影响创建窗口计数。"""

from datetime import UTC, datetime

import pytest

from data_hub.order_compare import compared
from data_hub.order_count import arguments as count_arguments
from data_hub.order_count import collect_counts
from data_hub.order_groups import grouped
from data_hub.order_metrics import FIELDS, aggregate, instant

ARGS = dict(
    baseline_since="2026-10-01T00:00:00Z",
    baseline_until="2026-10-02T00:00:00Z",
    current_since="2026-10-02T00:00:00Z",
    current_until="2026-10-03T00:00:00Z",
    status="all",
)
WINDOW = dict(since=ARGS["baseline_since"], until=ARGS["baseline_until"])


def source(created="2026-10-01T00:00:00Z", updated="invalid"):
    return dict(columns=list(FIELDS), rows=[["A", created, "pending", None, None, updated]], truncated=False, view_version="v1")


@pytest.mark.parametrize("offset", ["+00:60", "-00:60", "+0060", "+00:00:60", "+000060", "+00:60.1"])
def test_invalid_source_offset_fails_closed_in_all_projections(offset):
    for day in ("2026-10-01", "2026-09-01"):
        rows = source(day + "T01:00:00" + offset)
        metrics = aggregate(rows, WINDOW)
        comparison = compared(rows, ARGS)
        groups = grouped(rows, WINDOW | dict(status="all", metric="order_count", group_by="status"))
        assert not metrics["complete"] and metrics["order_count"] is None
        assert not groups["complete"] and groups["groups"] == []
        assert not comparison["complete"]
        assert all(
            comparison[k] is None for k in ("baseline_count", "current_count", "delta_count", "relative_numerator", "relative_denominator")
        )


@pytest.mark.parametrize("updated", ["0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00", "2026-10-01T00:00:00+00:60"])
def test_unusable_updated_at_is_unknown_without_poisoning_valid_creation_count(updated):
    rows = source(updated=updated)
    metrics = aggregate(rows, WINDOW)
    assert metrics["complete"] and metrics["order_count"] == 1 and metrics["unknown_updated"] == 1
    comparison = compared(rows, ARGS)
    assert comparison["complete"] and comparison["baseline_count"] == 1 and comparison["current_count"] == 0
    assert comparison["delta_count"] == comparison["relative_numerator"] == -1 and comparison["relative_denominator"] == 1


@pytest.mark.parametrize("created", ["0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00"])
def test_unrepresentable_created_at_is_invalid_fact(created):
    assert aggregate(source(created), WINDOW)["reason"] == "创建时间无效"
    assert not compared(source(created), ARGS)["complete"]


def test_valid_source_submillisecond_precision_and_timezone_seconds_are_preserved():
    assert instant("2026-10-01T01:00:00.999999+01:00") == datetime(2026, 10, 1, 0, 0, 0, 999999, tzinfo=UTC)
    assert instant("2026-10-01T00:00:30.000001+00:00:30") == datetime(2026, 10, 1, 0, 0, 0, 1, tzinfo=UTC)
    assert instant("20261001T010000.000001+0100") == datetime(2026, 10, 1, 0, 0, 0, 1, tzinfo=UTC)
    with pytest.raises(ValueError):
        count_arguments(WINDOW | dict(since="2026-10-01T00:00:00.000001Z", status="all"))


@pytest.mark.asyncio
async def test_count_projection_keeps_authority_recheck_with_unknown_updated_at():
    calls = []

    async def reader():
        calls.append("read")
        return source(updated="0001-01-01T00:00:00+14:00")

    output = await collect_counts(reader, WINDOW | dict(status="all"), lambda: "v1")
    assert output["complete"] and output["order_count"] == 1 and calls == ["read", "read"]

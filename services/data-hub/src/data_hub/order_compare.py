"""Two equal, disjoint creation windows over one complete authorized order source."""

from copy import deepcopy
from datetime import UTC, datetime

from data_hub.order_count import arguments as count_arguments
from data_hub.order_metrics import aggregate

PARAMETERS = ("baseline_since", "baseline_until", "current_since", "current_until", "status")


def arguments(value):
    if type(value) is not dict or set(value) != set(PARAMETERS):
        raise ValueError("only two explicit windows and one shared current-status filter accepted")
    bounds = []
    for role in ("baseline", "current"):
        try:
            bounds.append(count_arguments(dict(since=value[f"{role}_since"], until=value[f"{role}_until"], status=value["status"])))
        except OverflowError as exc:
            raise ValueError("comparison timestamp is outside representable UTC range") from exc
    (bs, bu), (cs, cu) = bounds
    if bu - bs != cu - cs:
        raise ValueError("comparison windows must have equal elapsed duration")
    if bu > cs:
        raise ValueError("baseline must end before or at current window start")
    return bs, bu, cs, cu


def compared(result, value):
    bs, bu, cs, cu = arguments(value)
    output = dict(
        profile="synthetic-order-compare-v1",
        baseline_since=bs.isoformat(timespec="milliseconds"),
        baseline_until=bu.isoformat(timespec="milliseconds"),
        current_since=cs.isoformat(timespec="milliseconds"),
        current_until=cu.isoformat(timespec="milliseconds"),
        status_filter=value["status"],
        view_version=result.get("view_version"),
        snapshot_guaranteed=False,
        complete=False,
        reason=None,
        baseline_count=None,
        current_count=None,
        delta_count=None,
        relative_numerator=None,
        relative_denominator=None,
    )
    counts = []
    for start, end in ((bs, bu), (cs, cu)):
        try:
            summary = aggregate(result, dict(since=start.isoformat(), until=end.isoformat()))
        except OverflowError:
            return output | {"reason": "来源创建时间超出有效UTC范围"}
        if not summary["complete"]:
            return output | {"reason": summary["reason"]}
        status = value["status"]
        counts.append(summary["order_count"] if status == "all" else summary["statuses"][status])
    baseline, current = counts
    delta = current - baseline
    return output | dict(
        complete=True,
        baseline_count=baseline,
        current_count=current,
        delta_count=delta,
        relative_numerator=delta if baseline else None,
        relative_denominator=baseline if baseline else None,
    )


async def collect_compare(reader, value, revision):
    """Both counts share the same source, verified again before releasing either."""
    arguments(value)
    before = deepcopy(revision())
    started = datetime.now(UTC).isoformat()
    result = deepcopy(await reader())
    output = compared(result, value)
    checked = await reader()
    if revision() != before or checked != result:
        raise ValueError("authority or source changed")
    return output | dict(read_started=started, read_finished=datetime.now(UTC).isoformat())

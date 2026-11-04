"""Bounded synthetic grouping, using the existing complete authorized order source."""

from datetime import UTC, datetime

from data_hub.order_count import arguments as count_arguments
from data_hub.order_metrics import FIELDS, aggregate, instant


def arguments(value):
    if type(value) is not dict or set(value) != {"since", "until", "status", "metric", "group_by"}:
        raise ValueError("only explicit window/status/metric/group_by accepted")
    bounds = count_arguments({k: value[k] for k in ("since", "until", "status")})
    if value["metric"] == "order_count":
        if value["group_by"] not in ("status", "currency", "status_currency"):
            raise ValueError("unsupported count grouping")
    elif value["metric"] in ("completed_amount", "completed_average"):
        if value["status"] != "completed" or value["group_by"] != "currency":
            raise ValueError("completed amounts require completed status and currency grouping")
    else:
        raise ValueError("unsupported metric")
    return bounds


def grouped(result, value):
    since, until = arguments(value)
    summary = aggregate(result, {k: value[k] for k in ("since", "until")})
    base = {
        "profile": "synthetic-order-groups-v1",
        "since": since.isoformat(),
        "until": until.isoformat(),
        "status_filter": value["status"],
        "metric": value["metric"],
        "group_by": value["group_by"],
        "view_version": result.get("view_version"),
        "snapshot_guaranteed": False,
        "complete": False,
        "reason": summary["reason"],
        "groups": [],
    }
    if not summary["complete"]:
        return base
    # aggregate validated every source row and typed duplicate before selection.
    unique = {row[0]: dict(zip(FIELDS, row, strict=True)) for row in result["rows"]}
    groups = {}
    for row in unique.values():
        if not since <= instant(row["created_at"]) < until or value["status"] not in ("all", row["status"]):
            continue
        dimensions = ("status", "currency") if value["group_by"] == "status_currency" else (value["group_by"],)
        if "currency" in dimensions and row["currency"] not in ("CNY", "USD"):
            return base | {"reason": "所选范围币种无效，无法保证分组完整", "groups": []}
        key = tuple(row[field] for field in dimensions)
        group = groups.setdefault(key, {"key": dict(zip(dimensions, key, strict=True)), "order_count": 0})
        group["order_count"] += 1
        if value["metric"] in ("completed_amount", "completed_average"):
            group.setdefault("valid_amount_count", 0)
            group.setdefault("known", 0)
            amount = row["amount_minor"]
            if type(amount) is int and amount >= 0:
                group["valid_amount_count"] += 1
                group["known"] += amount
    output = []
    for _, group in sorted(groups.items()):
        if value["metric"] in ("completed_amount", "completed_average"):
            subtotal = group.pop("known")
            n, valid = group["order_count"], group["valid_amount_count"]
            group.update(
                known_minor=str(subtotal) if valid else None,
                total_minor=str(subtotal) if valid == n else None,
                amount_coverage=valid / n,
                amount_complete=valid == n,
            )
            if value["metric"] == "completed_average":
                group.update(average_numerator_minor=str(subtotal) if valid == n else None, average_denominator=n if valid == n else None)
        output.append(group)
    return base | {"complete": True, "reason": None, "groups": output}


async def collect_groups(reader, value, revision):
    arguments(value)
    before = revision()
    started = datetime.now(UTC).isoformat()
    result = await reader()
    output = grouped(result, value)
    checked = await reader()
    if revision() != before or checked != result:
        raise ValueError("authority or source changed")
    return output | {"read_started": started, "read_finished": datetime.now(UTC).isoformat()}

"""Fixed-offset daily buckets over an already validated authorized order source."""

from datetime import UTC, datetime, time, timedelta, timezone

from data_hub.order_count import arguments as count_arguments
from data_hub.order_metrics import FIELDS, instant

ZONES = {"UTC": UTC, "UTC+08:00": timezone(timedelta(hours=8))}


def arguments(value):
    if type(value) is not dict or set(value) != {"since", "until", "status", "metric", "group_by", "bucket_timezone"}:
        raise ValueError("daily query requires explicit bucket timezone")
    bounds = count_arguments({k: value[k] for k in ("since", "until", "status")})
    if value["bucket_timezone"] not in ("UTC", "UTC+08:00"):
        raise ValueError("only UTC and fixed UTC+08:00 supported")
    if value["metric"] == "order_count":
        if value["group_by"] != "day":
            raise ValueError("daily count requires day grouping")
    elif value["metric"] in ("completed_amount", "completed_average"):
        if value["status"] != "completed" or value["group_by"] != "day_currency":
            raise ValueError("daily money requires completed status and day/currency grouping")
    else:
        raise ValueError("unsupported daily metric")
    return bounds


def groups(result, value, base, since, until):
    zone = ZONES[value["bucket_timezone"]]
    unique = {row[0]: dict(zip(FIELDS, row, strict=True)) for row in result["rows"]}
    buckets = {}
    for row in unique.values():
        created = instant(row["created_at"])
        if not since <= created < until or value["status"] not in ("all", row["status"]):
            continue
        try:
            day = created.astimezone(zone).date()
            start = datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)
            end = start + timedelta(days=1)  # Fixed offsets only; no DST calendar assumptions.
        except OverflowError:
            return base | {"reason": "日期超出可表示的日桶边界", "groups": []}
        key = {"day": day.isoformat()}
        if value["group_by"] == "day_currency":
            if row["currency"] not in ("CNY", "USD"):
                return base | {"reason": "所选范围币种无效，无法保证日分组完整", "groups": []}
            key["currency"] = row["currency"]
        identifier = (key["day"], key.get("currency", ""))
        bucket = buckets.setdefault(
            identifier,
            {
                "key": key,
                "order_count": 0,
                "bucket_since": max(start, since).isoformat(),
                "bucket_until": min(end, until).isoformat(),
                "partial_bucket": since > start or until < end,
            },
        )
        bucket["order_count"] += 1
        if value["metric"] != "order_count":
            bucket.setdefault("valid_amount_count", 0)
            bucket.setdefault("known", 0)
            amount = row["amount_minor"]
            if type(amount) is int and amount >= 0:
                bucket["valid_amount_count"] += 1
                bucket["known"] += amount
    output = []
    for _, bucket in sorted(buckets.items()):
        if value["metric"] != "order_count":
            known = bucket.pop("known")
            n, valid = bucket["order_count"], bucket["valid_amount_count"]
            bucket.update(
                valid_amount_count=valid,
                known_minor=str(known) if valid else None,
                total_minor=str(known) if valid == n else None,
                amount_coverage=valid / n,
                amount_complete=valid == n,
            )
            if value["metric"] == "completed_average":
                bucket.update(average_numerator_minor=str(known) if valid == n else None, average_denominator=n if valid == n else None)
        output.append(bucket)
    return base | {"complete": True, "reason": None, "groups": output}

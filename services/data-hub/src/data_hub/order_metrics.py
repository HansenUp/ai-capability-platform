"""Deterministic bounded order cohort calculation, with no I/O or model calls."""

from datetime import UTC, datetime, timedelta

FIELDS = ("order_id", "created_at", "status", "amount_minor", "currency", "updated_at")
STATUSES = ("pending", "completed", "cancelled")


def instant(value):
    if not isinstance(value, str):
        raise ValueError("timezone required")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone required")
    return result.astimezone(UTC)


def window(arguments):
    if type(arguments) is not dict or set(arguments) != {"since", "until"}:
        raise ValueError("only since/until accepted")
    since, until = (instant(arguments[k]) for k in ("since", "until"))
    if not timedelta(0) < until - since <= timedelta(days=31):
        raise ValueError("window must be positive and at most 31 days")
    return since, until


def aggregate(result, arguments):
    since, until = window(arguments)
    base = dict(
        profile="synthetic-order-metrics-v1",
        since=since.isoformat(),
        until=until.isoformat(),
        view_version=result.get("view_version"),
        snapshot_guaranteed=False,
        complete=False,
        reason=None,
        order_count=None,
        statuses=None,
        currencies=[],
        unknown_updated=0,
    )

    def unavailable(reason):
        return base | {"reason": reason}

    if (
        result.get("truncated") is not False
        or result.get("columns") != list(FIELDS)
        or (not isinstance(result.get("rows"), list) or len(result["rows"]) > 100)
    ):
        return unavailable("来源截断或范围不完整")
    if not isinstance(result.get("rows"), list):
        return unavailable("来源缺失")
    unique = {}
    for values in result["rows"]:
        if not isinstance(values, (list, tuple)) or len(values) != len(FIELDS):
            return unavailable("行结构无效")
        row = dict(zip(FIELDS, values, strict=True))
        oid = row["order_id"]
        if not isinstance(oid, str) or not oid:
            return unavailable("订单ID无效")
        if oid in unique and any(type(unique[oid][k]) is not type(row[k]) or unique[oid][k] != row[k] for k in FIELDS):
            return unavailable("订单ID冲突")
        unique[oid] = row
    cohort = []
    for row in unique.values():
        try:
            created = instant(row["created_at"])
        except (ValueError, TypeError):
            return unavailable("创建时间无效")
        if row["status"] not in STATUSES:
            return unavailable("订单状态无效")
        if since <= created < until:
            cohort.append(row)
    counts = {s: sum(row["status"] == s for row in cohort) for s in STATUSES}
    groups = {}
    invalid_currency = False
    unknown_updated = 0
    for row in cohort:
        try:
            instant(row["updated_at"])
        except (ValueError, TypeError):
            unknown_updated += 1
        if row["status"] != "completed":
            continue
        currency = row["currency"]
        if currency not in ("CNY", "USD"):
            invalid_currency = True
            continue
        group = groups.setdefault(currency, dict(currency=currency, completed=0, valid=0, known_minor=0))
        group["completed"] += 1
        amount = row["amount_minor"]
        if type(amount) is int and amount >= 0:
            group["valid"] += 1
            group["known_minor"] += amount
    currencies = []
    if not invalid_currency:
        for _, g in sorted(groups.items()):
            currencies.append(
                g
                | {
                    "known_minor": str(g["known_minor"]) if g["valid"] else None,
                    "total_minor": str(g["known_minor"]) if g["valid"] == g["completed"] else None,
                    "coverage": g["valid"] / g["completed"],
                }
            )
    return base | dict(
        complete=True,
        order_count=len(cohort),
        statuses=counts,
        currencies=currencies,
        reason="币种无效，正式金额聚合不可用" if invalid_currency else None,
        unknown_updated=unknown_updated,
    )


async def collect(reader, arguments, revision):
    """Read through the authority again before releasing an aggregate; no retries."""
    window(arguments)
    before = revision()
    started = datetime.now(UTC).isoformat()
    result = await reader()
    output = aggregate(result, arguments)
    checked = await reader()
    if revision() != before or checked != result:
        raise ValueError("authority or source changed")
    return output | {"read_started": started, "read_finished": datetime.now(UTC).isoformat()}

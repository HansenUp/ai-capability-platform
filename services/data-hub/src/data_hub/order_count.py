"""Count-only projection over the existing authorized synthetic order cohort."""

import re

from data_hub.order_metrics import STATUSES, collect, window

_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:\d{2})\Z")


def arguments(value):
    if type(value) is not dict or set(value) != {"since", "until", "status"}:
        raise ValueError("only since/until/status accepted")
    if value["status"] not in ("all", *STATUSES):
        raise ValueError("invalid status")
    if any(not isinstance(value[k], str) or not _INSTANT.fullmatch(value[k]) for k in ("since", "until")):
        raise ValueError("explicit timezone and timestamp required")
    for key in ("since", "until"):
        zone = value[key][-6:]
        if not value[key].endswith("Z") and (int(zone[1:3]) > 14 or int(zone[4:]) > 59 or (int(zone[1:3]) == 14 and int(zone[4:]) != 0)):
            raise ValueError("invalid timezone")
    return window({k: value[k] for k in ("since", "until")})


async def collect_counts(reader, value, revision):
    """No row/amount output. Truncation, revocation and source changes fail closed."""
    arguments(value)
    summary = await collect(reader, {k: value[k] for k in ("since", "until")}, revision)
    status = value["status"]
    counts = None
    if summary["complete"]:
        counts = {s: summary["statuses"][s] if status in ("all", s) else 0 for s in STATUSES}
    return {
        "profile": "synthetic-order-count-v1",
        "since": summary["since"],
        "until": summary["until"],
        "status_filter": status,
        "view_version": summary["view_version"],
        "snapshot_guaranteed": False,
        "read_started": summary["read_started"],
        "read_finished": summary["read_finished"],
        "complete": summary["complete"],
        "reason": None if summary["complete"] else summary["reason"],
        "order_count": sum(counts.values()) if counts is not None else None,
        "statuses": counts,
    }

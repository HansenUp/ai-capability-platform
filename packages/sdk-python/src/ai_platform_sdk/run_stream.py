"""Read-only persisted Run SSE. No reconnection, resume or business-write retries."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
from pydantic import TypeAdapter

from .types_runtime import RunEvent

_ROW = TypeAdapter(RunEvent)
_MAX_FRAME = 1024 * 1024


async def persisted_events(response: httpx.Response, run_id: str, cursor: int) -> AsyncIterator[RunEvent]:
    response.raise_for_status()
    if response.status_code != 200 or response.headers.get("content-type", "").split(";")[0].strip().lower() != "text/event-stream":
        raise ValueError("Run stream response is not SSE")
    import json

    data: list[str] = []
    event, event_id, size = "", "", 0
    async for line in response.aiter_lines():
        size += len(line.encode("utf-8"))
        if size > _MAX_FRAME:
            raise ValueError("Run SSE frame exceeds read limit")
        if not line:
            if data:
                value: Any = json.loads("\n".join(data))
                if not isinstance(value, dict) or value.get("run_id") != run_id:
                    raise ValueError("Run SSE scope mismatch")
                if event == "done":
                    if value.get("type") != "done" or value.get("stop_reason") not in {
                        "end_turn",
                        "max_tokens",
                        "cancelled",
                        "error",
                        "awaiting",
                    }:
                        raise ValueError("Invalid Run SSE done")
                    return  # A stream boundary, not proof of Run completion or write acknowledgement.
                seq = value.get("seq")
                if type(seq) is not int or not cursor < seq <= 2**53 or event_id != str(seq) or value.get("type") != event:
                    raise ValueError("Run SSE sequence or event mismatch")
                if not isinstance(value.get("payload"), dict):
                    raise ValueError("Invalid Run SSE payload")
                row = _ROW.validate_python(value)
                cursor = seq
                yield row
            data, event, event_id, size = [], "", "", 0
        elif not line.startswith(":"):
            field, separator, value = line.partition(":")
            value = value.removeprefix(" ") if separator else ""
            if field == "data":
                data.append(value)
            elif field == "event":
                event = value
            elif field == "id":
                event_id = value
    raise ValueError("Run SSE disconnected before done; resume reading manually from the last confirmed sequence")

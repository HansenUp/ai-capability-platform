"""Isolated in-process data.query adapter; never registered as a network tool."""

from __future__ import annotations

import math
from typing import Any

from .query import Query, QueryDenied, Result
from .reviewed import ReviewedQuery


def _parse(arguments: object) -> Query:
    if type(arguments) is not dict or set(arguments) != {"view_id", "version", "columns", "filters", "limit"}:
        raise QueryDenied()
    view_id = arguments["view_id"]
    version = arguments["version"]
    columns = arguments["columns"]
    filters = arguments["filters"]
    limit = arguments["limit"]
    if (
        type(view_id) is not str
        or type(version) is not str
        or type(columns) is not list
        or not all(type(column) is str for column in columns)
        or type(filters) is not list
        or type(limit) is not int
    ):
        raise QueryDenied()
    parsed = []
    for item in filters:
        if type(item) is not dict or set(item) != {"column", "value"} or type(item["column"]) is not str:
            raise QueryDenied()
        value = item["value"]
        if value is not None and type(value) not in (str, int):
            raise QueryDenied()
        parsed.append((item["column"], value))
    return Query(view_id, version, tuple(columns), tuple(parsed), limit)


def _json_result(result: Result) -> dict[str, Any]:
    rows = []
    for row in result.rows:
        if len(row) != len(result.columns):
            raise QueryDenied()
        values = []
        for value in row:
            if value is not None and type(value) not in (str, int, float):
                raise QueryDenied()
            if type(value) is float and not math.isfinite(value):
                raise QueryDenied()
            values.append(value)
        rows.append(values)
    return {"columns": list(result.columns), "rows": rows, "truncated": result.truncated, "view_version": result.view_version}


class LocalDataQuery:
    """Trusted host injects the reviewed guard; callers supply neither identity nor policy."""

    def __init__(self, guard: ReviewedQuery) -> None:
        self._guard = guard

    async def invoke(self, arguments: object) -> dict[str, Any]:
        try:
            request = _parse(arguments)
            return _json_result(await self._guard.query(request))
        except Exception:
            raise QueryDenied() from None

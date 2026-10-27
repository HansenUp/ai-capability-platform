"""Isolated HTTP contract adapter: MockTransport only, never enabled by environment.

The business API and downstream credential semantics are not approved; this module
provides no network connection, default client, or production route.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mcp_ticket.auth import Identity
from mcp_ticket.tools import GetIn, SearchIn

_MAX_BYTES = 1_048_576
_ORIGIN = "https://synthetic-ticket.invalid"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError("invalid JSON constant")


class ReadDenied(Exception):
    """Not found or not authorized, with no existence leak."""


class ReadUnavailable(Exception):
    """Untrusted or unavailable upstream, without upstream details."""


@dataclass(frozen=True)
class SyntheticCaller:
    """Injected test context; not proof of a real credential or authorization."""

    identity: Identity
    token: str = field(repr=False)


class Summary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=128)
    title: str
    status: str
    priority: str | None
    category: str | None
    owner: str
    created_at: str
    updated_at: str


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    time: str
    actor: str
    type: str
    content: str


class Ticket(Summary):
    tenant: str
    requester: str
    description: str
    records: list[Record] = Field(max_length=50)
    records_truncated: bool
    customer_feedback: dict[str, Any] | None
    closed_at: str | None


class SearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    items: list[Summary] = Field(max_length=50)
    next_cursor: str | None
    total: int | None = Field(default=None, ge=0)


class SyntheticHttpTicketAdapter:
    """Only a supplied MockTransport can be used; endpoint is fixed and non-routable."""

    def __init__(self, transport: httpx.MockTransport) -> None:
        if not isinstance(transport, httpx.MockTransport):
            raise ValueError("synthetic MockTransport required")
        self._transport = transport

    def _request(self, path: str, caller: SyntheticCaller, params: dict[str, Any] | None = None) -> Any:
        if (
            type(caller) is not SyntheticCaller
            or type(caller.identity) is not Identity
            or not isinstance(caller.identity.sub, str)
            or not caller.identity.sub.strip()
            or not isinstance(caller.identity.tenant, str)
            or not caller.identity.tenant.strip()
            or caller.identity.act_verified is not True
            or not isinstance(caller.token, str)
            or not caller.token.strip()
            or "\r" in caller.token
            or "\n" in caller.token
        ):
            raise ReadUnavailable()
        try:
            with httpx.Client(transport=self._transport, trust_env=False, follow_redirects=False, timeout=5.0) as client:
                with client.stream("GET", _ORIGIN + path, params=params, headers={"Authorization": "Bearer " + caller.token}) as response:
                    if response.status_code in (403, 404):
                        raise ReadDenied()
                    if response.status_code != 200:
                        raise ReadUnavailable()
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > _MAX_BYTES:
                            raise ReadUnavailable()
                    return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        except ReadDenied:
            raise
        except (httpx.HTTPError, ValueError, UnicodeError, TypeError, RecursionError, ReadUnavailable):
            raise ReadUnavailable() from None

    def get(self, body: GetIn, caller: SyntheticCaller) -> dict[str, Any]:
        if (
            type(body) is not GetIn
            or not isinstance(body.id, str)
            or not body.id.strip()
            or len(body.id) > 128
            or body.id in (".", "..")
            or "/" in body.id
            or "\\" in body.id
            or any(ord(char) < 32 or ord(char) == 127 for char in body.id)
        ):
            raise ReadUnavailable()
        raw = self._request("/tickets/" + quote(body.id, safe=""), caller)
        try:
            ticket = Ticket.model_validate(raw)
            if ticket.id != body.id or ticket.tenant != caller.identity.tenant:
                raise ReadUnavailable()
            # Only minimal synthetic fields cross this test-only tool boundary.
            # Real field approval and downstream authorization remain separate gates.
            return ticket.model_dump(include={"id", "title", "status"})
        except (ValidationError, ReadUnavailable):
            raise ReadUnavailable() from None

    def search(self, body: SearchIn, caller: SyntheticCaller) -> dict[str, Any]:
        if type(body) is not SearchIn:
            raise ReadUnavailable()
        params = body.model_dump(exclude_none=True)
        raw = self._request("/tickets", caller, params=params)
        try:
            result = SearchResult.model_validate(raw)
            ids = [item.id for item in result.items]
            if len(ids) > body.limit or len(ids) != len(set(ids)) or (not ids and result.next_cursor is not None):
                raise ReadUnavailable()
            return result.model_dump()
        except (ValidationError, ReadUnavailable):
            raise ReadUnavailable() from None

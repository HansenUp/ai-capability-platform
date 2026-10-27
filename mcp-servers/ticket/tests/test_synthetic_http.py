"""Contract tests for mock-only HTTP adapter; no real service is reachable."""

import json

import httpx
import pytest
from fastapi import HTTPException
from ticket_testkit import ALICE, bearer

from mcp_ticket.auth import Identity
from mcp_ticket.synthetic_http import ReadDenied, ReadUnavailable, SyntheticCaller, SyntheticHttpTicketAdapter
from mcp_ticket.tools import GetIn, SearchIn


@pytest.fixture
def synthetic_ticket():
    return {
        "id": "T-SYNTHETIC-1",
        "title": "synthetic",
        "status": "open",
        "priority": "low",
        "category": "network",
        "owner": ALICE,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "tenant": "default",
        "requester": ALICE,
        "description": "synthetic only",
        "records": [{"time": "2026-01-01T00:00:00Z", "actor": "synthetic", "type": "note", "content": "test"}],
        "records_truncated": False,
        "customer_feedback": None,
        "closed_at": None,
    }


def context(token="signed-test-token", tenant="default", verified=True):  # noqa: S107 - synthetic test credential
    return SyntheticCaller(Identity(sub=ALICE, tenant=tenant, act_verified=verified), token)


def test_get_forwards_only_bearer_and_maps_exact_readonly_shape(synthetic_ticket):
    seen = []

    def handler(request):
        seen.append(request)
        assert request.method == "GET" and request.url.host == "synthetic-ticket.invalid"
        assert request.url.path == "/tickets/T-SYNTHETIC-1"
        assert request.headers["authorization"] == "Bearer signed-test-token"  # noqa: S105 - synthetic
        assert "x-ai-dev-sub" not in request.headers and request.content == b""
        return httpx.Response(200, json=synthetic_ticket)

    adapter = SyntheticHttpTicketAdapter(httpx.MockTransport(handler))
    assert adapter.get(GetIn(id="T-SYNTHETIC-1"), context()) == {"id": "T-SYNTHETIC-1", "title": "synthetic", "status": "open"}
    assert len(seen) == 1 and "signed-test-token" not in repr(context())


def test_search_forwards_query_and_validates_paginated_shape(synthetic_ticket):
    summary = {k: synthetic_ticket[k] for k in ("id", "title", "status", "priority", "category", "owner", "created_at", "updated_at")}

    def handler(request):
        assert request.method == "GET" and request.url.path == "/tickets"
        assert request.url.params["q"] == "network" and request.url.params["limit"] == "2"
        return httpx.Response(200, json={"items": [summary], "next_cursor": "next", "total": 2})

    result = SyntheticHttpTicketAdapter(httpx.MockTransport(handler)).search(SearchIn(q="network", limit=2), context())
    assert result == {"items": [summary], "next_cursor": "next", "total": 2}


@pytest.mark.parametrize("status", [403, 404])
def test_denied_and_missing_use_same_exception(status):
    adapter = SyntheticHttpTicketAdapter(httpx.MockTransport(lambda request: httpx.Response(status, text="private detail")))
    with pytest.raises(ReadDenied) as error:
        adapter.get(GetIn(id="private"), context())
    assert str(error.value) == ""


@pytest.mark.parametrize("status", [301, 302, 401, 429, 500])
def test_other_upstream_errors_fail_closed_without_redirect_or_details(status):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(status, headers={"location": "https://redirect.invalid/private"}, text="secret upstream detail")

    with pytest.raises(ReadUnavailable) as error:
        SyntheticHttpTicketAdapter(httpx.MockTransport(handler)).get(GetIn(id="x"), context())
    assert str(error.value) == "" and seen == ["synthetic-ticket.invalid"]


@pytest.mark.parametrize("change", [{"tenant": "other"}, {"id": "wrong"}, {"records_truncated": "false"}, {"extra": "private"}])
def test_untrusted_detail_response_rejected(synthetic_ticket, change):
    adapter = SyntheticHttpTicketAdapter(httpx.MockTransport(lambda request: httpx.Response(200, json=synthetic_ticket | change)))
    with pytest.raises(ReadUnavailable):
        adapter.get(GetIn(id="T-SYNTHETIC-1"), context())


@pytest.mark.parametrize("mode", ["invalid-json", "oversize", "timeout"])
def test_upstream_bad_payload_or_transport_failure_is_sanitized(mode):
    def handler(request):
        if mode == "timeout":
            raise httpx.ReadTimeout("private upstream detail", request=request)
        return httpx.Response(200, content=b"{" if mode == "invalid-json" else b"x" * 1_048_577)

    with pytest.raises(ReadUnavailable) as error:
        SyntheticHttpTicketAdapter(httpx.MockTransport(handler)).get(GetIn(id="x"), context())
    assert str(error.value) == "" and error.value.__cause__ is None


@pytest.mark.parametrize("mode", ["duplicate", "oversize-page", "empty-with-cursor"])
def test_invalid_search_pagination_fails_closed(synthetic_ticket, mode):
    summary = {k: synthetic_ticket[k] for k in ("id", "title", "status", "priority", "category", "owner", "created_at", "updated_at")}
    items = [] if mode == "empty-with-cursor" else [summary, summary]
    result = {"items": items, "next_cursor": "next", "total": 2}
    adapter = SyntheticHttpTicketAdapter(httpx.MockTransport(lambda request: httpx.Response(200, json=result)))
    with pytest.raises(ReadUnavailable):
        adapter.search(SearchIn(limit=1 if mode == "oversize-page" else 2), context())


def test_context_without_verified_delegation_fails_before_request():
    def forbidden(request):
        pytest.fail("unverified identity reached transport")

    adapter = SyntheticHttpTicketAdapter(httpx.MockTransport(forbidden))
    with pytest.raises(ReadUnavailable):
        adapter.get(GetIn(id="x"), context(verified=False))
    with pytest.raises(ValueError):
        SyntheticHttpTicketAdapter(httpx.HTTPTransport())


def test_signed_mcp_get_path_with_injected_synthetic_adapter(client, idp, monkeypatch, synthetic_ticket):
    from mcp_ticket import main

    token = idp.token(sub=ALICE, tenant="default", roles=("sales",))
    calls = []

    def downstream(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer " + token
        return httpx.Response(200, content=json.dumps(synthetic_ticket).encode())

    adapter = SyntheticHttpTicketAdapter(httpx.MockTransport(downstream))

    def local_bridge(ident, body):
        try:
            return adapter.get(body, SyntheticCaller(ident, token))
        except ReadDenied:
            raise HTTPException(404, "ticket not found or no permission") from None
        except ReadUnavailable:
            raise HTTPException(503, "ticket source unavailable") from None

    monkeypatch.setattr(main, "ticket_get", local_bridge)
    response = client.post("/tools/ticket.get", json={"id": "T-SYNTHETIC-1"}, headers=bearer(token))
    assert response.status_code == 200
    assert response.json() == {"id": "T-SYNTHETIC-1", "title": "synthetic", "status": "open"}
    assert len(calls) == 1
    denied = client.post("/tools/ticket.get", json={"id": "T-SYNTHETIC-1"})
    assert denied.status_code == 401 and len(calls) == 1


@pytest.mark.parametrize("identifier", ["../other", "a/b", "a\\b", ".", "..", "invalid\nheader"])
def test_unsafe_ticket_id_never_reaches_transport(identifier):
    def forbidden(request):
        pytest.fail("invalid identifier reached transport")

    adapter = SyntheticHttpTicketAdapter(httpx.MockTransport(forbidden))
    with pytest.raises(ReadUnavailable):
        adapter.get(GetIn(id=identifier), context())


def test_get_projection_excludes_personal_and_sensitive_fields(synthetic_ticket):
    synthetic_ticket.update(
        description="SECRET-DESCRIPTION",
        owner="SECRET-OWNER",
        requester="SECRET-REQUESTER",
        customer_feedback={"private": "SECRET-FEEDBACK"},
        records=[{"time": "now", "actor": "SECRET-ACTOR", "type": "note", "content": "SECRET-RECORD"}],
    )
    adapter = SyntheticHttpTicketAdapter(httpx.MockTransport(lambda request: httpx.Response(200, json=synthetic_ticket)))
    result = adapter.get(GetIn(id="T-SYNTHETIC-1"), context())
    assert result == {"id": "T-SYNTHETIC-1", "title": "synthetic", "status": "open"}
    assert "SECRET-" not in json.dumps(result)


def test_downstream_row_authority_is_checked_each_time_and_revocation_fails_closed(synthetic_ticket):
    # These tokens and ACLs are local stand-ins; real token verification is not implemented here.
    acl = {"alice-token": ("default", ALICE), "bob-token": ("default", "bob"), "other-token": ("other", ALICE)}
    authorized = {("default", ALICE)}
    calls = []

    def handler(request):
        calls.append(request.method)
        principal = acl.get(request.headers["authorization"].removeprefix("Bearer "))
        if principal not in authorized:
            return httpx.Response(403, text="private acl")
        return httpx.Response(200, json=synthetic_ticket)

    adapter = SyntheticHttpTicketAdapter(httpx.MockTransport(handler))
    alice = SyntheticCaller(Identity(sub=ALICE, tenant="default", act_verified=True), "alice-token")
    bob = SyntheticCaller(Identity(sub="bob", tenant="default", act_verified=True), "bob-token")
    other = SyntheticCaller(Identity(sub=ALICE, tenant="other", act_verified=True), "other-token")
    assert adapter.get(GetIn(id="T-SYNTHETIC-1"), alice)["id"] == "T-SYNTHETIC-1"
    for caller in (bob, other):
        with pytest.raises(ReadDenied):
            adapter.get(GetIn(id="T-SYNTHETIC-1"), caller)
    authorized.clear()
    with pytest.raises(ReadDenied):
        adapter.get(GetIn(id="T-SYNTHETIC-1"), alice)
    assert calls == ["GET"] * 4


@pytest.mark.parametrize("mode", ["duplicate-id", "duplicate-nested", "nan"])
def test_ambiguous_or_nonstandard_json_fails_closed_without_detail(synthetic_ticket, mode):
    synthetic_ticket["customer_feedback"] = {"rating": 1}
    payload = json.dumps(synthetic_ticket)
    if mode == "duplicate-id":
        payload = payload.replace('"id": "T-SYNTHETIC-1"', '"id": "other", "id": "T-SYNTHETIC-1"')
    elif mode == "duplicate-nested":
        payload = payload.replace('"rating": 1', '"rating": 2, "rating": 1')
    else:
        payload = payload.replace('"rating": 1', '"rating": NaN')
    adapter = SyntheticHttpTicketAdapter(httpx.MockTransport(lambda request: httpx.Response(200, content=payload.encode())))
    with pytest.raises(ReadUnavailable) as error:
        adapter.get(GetIn(id="T-SYNTHETIC-1"), context())
    assert str(error.value) == "" and error.value.__cause__ is None


def test_excessive_json_nesting_fails_closed_without_upstream_detail():
    payload = b"[" * 3000 + b"0" + b"]" * 3000
    adapter = SyntheticHttpTicketAdapter(httpx.MockTransport(lambda request: httpx.Response(200, content=payload)))
    with pytest.raises(ReadUnavailable) as error:
        adapter.get(GetIn(id="T-SYNTHETIC-1"), context())
    assert str(error.value) == "" and error.value.__cause__ is None

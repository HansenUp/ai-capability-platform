"""Synthetic-only composition test; this does not wire the application demo or a real provider."""

import httpx
import pytest

from mcp_ticket.auth import Identity
from mcp_ticket.synthetic_http import ReadDenied, ReadUnavailable, SyntheticCaller, SyntheticHttpTicketAdapter
from mcp_ticket.tools import GetIn


def ticket(title, status):
    return {
        "id": "SYNTHETIC-1",
        "tenant": "test-tenant",
        "title": title,
        "status": status,
        "priority": None,
        "category": None,
        "owner": "PRIVATE-OWNER",
        "requester": "PRIVATE-REQUESTER",
        "description": "PRIVATE-DESCRIPTION",
        "records": [],
        "records_truncated": False,
        "customer_feedback": {"note": "PRIVATE-FEEDBACK"},
        "created_at": "synthetic-time",
        "updated_at": "synthetic-time",
        "closed_at": None,
    }


class SyntheticModel:
    def __init__(self):
        self.inputs = []

    def summarize(self, projected):
        self.inputs.append(projected.copy())
        return f"模拟摘要：{projected['id']} / {projected['title']} / {projected['status']}"


def run_synthetic_flow(adapter, caller, model):
    # This composition is test-only and receives an already validated caller stand-in.
    try:
        projected = adapter.get(GetIn(id="SYNTHETIC-1"), caller)
    except (ReadDenied, ReadUnavailable):
        return {"status": "unavailable", "summary": None}
    return {"status": "ok", "summary": model.summarize(projected)}


@pytest.fixture
def caller():
    return SyntheticCaller(Identity(sub="test-user", tenant="test-tenant", act_verified=True), "test-token")


@pytest.mark.parametrize("title,status", [("合成打印故障", "open"), ("合成网络故障", "resolved")])
def test_projection_is_model_input_and_summary_depends_on_input(caller, title, status):
    requests = []

    def handler(request):
        requests.append(request.method)
        return httpx.Response(200, json=ticket(title, status))

    model = SyntheticModel()
    result = run_synthetic_flow(SyntheticHttpTicketAdapter(httpx.MockTransport(handler)), caller, model)
    assert requests == ["GET"]
    assert model.inputs == [{"id": "SYNTHETIC-1", "title": title, "status": status}]
    assert result == {"status": "ok", "summary": f"模拟摘要：SYNTHETIC-1 / {title} / {status}"}
    assert "PRIVATE-" not in str(model.inputs) and "test-token" not in str(model.inputs)


@pytest.mark.parametrize("mode", ["denied", "missing", "unavailable", "wrong-tenant", "invalid-json"])
def test_failure_never_calls_model_or_produces_success_summary(caller, mode):
    def handler(request):
        if mode == "denied":
            return httpx.Response(403, text="private")
        if mode == "missing":
            return httpx.Response(404, text="private")
        if mode == "unavailable":
            return httpx.Response(503, text="private")
        if mode == "invalid-json":
            return httpx.Response(200, text="{")
        return httpx.Response(200, json=ticket("synthetic", "open") | {"tenant": "another"})

    model = SyntheticModel()
    result = run_synthetic_flow(SyntheticHttpTicketAdapter(httpx.MockTransport(handler)), caller, model)
    assert result == {"status": "unavailable", "summary": None}
    assert model.inputs == []

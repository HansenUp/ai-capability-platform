from copy import deepcopy

import httpx
import pytest

from console_api import trace_metrics

HEADERS = {"X-Console-Tenant": "trace-a", "X-Console-Role": "TENANT_ADMIN"}
PARAMS = {"agent_id": "metric-agent", "since": "2026-10-18T00:00:00Z", "until": "2026-10-19T00:00:00Z"}


def trace(identifier, tenant="trace-a", tokens=60, cost="0.002"):
    return {
        "id": identifier,
        "timestamp": "2026-10-18T12:00:00Z",
        "metadata": {"attributes": {"ai.tenant_id": tenant, "ai.agent_id": "metric-agent"}},
        "observations": [
            {
                "id": "gen-" + identifier,
                "traceId": identifier,
                "type": "GENERATION",
                "usage": {"unit": "TOKENS", "total": tokens},
                "usageDetails": {"total": tokens},
                "totalCost": cost,
                "calculatedTotalCost": 0,
                "costDetails": {},
            }
        ],
        "input": "never-return-this-input",
        "output": "never-return-this-output",
    }


@pytest.fixture
def source(c, monkeypatch):
    monkeypatch.setenv("LANGFUSE_HOST", "http://127.0.0.1:3000")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "local-public")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "local-secret")
    monkeypatch.setenv("CONSOLE_ENV", "dev")
    assert c.post("/agents", headers=HEADERS, json={"agent_id": "metric-agent", "version": "1"}).status_code == 201
    rows = [trace("first"), trace("foreign", tenant="trace-b"), trace("second", tokens=0, cost=0)]
    calls = []
    state = {"rows": rows, "mode": "ok"}

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        if state["mode"] == "outage":
            return httpx.Response(503, text="secret backend diagnostics")
        if request.url.path == "/api/public/traces":
            assert request.url.params["name"] == "run metric-agent"
            limit, page = int(request.url.params["limit"]), int(request.url.params["page"])
            rows = state["rows"]
            total = len(rows)
            body = {
                "data": rows[(page - 1) * limit : page * limit],
                "meta": {"page": page, "limit": limit, "totalItems": total, "totalPages": (total + limit - 1) // limit},
            }
            if state["mode"] == "truncated":
                body["meta"]["totalItems"] += 1
            if state["mode"] == "overflow":
                body["meta"].update(totalItems=999999, totalPages=10000)
            return httpx.Response(200, json=body)
        row = next(row for row in state["rows"] if request.url.path.endswith("/" + row["id"]))
        value = deepcopy(row)
        if state["mode"] == "detail_tenant_drift":
            value["metadata"]["attributes"]["ai.tenant_id"] = "other"
        return httpx.Response(200, json=value)

    monkeypatch.setattr(trace_metrics, "_transport", httpx.MockTransport(handler))
    return state, calls


def test_trace_metrics_tenant_scope_and_missing_values(c, source):
    state, calls = source
    response = c.get("/metrics/traces", headers=HEADERS, params=PARAMS)
    assert response.status_code == 200, response.text
    metrics = response.json()
    assert metrics["trace_calls"] == 2 and metrics["generations"] == 2
    assert metrics["tokens"]["total"] == 60 and metrics["cost_usd"]["total"] == "0.002"
    assert metrics["run_metrics_available"] is False
    assert not any(request.url.path.endswith("/foreign") for request in calls)
    assert "never-return" not in response.text and "local-secret" not in response.text
    state["rows"][0]["observations"][0].update(usageDetails={}, totalCost=None, calculatedTotalCost=0)
    metrics = c.get("/metrics/traces", headers=HEADERS, params=PARAMS).json()
    assert metrics["tokens"]["total"] is None and metrics["cost_usd"]["total"] is None
    assert metrics["tokens"]["traces_with_complete_usage"] == 1
    assert metrics["cost_usd"]["traces_with_complete_cost"] == 1
    state["rows"] = []
    metrics = c.get("/metrics/traces", headers=HEADERS, params=PARAMS).json()
    assert metrics["trace_calls"] == 0 and metrics["tokens"]["total"] is None


@pytest.mark.parametrize("mode", ["outage", "truncated", "overflow", "detail_tenant_drift", "duplicate", "bad_timestamp", "conflict"])
def test_trace_metrics_fail_closed(c, source, mode):
    state, _ = source
    state["mode"] = mode
    if mode == "duplicate":
        state["rows"].append(deepcopy(state["rows"][0]))
    if mode == "bad_timestamp":
        state["rows"][0]["timestamp"] = "2026-10-18"
    if mode == "conflict":
        state["rows"][0]["metadata"]["tenant_id"] = "other"
    response = c.get("/metrics/traces", headers=HEADERS, params=PARAMS)
    assert response.status_code == 503
    assert "trace_calls" not in response.text and "secret backend" not in response.text


def test_trace_metrics_complete_pagination(c, source):
    state, _ = source
    state["rows"] = [trace(str(index)) for index in range(101)]
    response = c.get("/metrics/traces", headers=HEADERS, params=PARAMS)
    assert response.status_code == 200 and response.json()["trace_calls"] == 101
    assert response.json()["tokens"]["total"] == 6060


@pytest.mark.parametrize("cost", [None, -1, "NaN", True])
def test_trace_metrics_unknown_cost_is_not_zero(c, source, cost):
    state, _ = source
    state["rows"] = [trace("missing-cost", cost=cost)]
    metrics = c.get("/metrics/traces", headers=HEADERS, params=PARAMS).json()
    assert metrics["cost_usd"]["total"] is None
    assert metrics["cost_usd"]["traces_with_complete_cost"] == 0


def test_trace_metrics_non_token_units_and_missing_generations(c, source):
    state, _ = source
    state["rows"] = [trace("characters")]
    state["rows"][0]["observations"][0]["usage"]["unit"] = "CHARACTERS"
    assert c.get("/metrics/traces", headers=HEADERS, params=PARAMS).json()["tokens"]["total"] is None
    state["rows"][0]["observations"] = []
    metrics = c.get("/metrics/traces", headers=HEADERS, params=PARAMS).json()
    assert metrics["trace_calls"] == 1 and metrics["generations"] == 0
    assert metrics["cost_usd"]["total"] is None and metrics["tokens"]["total"] is None


def test_trace_metrics_permissions_windows_and_config(c, source, monkeypatch):
    _, calls = source
    for role in ("USER", "ADMIN"):
        assert c.get("/metrics/traces", headers=HEADERS | {"X-Console-Role": role}, params=PARAMS).status_code == 403
    assert c.get("/metrics/traces", headers=HEADERS | {"X-Console-Tenant": "other"}, params=PARAMS).status_code == 404
    assert not calls
    for change in (
        {"since": "2026-10-18"},
        {"until": "2026-10-18T00:00:00Z"},
        {"until": "2026-11-18T00:00:00Z"},
        {"since": "0001-01-01T00:00:00+14:00", "until": "0001-01-02T00:00:00+14:00"},
    ):
        assert c.get("/metrics/traces", headers=HEADERS, params=PARAMS | change).status_code == 422
    monkeypatch.setenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
    assert c.get("/metrics/traces", headers=HEADERS, params=PARAMS).status_code == 503
    monkeypatch.setenv("LANGFUSE_HOST", "http://127.0.0.1:3000")
    monkeypatch.delenv("LANGFUSE_SECRET_KEY")
    assert c.get("/metrics/traces", headers=HEADERS, params=PARAMS).status_code == 503

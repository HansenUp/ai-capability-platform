import httpx
import pytest

from console_api import eval_gate


@pytest.mark.parametrize(
    "field,value",
    [
        ("MODE", "langfues"),
        ("MIN_PASS_RATE", float("nan")),
        ("MIN_PASS_RATE", float("inf")),
        ("MIN_PASS_RATE", -0.1),
        ("MIN_PASS_RATE", 1.1),
        ("MIN_PASS_RATE", True),
        ("MIN_ITEMS", 0),
        ("MIN_ITEMS", -1),
        ("MIN_ITEMS", 1.5),
        ("MIN_ITEMS", True),
        ("TRACE_MAX_AGE_S", 0),
        ("TRACE_MAX_AGE_S", float("nan")),
        ("TRACE_SKEW_S", -1),
        ("TRACE_SKEW_S", float("inf")),
    ],
)
async def test_invalid_gate_configuration_rejects_publish_and_startup(monkeypatch, field, value):
    monkeypatch.setattr(eval_gate, field, value)
    monkeypatch.setenv("CONSOLE_ENV", "dev")
    assert "invalid evaluation gate configuration" in await eval_gate.check("agent", "1", None, True)
    with pytest.raises(RuntimeError, match="配置非法"):
        eval_gate.validate_for_production()


@pytest.mark.parametrize("total", [51, -1, 0, True, "2", 1.5])
async def test_invalid_pagination_rejects_partial_results(total):
    def handler(request):
        return httpx.Response(200, json={"data": [{"datasetItemId": "good", "traceId": "good"}], "meta": {"totalPages": total}})

    async with httpx.AsyncClient(base_url="https://langfuse.test", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(eval_gate._Unavailable):
            await eval_gate._run_items(client, {"datasetId": "dataset"}, "run")


async def test_pagination_without_total_pages_reads_until_empty():
    pages = []

    def handler(request):
        page = int(request.url.params["page"])
        pages.append(page)
        return httpx.Response(200, json={"data": [{"datasetItemId": str(page), "traceId": str(page)}] if page < 3 else []})

    async with httpx.AsyncClient(base_url="https://langfuse.test", transport=httpx.MockTransport(handler)) as client:
        items = await eval_gate._run_items(client, {"datasetId": "dataset"}, "run")
    assert pages == [1, 2, 3] and len(items) == 2


async def test_pagination_never_ending_or_shrinking_fails_closed(monkeypatch):
    monkeypatch.setattr(eval_gate, "MAX_PAGES", 3)
    for shrinking in (True, False):

        def handler(request, shrinking=shrinking):
            page = int(request.url.params["page"])
            body = {"data": [{"datasetItemId": str(page), "traceId": str(page)}]}
            if shrinking:
                body["meta"] = {"totalPages": 3 if page == 1 else 2}
            return httpx.Response(200, json=body)

        async with httpx.AsyncClient(base_url="https://langfuse.test", transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(eval_gate._Unavailable):
                await eval_gate._run_items(client, {"datasetId": "dataset"}, "run")


@pytest.mark.parametrize("items", [[None], [1], {"not": "an array"}, [{"traceId": "orphan"}]])
async def test_malformed_items_do_not_disappear_from_denominator(items):
    async with httpx.AsyncClient(base_url="https://langfuse.test") as client:
        with pytest.raises(eval_gate._Unavailable):
            await eval_gate.recompute(client, {"datasetRunItems": items}, "run")


@pytest.mark.parametrize("value", [True, "1", None, 0])
async def test_only_numeric_pass_scores_are_accepted(value):
    trace = {"timestamp": "2026-10-18T00:00:00Z", "scores": [{"name": "eval_pass", "value": value}]}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=trace))
    run = {"createdAt": "2026-10-18T00:01:00Z", "datasetRunItems": [{"datasetItemId": "one", "traceId": "one"}]}
    async with httpx.AsyncClient(base_url="https://langfuse.test", transport=transport) as client:
        assert await eval_gate.recompute(client, run, "run") == (0, 1)


def test_naive_timestamp_is_not_host_timezone_dependent():
    assert eval_gate._ts("2026-10-18T00:00:00") is None
    assert eval_gate._ts("2026-10-18T00:00:00Z") == eval_gate._ts("2026-10-18T08:00:00+08:00")


def test_policy_read_and_publish_audit_are_tenant_scoped(c, monkeypatch):
    tenant = {"X-Console-Tenant": "eval-policy", "X-Console-Role": "TENANT_ADMIN"}
    monkeypatch.setattr(eval_gate, "DATASETS", {"policy-agent": ["policy-dataset"]})
    assert c.post("/agents", headers=tenant, json={"agent_id": "policy-agent", "version": "1"}).status_code == 201
    response = c.get("/agents/policy-agent/eval-gate", headers=tenant)
    assert response.status_code == 200
    policy = response.json()
    assert policy["min_items"] == 30 and policy["min_pass_rate"] == 0.8
    assert policy["datasets"] == ["policy-dataset"]
    assert "KEY" not in str(policy) and "Authorization" not in str(policy)
    assert c.get("/agents/policy-agent/eval-gate", headers=tenant | {"X-Console-Role": "USER"}).status_code == 403
    assert (
        c.get("/agents/policy-agent/eval-gate", headers=tenant | {"X-Console-Role": "ADMIN", "X-Console-User": "unassigned"}).status_code
        == 403
    )
    assert c.get("/agents/policy-agent/eval-gate", headers=tenant | {"X-Console-Tenant": "other"}).status_code == 404
    assert c.post("/agents/policy-agent/versions/1/publish", headers=tenant, json={"eval_passed": True}).status_code == 200
    row = next(row for row in c.get("/audit", headers=tenant).json() if row["action"] == "agent.publish")
    assert row["detail"]["eval_gate"] == policy and row["detail"]["eval_checked"] is True


def test_invalid_policy_never_syncs_a_release(c, runtime, monkeypatch):
    assert c.post("/agents", json={"agent_id": "invalid-policy-agent", "version": "1"}).status_code == 201
    calls = len(runtime.calls)
    monkeypatch.setattr(eval_gate, "MIN_PASS_RATE", float("nan"))
    response = c.post("/agents/invalid-policy-agent/versions/1/publish", json={"eval_passed": True})
    assert response.status_code == 409 and len(runtime.calls) == calls
    assert c.get("/agents/invalid-policy-agent/eval-gate").status_code == 409

import asyncio

import httpx
import pytest

from console_api import eval_gate


def setup(c, monkeypatch, agent="s11-eval", score=1, metadata=None, status=200):
    monkeypatch.setattr(eval_gate, "MODE", "langfuse")
    monkeypatch.setattr(eval_gate, "MIN_ITEMS", 2)
    monkeypatch.setattr(eval_gate, "DATASETS", {agent: ["dataset"]})
    assert c.post("/agents", json={"agent_id": agent, "version": "1"}).status_code == 201
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if "/datasets/" in request.url.path:
            return httpx.Response(
                status,
                json={
                    "metadata": metadata or {"agent_id": agent, "agent_version": "1", "tenant_id": "default"},
                    "createdAt": "2026-10-21T00:01:00Z",
                    "datasetRunItems": [{"datasetItemId": str(index), "traceId": str(index)} for index in [1, 2]],
                },
            )
        return httpx.Response(
            200,
            json={
                "id": request.url.path.split("/")[-1],
                "timestamp": "2026-10-21T00:00:00Z",
                "metadata": {"agent_id": agent, "agent_version": "1", "tenant_id": "default"},
                "scores": [{"name": "eval_pass", "value": score}],
                "input": "not exposed",
                "output": "not exposed",
            },
        )

    monkeypatch.setattr(eval_gate, "_transport", httpx.MockTransport(handler))
    return f"/agents/{agent}/versions/1/evaluation", calls


def test_read_only_recomputation_does_not_publish_or_audit(c, runtime, monkeypatch):
    path, _ = setup(c, monkeypatch)
    before_rows, before_audit, calls = c.get("/agents").json(), c.get("/audit").json(), len(runtime.calls)
    response = c.get(path, params={"eval_run_ref": "langfuse:dataset/run"})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    result = response.json()
    assert result["status"] == "passed" and result["passed_items"] == result["total_items"] == 2
    assert result["pass_rate"] == 1 and result["stored_eval_run_ref"] is None
    assert result["policy"]["min_items"] == 2 and result["checked_at"].endswith("+00:00")
    assert "not_release_authorization" in result["scope"] and "not exposed" not in response.text
    assert c.get("/agents").json() == before_rows and c.get("/audit").json() == before_audit and len(runtime.calls) == calls


def test_failed_preview_and_publish_use_same_policy(c, runtime, monkeypatch):
    path, _ = setup(c, monkeypatch, "s11-eval-failed", score=0)
    params = {"eval_run_ref": "langfuse:dataset/run"}
    result = c.get(path, params=params).json()
    assert result["status"] == "failed" and result["passed_items"] == 0 and result["pass_rate"] == 0
    count = len(runtime.calls)
    rejected = c.post(path.removesuffix("evaluation") + "publish", json=params | {"eval_passed": True})
    assert rejected.status_code == 409 and result["reason"] in rejected.text and len(runtime.calls) == count


def test_flag_is_not_real_evaluation(c, monkeypatch):
    path, calls = setup(c, monkeypatch, "s11-eval-flag")
    monkeypatch.setattr(eval_gate, "MODE", "flag")
    result = c.get(path).json()
    assert result["status"] == "not_evaluated" and result["source"] == "development_flag"
    assert result["passed_items"] is result["total_items"] is result["pass_rate"] is None and calls == []


def test_access_is_checked_before_langfuse(c, monkeypatch):
    path, calls = setup(c, monkeypatch, "s11-eval-access")
    for headers, status in [
        ({"X-Console-Role": "USER"}, 403),
        ({"X-Console-Role": "ADMIN", "X-Console-User": "not-assigned"}, 403),
        ({"X-Console-Tenant": "foreign"}, 404),
    ]:
        assert c.get(path, headers=headers, params={"eval_run_ref": "langfuse:dataset/run"}).status_code == status
    assert c.get(path.replace("versions/1", "versions/missing")).status_code == 404
    assert c.get(path, params={"eval_run_ref": "x" * 129}).status_code == 422
    assert calls == []


@pytest.mark.parametrize(
    "metadata",
    [
        {"agent_id": "foreign-agent", "agent_version": "1", "tenant_id": "foreign"},
        {"agent_id": "s11-eval-mismatch", "agent_version": "1", "tenant_id": "foreign"},
    ],
)
def test_mismatched_source_has_no_counts_or_foreign_metadata(c, monkeypatch, metadata):
    agent = "s11-eval-mismatch" + ("-agent" if metadata["agent_id"] == "foreign-agent" else "")
    path, calls = setup(c, monkeypatch, agent, metadata=metadata)
    response = c.get(path, params={"eval_run_ref": "langfuse:dataset/run"})
    result = response.json()
    assert result["status"] == "failed" and result["total_items"] is None
    assert "foreign" not in response.text and len(calls) == 1


@pytest.mark.parametrize("status", [302, 503])
def test_unavailable_is_unknown_not_zero(c, monkeypatch, status):
    path, _ = setup(c, monkeypatch, f"s11-eval-unavailable-{status}", status=status)
    result = c.get(path, params={"eval_run_ref": "langfuse:dataset/run"}).json()
    assert result["status"] == "unavailable" and result["total_items"] is result["pass_rate"] is None


def test_saved_reference_default_and_preview_override_are_read_only(c, runtime, monkeypatch):
    path, _ = setup(c, monkeypatch, "s11-eval-saved")
    original = "langfuse:dataset/original"
    assert c.post(path.removesuffix("evaluation") + "publish", json={"eval_run_ref": original}).status_code == 200
    count = len(runtime.calls)
    saved = c.get(path).json()
    assert saved["eval_run_ref"] == saved["stored_eval_run_ref"] == original
    preview = c.get(path, params={"eval_run_ref": "langfuse:dataset/preview"}).json()
    assert preview["eval_run_ref"] == "langfuse:dataset/preview" and preview["stored_eval_run_ref"] == original
    assert c.get(path).json()["eval_run_ref"] == original and len(runtime.calls) == count


async def test_deadline_never_returns_partial_counts(monkeypatch):
    async def delayed(*args):
        await asyncio.sleep(1)

    monkeypatch.setattr(eval_gate, "_evaluate", delayed)
    monkeypatch.setattr(eval_gate, "EVALUATION_TIMEOUT_S", 0.001)
    result = await eval_gate.evaluate("a", "1", None, None, "default")
    assert result["status"] == "unavailable" and result["total_items"] is None
    assert await eval_gate.check("a", "1", None, None) == result["reason"]


@pytest.mark.parametrize("score,verdict", [(1, "passed"), (0, "not_passed"), (True, "not_passed")])
def test_sample_details_are_read_local_minimal_projection(c, monkeypatch, score, verdict):
    path, _ = setup(c, monkeypatch, "detail-" + str(score), score=score)
    response = c.get(path, params={"eval_run_ref": "langfuse:dataset/run"})
    rows = response.json()["sample_details"]
    assert rows == [
        {"sample_label": f"sample-{i}", "verdict": verdict, "reasons": [] if score == 1 and score is not True else ["score_not_passed"]}
        for i in (1, 2)
    ]
    assert "not exposed" not in response.text
    assert response.json()["total_items"] == len(rows)


@pytest.mark.parametrize("kind", ["foreign", "no-tenant", "missing-score", "time", "outage"])
def test_sample_diagnostics_do_not_disclose_source_identity(c, monkeypatch, kind):
    agent = "details-" + kind
    path, _ = setup(c, monkeypatch, agent)
    original = eval_gate._transport

    def handler(request):
        if "/datasets/" in request.url.path:
            response = original.handle_request(request)
            data = response.json()
            if kind == "no-tenant":
                data["metadata"].pop("tenant_id")
            return httpx.Response(200, json=data)
        if kind == "outage":
            return httpx.Response(503)
        return httpx.Response(200, json={
            "id": "PRIVATE-ID", "input": "PRIVATE-PROMPT", "output": "PRIVATE-OUTPUT",
            "metadata": {"tenant_id": "PRIVATE-TENANT" if kind == "foreign" else "default", "agent_id": agent, "agent_version": "1"},
            "timestamp": "2020-01-01T00:00:00Z" if kind == "time" else "2026-10-21T00:00:00Z",
            "scores": [] if kind == "missing-score" else [{"name": "eval_pass", "value": 1, "comment": "PRIVATE-COMMENT"}],
        })

    monkeypatch.setattr(eval_gate, "_transport", httpx.MockTransport(handler))
    response = c.get(path, params={"eval_run_ref": "langfuse:dataset/run"})
    assert "PRIVATE-" not in response.text
    rows = response.json()["sample_details"]
    if kind in {"outage", "no-tenant"}:
        assert rows is None
    else:
        expected = {"foreign": "evidence_not_accepted", "missing-score": "score_missing", "time": "time_window_not_verified"}[kind]
        assert all(row["verdict"] == "not_verifiable" and row["reasons"] == [expected] for row in rows)
        assert response.json()["passed_items"] == 0 and response.json()["total_items"] == 2


async def test_details_group_multiple_traces_and_missing_evidence():
    run = {"createdAt": "2026-10-21T00:01:00Z", "datasetRunItems": [
        {"datasetItemId": "PRIVATE-A", "traceId": "one"},
        {"datasetItemId": "PRIVATE-A", "traceId": "two"},
        {"datasetItemId": "PRIVATE-A", "traceId": "two"},
        {"datasetItemId": "PRIVATE-B"},
    ]}
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"timestamp": "2026-10-21T00:00:00Z", "metadata": {"tenant_id": "default"},
                                        "scores": [{"name": "eval_pass", "value": int(request.url.path.endswith("one"))}]})

    details = []
    async with httpx.AsyncClient(base_url="http://synthetic", transport=httpx.MockTransport(handler)) as client:
        assert await eval_gate.recompute(client, run, "synthetic", {"tenant_id": "default"}, details) == (0, 2)
    assert len(calls) == 2
    assert details == [
        {"sample_label": "sample-1", "verdict": "not_passed", "reasons": ["score_not_passed"]},
        {"sample_label": "sample-2", "verdict": "not_verifiable", "reasons": ["trace_missing"]},
    ]

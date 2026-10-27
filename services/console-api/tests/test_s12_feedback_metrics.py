from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from console_api import feedback_metrics
from console_api.feedback_summary import Rating, summarize

START = datetime(2026, 10, 18, tzinfo=UTC)
DONE = START + timedelta(hours=1)
HEADERS = {"X-Console-Tenant": "feedback-a", "X-Console-Role": "TENANT_ADMIN", "Authorization": "Bearer isolated-user"}
PARAMS = {"agent_id": "feedback-agent", "since": START.isoformat(), "until": (START + timedelta(days=1)).isoformat()}


def rating(identifier, value=1, at=None, trusted=True, run="r1", kind="NUMERIC"):
    return Rating(identifier, run, at or DONE + timedelta(hours=1), value, kind, trusted)


def test_latest_revision_per_run_and_duplicate_ids():
    rows = [
        rating("old", 1),
        rating("new", -1, DONE + timedelta(hours=2)),
        rating("new", -1, DONE + timedelta(hours=2)),
        rating("second-run", 1, run="r2"),
    ]
    result = summarize({"r1": DONE, "r2": DONE, "r3": DONE}, rows)
    assert result["positive_runs"] == 1 and result["negative_runs"] == 1
    assert result["satisfaction"] == 0.5 and result["feedback_coverage"] == 2 / 3


@pytest.mark.parametrize(
    "value,field",
    [
        (0, "non_binary_runs"),
        (0.5, "non_binary_runs"),
        (True, "invalid_score_runs"),
        ("1", "invalid_score_runs"),
        (float("nan"), "invalid_score_runs"),
        (10**1000, "invalid_score_runs"),
    ],
)
def test_latest_non_binary_or_invalid_never_falls_back(value, field):
    result = summarize({"r1": DONE}, [rating("old", 1), rating("latest", value, DONE + timedelta(hours=2))])
    assert result[field] == 1 and result["positive_runs"] == 0 and result["verified_feedback_runs"] == 0
    assert result["satisfaction"] is None


def test_conflicts_unverified_and_preterminal_are_explicit():
    for rows in [
        [rating("a", 1), rating("b", -1)],
        [rating("a", 1), rating("a", -1)],
        [Rating("no-time", "r1", None, 1, "NUMERIC", True)],
        [rating("naive", at=datetime(2026, 10, 18))],
    ]:
        result = summarize({"r1": DONE}, rows)
        assert result["conflicting_runs"] == 1 and result["feedback_coverage"] is None
    result = summarize({"r1": DONE}, [rating("legacy", trusted=False)])
    assert result["unverified_runs"] == 1 and result["satisfaction"] is None and result["feedback_coverage"] is None
    result = summarize({"r1": DONE}, [rating("before", at=START)])
    assert result["pre_terminal_runs"] == 1 and result["feedback_coverage"] == 0
    assert summarize({}, [rating("none")])["feedback_coverage"] is None
    assert summarize({"r1": DONE}, [])["feedback_coverage"] == 0


@pytest.fixture
def source(c, monkeypatch):
    monkeypatch.setenv("LANGFUSE_HOST", "http://127.0.0.1:3000")
    monkeypatch.setenv("RUNTIME_ADMIN_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "feedback-public")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "feedback-secret")
    monkeypatch.setenv("CONSOLE_ENV", "dev")
    assert c.post("/agents", headers=HEADERS, json={"agent_id": "feedback-agent", "version": "1"}).status_code == 201
    body = {
        "scope": "runtime_feedback_cohort_v1",
        "tenant_id": "feedback-a",
        "agent_id": "feedback-agent",
        "subject": "human:admin",
        "auth_method": "user",
        "since": PARAMS["since"],
        "until": PARAMS["until"],
        "complete_scan": True,
        "snapshot_guaranteed": False,
        "read_started_at": (DONE + timedelta(days=1)).isoformat(),
        "read_finished_at": (DONE + timedelta(days=1)).isoformat(),
        "counts": {"completed": 1, "failed": 0, "canceled": 0, "pending": 0},
        "runs": [
            {
                "run_id": "r1",
                "tenant_id": "feedback-a",
                "agent_id": "feedback-agent",
                "subject": "human:owner",
                "state": "completed",
                "created_at": START.isoformat(),
                "finished_at": DONE.isoformat(),
            }
        ],
    }
    score = {
        "id": "score1",
        "name": "user_feedback",
        "traceId": "trace1",
        "dataType": "NUMERIC",
        "value": 1,
        "updatedAt": (DONE + timedelta(hours=1)).isoformat(),
        "comment": "PRIVATE-COMMENT",
        "metadata": {"run_id": "r1", "subject": "human:owner", "verified": True, "correction": "PRIVATE-CORRECTION"},
    }
    trace = {
        "id": "trace1",
        "sessionId": "r1",
        "metadata": {
            "attributes": {"ai.tenant_id": "feedback-a", "ai.agent_id": "feedback-agent", "ai.run_id": "r1", "ai.subject": "human:owner"}
        },
    }
    state = {"cohort": body, "scores": [score], "trace": trace, "runtime_status": 200, "mode": "ok"}
    calls = []

    def runtime(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path == "/v1/metrics/feedback-cohort"
        assert request.headers["authorization"] == HEADERS["Authorization"]
        assert "x-ai-subject" not in request.headers and "x-ai-tenant" not in request.headers
        return httpx.Response(state["runtime_status"], json=state["cohort"])

    def langfuse(request):
        calls.append(request)
        assert request.method == "GET" and request.headers["authorization"].startswith("Basic ")
        assert HEADERS["Authorization"] not in str(request.headers)
        if state["mode"] == "outage":
            return httpx.Response(500, text="SECRET-SOURCE-DIAGNOSTIC")
        if state["mode"] == "redirect":
            return httpx.Response(302, headers={"Location": "https://external.invalid/secret"})
        if state["mode"] == "oversized":
            return httpx.Response(200, json={"data": "x" * (2 * 1024 * 1024)})
        if request.url.path == "/api/public/v2/scores":
            page, limit = int(request.url.params["page"]), int(request.url.params["limit"])
            rows = state["scores"]
            total = len(rows)
            result = {
                "data": rows[(page - 1) * limit : page * limit],
                "meta": {"page": page, "limit": limit, "totalItems": total, "totalPages": (total + limit - 1) // limit},
            }
            if state["mode"] == "overflow":
                result["meta"].update(totalItems=2001, totalPages=21)
            if state["mode"] == "drift" and page == 2:
                result["meta"]["totalItems"] += 1
            if state["mode"] == "repeated_page" and page == 2:
                result["data"] = rows[:limit]
            return httpx.Response(200, json=result)
        if request.url.path == "/api/public/traces/foreign-trace":
            return httpx.Response(
                200,
                json={
                    "id": "foreign-trace",
                    "sessionId": "foreign-run",
                    "metadata": {
                        "attributes": {
                            "ai.tenant_id": "foreign",
                            "ai.agent_id": "foreign",
                            "ai.run_id": "foreign-run",
                            "ai.subject": "foreign",
                        }
                    },
                },
            )
        assert request.url.path in ("/api/public/traces/trace1", "/api/public/traces/trace2")
        return httpx.Response(200, json=state["trace"] | {"id": request.url.path.rsplit("/", 1)[-1]})

    monkeypatch.setattr(feedback_metrics, "_runtime_transport", httpx.MockTransport(runtime))
    monkeypatch.setattr(feedback_metrics, "_langfuse_transport", httpx.MockTransport(langfuse))
    return state, calls


def test_readonly_legacy_remains_unverified_and_does_not_leak(c, source):
    state, calls = source
    state["scores"].append(deepcopy(state["scores"][0]))
    response = c.get("/metrics/feedback", headers=HEADERS, params=PARAMS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["feedback"]["unverified_runs"] == 1 and body["feedback"]["verified_feedback_runs"] == 0
    assert body["feedback"]["satisfaction"] is None and body["feedback"]["feedback_coverage"] is None
    assert body["subject"] == "human:admin" and body["snapshot_guaranteed"] is False
    assert all(text not in response.text for text in ("PRIVATE-", "feedback-secret", "isolated-user", "score1", "human:owner"))
    assert response.headers["cache-control"] == "no-store"
    assert len(calls) == 3
    state["scores"] = []
    assert c.get("/metrics/feedback", headers=HEADERS, params=PARAMS).json()["feedback"]["feedback_coverage"] == 0
    state["cohort"]["runs"] = []
    state["cohort"]["counts"]["completed"] = 0
    assert c.get("/metrics/feedback", headers=HEADERS, params=PARAMS).json()["feedback"]["feedback_coverage"] is None


@pytest.mark.parametrize("dimension", ["tenant_id", "agent_id", "run_id", "subject"])
def test_trace_attribute_conflict_fails_closed(c, source, dimension):
    state, _ = source
    state["trace"]["metadata"]["attributes"]["ai." + dimension] = "foreign"
    response = c.get("/metrics/feedback", headers=HEADERS, params=PARAMS)
    assert response.status_code == 503 and "feedback_coverage" not in response.text


@pytest.mark.parametrize(
    "mode",
    [
        "outage",
        "overflow",
        "drift",
        "same_id_conflict",
        "missing_run",
        "wrong_subject",
        "bad_trace_path",
        "redirect",
        "oversized",
        "repeated_page",
    ],
)
def test_source_incomplete_fails_closed(c, source, mode):
    state, _ = source
    state["mode"] = mode
    if mode == "drift":
        state["scores"] = [dict(state["scores"][0], id=f"score-{index}") for index in range(101)]
    if mode == "repeated_page":
        state["scores"] = [dict(state["scores"][0], id=f"score-{index}") for index in range(200)]
    if mode == "same_id_conflict":
        state["scores"].append(dict(state["scores"][0], value=-1))
    if mode == "missing_run":
        state["scores"][0]["metadata"].pop("run_id")
    if mode == "wrong_subject":
        state["scores"][0]["metadata"]["subject"] = "human:not-owner"
    if mode == "bad_trace_path":
        state["scores"][0]["traceId"] = ".."
    response = c.get("/metrics/feedback", headers=HEADERS, params=PARAMS)
    assert response.status_code == 503 and "SECRET-SOURCE" not in response.text and "feedback_coverage" not in response.text


def test_pagination_and_noncohort_records_do_not_leak(c, source):
    state, calls = source
    original = state["scores"][0]
    state["scores"] = [dict(original, id=f"score-{index}") for index in range(101)]
    state["scores"].append(dict(original, id="other", metadata={"run_id": "foreign-run"}, traceId="foreign-trace"))
    response = c.get("/metrics/feedback", headers=HEADERS, params=PARAMS)
    assert response.status_code == 200 and response.json()["feedback"]["unverified_runs"] == 1
    assert response.json()["feedback"]["conflicting_runs"] == 1
    assert any("foreign-trace" in str(request.url) for request in calls)
    assert "foreign" not in response.text


def test_multiple_traces_latest_nonbinary_missing_time_and_preterminal(c, source):
    state, _ = source
    original = deepcopy(state["scores"][0])
    state["scores"].append(dict(original, id="revised", traceId="trace2", value=0, updatedAt=(DONE + timedelta(hours=2)).isoformat()))
    body = c.get("/metrics/feedback", headers=HEADERS, params=PARAMS).json()["feedback"]
    assert body["unverified_runs"] == 1 and body["non_binary_runs"] == 1 and body["positive_runs"] == 0
    state["scores"] = [dict(original, updatedAt="not-a-time")]
    assert c.get("/metrics/feedback", headers=HEADERS, params=PARAMS).json()["feedback"]["conflicting_runs"] == 1
    state["scores"] = [dict(original, updatedAt=START.isoformat())]
    assert c.get("/metrics/feedback", headers=HEADERS, params=PARAMS).json()["feedback"]["pre_terminal_runs"] == 1


@pytest.mark.parametrize(
    "change", ["range", "duplicate_run", "count", "boolean_count", "finished", "scope", "auth_method", "scan", "state"]
)
def test_untrusted_cohort_response_is_rejected(c, source, change):
    state, calls = source
    body = state["cohort"]
    if change == "range":
        body["until"] = (START + timedelta(days=2)).isoformat()
    if change == "duplicate_run":
        body["runs"].append(deepcopy(body["runs"][0]))
    if change == "count":
        body["counts"]["completed"] = 2
    if change == "boolean_count":
        body["counts"]["completed"] = True
    if change == "finished":
        body["runs"][0]["finished_at"] = (START - timedelta(days=1)).isoformat()
    if change == "scope":
        body["scope"] = "other"
    if change == "auth_method":
        body["auth_method"] = "dev"
    if change == "scan":
        body["complete_scan"] = False
    if change == "state":
        body["runs"][0]["state"] = "invented"
    assert c.get("/metrics/feedback", headers=HEADERS, params=PARAMS).status_code == 503
    assert len(calls) == 1


def test_scope_auth_and_configuration_guards(c, source, monkeypatch):
    state, calls = source
    assert (
        c.get(
            "/metrics/feedback", headers={key: value for key, value in HEADERS.items() if key != "Authorization"}, params=PARAMS
        ).status_code
        == 401
    )
    assert c.get("/metrics/feedback", headers=HEADERS | {"X-Console-Role": "USER"}, params=PARAMS).status_code == 403
    assert not calls
    for status in (401, 403):
        state["runtime_status"] = status
        assert c.get("/metrics/feedback", headers=HEADERS, params=PARAMS).status_code == status
        assert c.get("/metrics/feedback", headers=HEADERS, params=PARAMS | {"agent_id": "nonexistent"}).status_code == status
    state["runtime_status"] = 200
    state["cohort"]["tenant_id"] = "foreign"
    assert c.get("/metrics/feedback", headers=HEADERS, params=PARAMS).status_code == 503
    state["cohort"]["tenant_id"] = "feedback-a"
    for name, value in [
        ("RUNTIME_ADMIN_URL", "https://external.invalid"),
        ("LANGFUSE_HOST", "http://localhost:3000/redirect"),
        ("CONSOLE_ENV", "prod"),
    ]:
        with monkeypatch.context() as patch:
            patch.setenv(name, value)
            assert c.get("/metrics/feedback", headers=HEADERS, params=PARAMS).status_code == 503


def test_score_self_report_cannot_hide_cohort_trace(c, source):
    state, _ = source
    state["scores"][0]["metadata"]["run_id"] = "foreign-run"
    response = c.get("/metrics/feedback", headers=HEADERS, params=PARAMS)
    assert response.status_code == 503 and "feedback_coverage" not in response.text


def test_v2_legacy_isolation_and_runtime_evidence_drift(c, source, monkeypatch):
    state, calls = source
    state["cohort"].update(scope="runtime_feedback_evidence_v2", proofs=[])
    count = 0

    def runtime(request):
        nonlocal count
        count += 1
        assert request.method == "GET" and request.url.path == "/v1/metrics/feedback-evidence"
        assert request.headers["authorization"] == HEADERS["Authorization"]
        return httpx.Response(200, json=state["cohort"])

    monkeypatch.setattr(feedback_metrics, "_runtime_transport", httpx.MockTransport(runtime))
    response = c.get("/metrics/feedback-v2", headers=HEADERS, params=PARAMS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert count == 2 and body["scope"] == "local_feedback_aggregate_v2"
    assert body["feedback"]["unverified_runs"] == 1 and body["feedback"]["satisfaction"] is None
    assert "PRIVATE" not in response.text and "proofs" not in body
    assert all(request.method == "GET" for request in calls)

    def drift(request):
        nonlocal count
        count += 1
        changed = deepcopy(state["cohort"])
        if count % 2 == 0:
            changed["proofs"] = [{"changed": True}]
        return httpx.Response(200, json=changed)

    monkeypatch.setattr(feedback_metrics, "_runtime_transport", httpx.MockTransport(drift))
    assert c.get("/metrics/feedback-v2", headers=HEADERS, params=PARAMS).status_code == 503

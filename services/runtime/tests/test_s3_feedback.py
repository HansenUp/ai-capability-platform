"""S3.4 / S3.5：门户 Agent 列表（/v1/agents）与反馈（/v1/runs/{run_id}/feedback → 观测适配器）。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from runtime.api import messages

ALICE = {"X-AI-Subject": "human:alice"}


@pytest.fixture
def adapter() -> Iterator[list[dict[str, Any]]]:
    """替身观测适配器：记录收到的反馈并回 forwarded=true。"""
    received: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append({"url": str(request.url), "body": json.loads(request.content)})
        return httpx.Response(200, json={"forwarded": True, "status": 200})

    messages.set_observe_transport_for_tests(httpx.MockTransport(handler))
    yield received
    messages.set_observe_transport_for_tests(None)


def _run(client: Any, headers: dict[str, str]) -> str:
    body = {"agent_id": "demo", "messages": [{"role": "user", "content": "hi"}], "stream": False}
    return str(client.post("/v1/messages", json=body, headers=headers).json()["run_id"])


def test_list_my_agents(client: Any) -> None:
    agents = {a["agent_id"]: a for a in client.get("/v1/agents", headers=ALICE).json()}
    assert agents["demo"]["version"] == "1" and agents["demo"]["rollout"] == "stable"
    assert client.get("/v1/agents", headers=ALICE | {"X-AI-Tenant": "nobody"}).json() == []


def test_feedback_is_forwarded_with_server_side_trace_id(client: Any, adapter: list[dict[str, Any]]) -> None:
    run_id = _run(client, ALICE)
    r = client.post(f"/v1/runs/{run_id}/feedback", json={"score": -1, "correction": "正确答案"}, headers=ALICE)
    assert r.status_code == 200, r.text
    sent = adapter[-1]
    assert sent["url"].endswith("/feedback")
    assert sent["body"] | {} == {
        "trace_id": r.json()["trace_id"],
        "run_id": run_id,
        "subject": "human:alice",
        "score": -1,
        "comment": "",
        "correction": "正确答案",
    }
    assert r.json()["trace_id"]  # trace_id 取自 t_run，调用方无从指定


def test_feedback_guards(client: Any, adapter: list[dict[str, Any]]) -> None:
    run_id = _run(client, ALICE)
    assert client.post(f"/v1/runs/{run_id}/feedback", json={"score": 1}, headers={"X-AI-Subject": "human:bob"}).status_code == 403
    assert client.post(f"/v1/runs/{run_id}/feedback", json={"score": 2}, headers=ALICE).status_code == 422
    assert client.post("/v1/runs/run-missing/feedback", json={"score": 1}, headers=ALICE).status_code == 404
    assert adapter == []


def test_feedback_adapter_down_is_502(client: Any) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    messages.set_observe_transport_for_tests(httpx.MockTransport(down))
    try:
        run_id = _run(client, ALICE)
        assert client.post(f"/v1/runs/{run_id}/feedback", json={"score": 1}, headers=ALICE).status_code == 502
    finally:
        messages.set_observe_transport_for_tests(None)

"""PlatformClient 的最小冒烟测试：验证构造函数正确设置认证与主体请求头，以及用生成类型解析 Run 事件日志。"""

import httpx
import pytest

from ai_platform_sdk import PlatformClient
from ai_platform_sdk.types_runtime import RunEvent, RunEventType


@pytest.mark.asyncio
async def test_client_constructs_headers():
    """token/subject 应分别映射到 Authorization / X-AI-Subject 请求头（httpx 会将头名归一化为小写）。"""
    c = PlatformClient("http://localhost:8080", token="t", subject="human:1")  # noqa: S106
    assert c._c.headers["authorization"] == "Bearer t" and c._c.headers["x-ai-subject"] == "human:1"
    await c.aclose()


@pytest.mark.asyncio
async def test_run_events_parsed_with_generated_types():
    """run_events 用 OpenAPI 生成的 RunEvent 解析响应；ts 取 Runtime 实际输出的无时区 ISO 格式。"""
    rows = [
        {
            "run_id": "r1",
            "seq": 3,
            "type": "run.state_changed",
            "payload": {"from": "working", "to": "completed"},
            "ts": "2026-10-16T13:40:08.136711",
        },
        {"run_id": "r1", "seq": 4, "type": "run.done", "payload": {}, "idempotency_key": None, "ts": "2026-10-16T13:40:08.2+00:00"},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/runs/r1/events" and req.url.params["since_seq"] == "2"
        return httpx.Response(200, json=rows)

    c = PlatformClient("http://rt", transport=httpx.MockTransport(handler))
    events = await c.run_events("r1", since_seq=2)
    await c.aclose()
    assert all(isinstance(e, RunEvent) for e in events)
    assert [e.seq for e in events] == [3, 4] and events[1].type is RunEventType.run_done
    assert events[0].payload == {"from": "working", "to": "completed"}


@pytest.mark.asyncio
async def test_approve_sends_params_hash_from_card():
    """approve 必须回传审批卡片上的 params_hash（Runtime 据此校验批的是哪一组参数）。"""
    import json

    from ai_contract import CardEvent

    card = CardEvent(card_type="impact_preview", payload={"run_id": "r1", "call_id": "c1", "params_hash": "abcd1234abcd1234"})
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "POST" and req.url.path == "/v1/runs/r1/approve"
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"run_id": "r1", "state": "working", "resume": "/v1/runs/r1/resume"})

    c = PlatformClient("http://rt", transport=httpx.MockTransport(handler))
    p = card.payload
    out = await c.approve(p["run_id"], p["call_id"], p["params_hash"], comment="ok")
    await c.aclose()
    assert out["state"] == "working"
    assert seen == [{"call_id": "c1", "params_hash": "abcd1234abcd1234", "comment": "ok", "ttl_seconds": 900}]

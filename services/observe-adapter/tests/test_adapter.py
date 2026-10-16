"""观测适配器测试：未配置 Langfuse 时反馈与用量进入本地缓冲。"""

from fastapi.testclient import TestClient

from observe_adapter.main import BUFFER, app

c = TestClient(app)


def test_feedback_and_usage_buffered_without_langfuse():
    """反馈返回 forwarded=False，用量 200，缓冲首条为 user_feedback 评分。"""
    assert c.post("/feedback", json={"trace_id": "t1", "subject": "human:1", "score": 1}).json()["forwarded"] is False
    assert c.post("/observe/usage", json={"agent_id": "a", "model": "m", "input_tokens": 1, "output_tokens": 2}).status_code == 200
    assert BUFFER[0]["body"]["name"] == "user_feedback"

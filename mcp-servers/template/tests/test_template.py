"""验证模板的 dry-run 预演、幂等去重与身份校验行为。"""

from fastapi.testclient import TestClient

from mcp_template.main import app

c = TestClient(app)
H = {"X-AI-Dev-Sub": "human:alice"}  # 开发环境身份头


def test_dry_run_then_idempotent_execute():
    """先 dry_run 得到影响预演，再实际执行；同一幂等键重复调用应被去重。"""
    body = {"ids": ["T-1", "T-2"], "idempotency_key": "agent:demo:call1:abcd"}
    prev = c.post("/tools/ticket.close_batch", json=body | {"dry_run": True}, headers=H).json()
    assert prev["impact_preview"]["tickets"] == 2
    first = c.post("/tools/ticket.close_batch", json=body, headers=H).json()
    second = c.post("/tools/ticket.close_batch", json=body, headers=H).json()
    assert first["closed"] == ["T-1", "T-2"] and second.get("deduplicated") is True
    # 幂等键长度不足 8 位应被 Pydantic 校验拒绝（422）
    assert c.post("/tools/ticket.close_batch", json={"ids": [], "idempotency_key": "short"}, headers=H).status_code == 422


def test_requires_identity():
    """未携带任何身份信息（Authorization / X-AI-Dev-Sub）时应返回 401。"""
    assert c.post("/tools/ticket.search?q=x").status_code == 401

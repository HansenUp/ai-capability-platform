"""pytest 共享夹具：为 runtime 服务的集成测试提供隔离的 SQLite 数据库、独立工作区目录，
以及一个已发布的示例 Agent（demo）。"""

import os
import tempfile

import pytest

# Isolated SQLite DB and workspace per test session; must be set before runtime.config is imported.
# （每个测试会话使用独立的 SQLite 数据库与工作区目录；必须在 runtime.config 被导入之前设置好
# 这些环境变量，因为 Settings 的默认值是在模块导入时通过 field(default_factory=...) 读取的。）
_tmp = tempfile.mkdtemp(prefix="runtime-test-")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp}/runtime.db"
os.environ["WORKSPACE_DIR"] = f"{_tmp}/ws"
os.environ["HARNESS"] = "mock"
os.environ["POLICY_URL"] = "http://127.0.0.1:1"  # unreachable → gate uses baseline fallback（不可达，闸口回退到基线策略）
os.environ["CAPABILITY_GATEWAY_URL"] = "http://127.0.0.1:1"
os.environ["TOOL_RESULT_OFFLOAD_TOKENS"] = "20"  # 调小阈值，便于测试触发工具结果外置逻辑


@pytest.fixture(scope="session")
def client():
    """会话级 FastAPI TestClient 夹具，延迟导入 app 以确保上面的环境变量先生效。"""
    from fastapi.testclient import TestClient

    from runtime.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session", autouse=True)
def demo_agent(client):
    """自动创建并发布一个名为 demo 的示例 Agent，供本会话内所有测试用例复用。"""
    r = client.post(
        "/admin/agents",
        json={
            "agent_id": "demo",
            "version": "1",
            "system_prompt": "你是工单助手。",
            # mock harness 由平台持有循环，capability_tier 为 L2，因此允许声明高风险写工具。
            # 工具必须在此显式声明：运行时只暴露组装结果中的工具，未声明的工具不会进入执行期工具表。
            "config": {"tools": ["echo", "ticket.search", "ticket.close_batch"], "skills": [{"name": "ticket-summary"}]},
        },
    )
    assert r.status_code == 201, r.text
    assert client.post("/admin/agents/demo/versions/1/publish", json={"eval_passed": True}).status_code == 200
    return "demo"

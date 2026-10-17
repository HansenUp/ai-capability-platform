"""pytest 共享夹具：为 runtime 服务的集成测试提供隔离的数据库（默认临时 SQLite；设置
RUNTIME_TEST_DATABASE_URL 时改用该库，如 make test-mysql 的 MySQL 测试库）、独立工作区目录，
以及一个已发布的示例 Agent（demo）。"""

import asyncio
import os
import re
import tempfile

import pytest
from sqlalchemy import MetaData, make_url, text
from sqlalchemy.ext.asyncio import create_async_engine


async def _reset_external_db(url: str) -> None:
    """准备外部测试库：MySQL 下先用不带库名的连接执行 CREATE DATABASE IF NOT EXISTS，再反射并删除库内
    全部表（含 alembic_version），保证随后 lifespan 中的 alembic upgrade head 从空库开始。
    为防误删业务数据，库名必须包含 "test"。"""
    u = make_url(url)
    db_name = u.database or ""
    if "test" not in db_name or not re.fullmatch(r"[A-Za-z0-9_]+", db_name):
        raise RuntimeError(f"RUNTIME_TEST_DATABASE_URL 的库名必须只含字母数字下划线且包含 'test'，当前为 {db_name!r}")
    if u.get_backend_name() == "mysql":
        server = create_async_engine(u._replace(database=None))  # URL.set(database=None) 不会清空库名
        try:
            async with server.begin() as conn:
                await conn.execute(text(f"CREATE DATABASE IF NOT EXISTS `{db_name}` CHARACTER SET utf8mb4"))
        finally:
            await server.dispose()
    eng = create_async_engine(u)
    try:
        async with eng.begin() as conn:
            meta = MetaData()
            await conn.run_sync(meta.reflect)
            await conn.run_sync(meta.drop_all)
    finally:
        await eng.dispose()


# Isolated SQLite DB and workspace per test session; must be set before runtime.config is imported.
# （每个测试会话使用独立的 SQLite 数据库与工作区目录；必须在 runtime.config 被导入之前设置好
# 这些环境变量，因为 Settings 的默认值是在模块导入时通过 field(default_factory=...) 读取的。）
_tmp = tempfile.mkdtemp(prefix="runtime-test-")
_external_db = os.environ.get("RUNTIME_TEST_DATABASE_URL")
if _external_db:
    # 外部测试库：会话开始前清空（此时尚无事件循环，可直接 asyncio.run），schema 由 lifespan 中的迁移重建
    asyncio.run(_reset_external_db(_external_db))
    os.environ["DATABASE_URL"] = _external_db
else:
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp}/runtime.db"
os.environ["WORKSPACE_DIR"] = f"{_tmp}/ws"
os.environ["HARNESS"] = "mock"
os.environ["POLICY_URL"] = "http://127.0.0.1:1"  # unreachable → gate uses baseline fallback（不可达，闸口回退到基线策略）
os.environ["CAPABILITY_GATEWAY_URL"] = "http://127.0.0.1:1"
os.environ["TOOL_RESULT_OFFLOAD_TOKENS"] = "20"
os.environ["CONFIDENTIAL_MODEL_ALIASES"] = (
    "default-chat"  # 测试默认允许 demo 的别名接收机密数据；阻断场景在用例中单独覆盖  # 调小阈值，便于测试触发工具结果外置逻辑
)

if _external_db:
    # 以下导入会触发 runtime.config 读取环境变量，必须放在全部 os.environ 设置之后。
    # asyncmy 连接绑定创建它的事件循环；测试里多个 TestClient / async 用例各有各的循环，连接池复用跨循环连接
    # 会报 "attached to a different loop"。测试期改用 NullPool（每次借用新建连接），只影响测试进程。
    from sqlalchemy.pool import NullPool

    from runtime.state import db as _db

    _db.engine = create_async_engine(_db.normalize_database_url(_external_db), poolclass=NullPool)
    _db.SessionLocal.configure(bind=_db.engine)


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

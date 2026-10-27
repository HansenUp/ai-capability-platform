"""Console 测试夹具：隔离的 Console 库（默认临时 SQLite；设置 CONSOLE_TEST_DATABASE_URL 时用该库，如 make test-mysql），
以及替身 Runtime（httpx.MockTransport，记录 Console 推送给 Runtime 的每个请求）。"""

import asyncio
import os
import re
import tempfile

import httpx
import pytest
from sqlalchemy import MetaData, make_url, text
from sqlalchemy.ext.asyncio import create_async_engine


async def _reset_external_db(url: str) -> None:
    """准备外部测试库：库名必须含 test；MySQL 下先建库，再删掉库内的 Console 表与其 Alembic 版本表。"""
    u = make_url(url)
    name = u.database or ""
    if "test" not in name or not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise RuntimeError(f"CONSOLE_TEST_DATABASE_URL 的库名必须只含字母数字下划线且包含 'test'，当前为 {name!r}")
    if u.get_backend_name() == "mysql":
        server = create_async_engine(u._replace(database=None))
        try:
            async with server.begin() as conn:
                await conn.execute(text(f"CREATE DATABASE IF NOT EXISTS `{name}` CHARACTER SET utf8mb4"))
        finally:
            await server.dispose()
    eng = create_async_engine(u)
    try:
        async with eng.begin() as conn:
            meta = MetaData()
            await conn.run_sync(meta.reflect)
            ours = [t for n, t in meta.tables.items() if n.startswith("t_console_") or n == "alembic_version_console"]
            await conn.run_sync(lambda c: meta.drop_all(c, tables=ours))
    finally:
        await eng.dispose()


_external = os.environ.get("CONSOLE_TEST_DATABASE_URL")
if _external:
    asyncio.run(_reset_external_db(_external))
    os.environ["CONSOLE_DATABASE_URL"] = _external
else:
    os.environ["CONSOLE_DATABASE_URL"] = f"sqlite+aiosqlite:///{tempfile.mkdtemp(prefix='console-test-')}/console.db"
os.environ.pop("CONSOLE_CLIENT_SECRET", None)  # 测试走开发态身份头，不去取令牌
os.environ.pop("CONSOLE_NODE_JWKS_URL", None)
os.environ["CONSOLE_NODE_TOKEN"] = "test-node-token"  # noqa: S105 —— 测试用节点静态令牌（S9.8）

if _external:
    # 异步驱动的连接绑定创建它的事件循环；TestClient 与 async 用例各有循环，测试期改用 NullPool
    from sqlalchemy.pool import NullPool

    from console_api import db

    db.engine = create_async_engine(db.normalize_database_url(_external), poolclass=NullPool)
    db.SessionLocal.configure(bind=db.engine)


class FakeRuntime:
    """替身 Runtime：记录请求；fail_with 非空时对所有请求返回该状态码。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.params: list[dict] = []  # 每个请求的查询参数
        self.fail_with: int | None = None
        self.fail_paths: dict[tuple[str, str], int] = {}  # (方法, 路径) → 只对该请求返回的错误码

    def handler(self, request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, request.url.path, body))
        self.params.append(dict(request.url.params))
        if (request.method, request.url.path) in self.fail_paths:
            return httpx.Response(self.fail_paths[(request.method, request.url.path)], json={"detail": "boom"})
        if self.fail_with:
            return httpx.Response(self.fail_with, json={"detail": "boom"})
        return httpx.Response(200, json={"ok": True})


@pytest.fixture(scope="session")
def runtime():
    from console_api import runtime_client

    fake = FakeRuntime()
    runtime_client.set_transport_for_tests(httpx.MockTransport(fake.handler))
    yield fake
    runtime_client.set_transport_for_tests(None)


@pytest.fixture(scope="session")
def c(runtime):
    from fastapi.testclient import TestClient

    from console_api.main import app

    with TestClient(app) as client:
        yield client

"""Alembic 迁移测试：在独立的临时 SQLite 上验证 upgrade head 后的表结构与 ORM 一致、downgrade base
能完整撤销、upgrade head 可重复执行；设置 RUNTIME_TEST_DATABASE_URL 时，另外校验外部测试库
（由会话 lifespan 迁移建出）的表结构与 ORM 一致。"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, Engine, create_engine, inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from runtime.state.db import Base, alembic_config, normalize_database_url

TABLES = {"t_agent_version", "t_run", "t_run_event", "t_audit"}


def _migrate(conn: Connection, action: str, target: str) -> None:
    """在给定连接上执行 alembic upgrade/downgrade（经 attributes["connection"] 共享连接）。"""
    cfg = alembic_config()
    cfg.attributes["connection"] = conn
    getattr(command, action)(cfg, target)


def _schema_diff(conn: Connection) -> list[object]:
    """比较实际库表结构与 ORM 元数据（含类型比较），返回差异列表；空列表表示完全一致。"""
    ctx = MigrationContext.configure(conn, opts={"compare_type": True})
    diff: list[object] = compare_metadata(ctx, Base.metadata)
    return diff


@pytest.fixture()
def sqlite_engine(tmp_path: Path) -> Iterator[Engine]:
    """每个用例一个全新的临时 SQLite 文件库（同步引擎即可，迁移本身是同步执行的）。"""
    eng = create_engine(f"sqlite:///{tmp_path / 'migrate.db'}")
    yield eng
    eng.dispose()


def test_upgrade_head_matches_orm(sqlite_engine: Engine) -> None:
    """upgrade head 后四张表齐全、关键列存在，且与 ORM 元数据无任何结构差异。"""
    with sqlite_engine.begin() as conn:
        _migrate(conn, "upgrade", "head")
    with sqlite_engine.connect() as conn:
        insp = inspect(conn)
        assert TABLES | {"alembic_version"} <= set(insp.get_table_names())
        assert {c["name"] for c in insp.get_columns("t_run_event")} >= {"run_id", "seq", "type", "payload", "idempotency_key", "ts"}
        assert insp.get_pk_constraint("t_run_event")["constrained_columns"] == ["run_id", "seq"]
        assert {u["name"] for u in insp.get_unique_constraints("t_agent_version")} == {"uq_agent_version"}
        assert _schema_diff(conn) == []


def test_downgrade_base_drops_all_tables(sqlite_engine: Engine) -> None:
    """downgrade base 后四张业务表全部消失。"""
    with sqlite_engine.begin() as conn:
        _migrate(conn, "upgrade", "head")
        _migrate(conn, "downgrade", "base")
    with sqlite_engine.connect() as conn:
        assert not TABLES & set(inspect(conn).get_table_names())


def test_upgrade_head_is_idempotent(sqlite_engine: Engine) -> None:
    """重复执行 upgrade head 不报错，且结构依旧与 ORM 一致（模拟节点多次重启）。"""
    for _ in range(2):
        with sqlite_engine.begin() as conn:
            _migrate(conn, "upgrade", "head")
    with sqlite_engine.connect() as conn:
        assert _schema_diff(conn) == []


def test_approval_migration_preserves_existing_credential_lock(sqlite_engine: Engine) -> None:
    with sqlite_engine.begin() as conn:
        _migrate(conn, "upgrade", "0007")
        conn.execute(text("INSERT INTO t_workbench_scope (scope_id, active_attempt, revision) VALUES ('scope', 'legacy', 1)"))
        conn.execute(
            text(
                "INSERT INTO t_workbench_attempt "
                "(attempt_id, scope_id, run_id, request_seq, fingerprint, status, revision, acknowledged, created_at) "
                "VALUES ('legacy', 'scope', 'run', 1, 'fingerprint', 'PREPARED', 1, 0, CURRENT_TIMESTAMP)"
            )
        )
        _migrate(conn, "upgrade", "head")
    with sqlite_engine.connect() as conn:
        assert conn.execute(text("SELECT action, call_id, params_hash FROM t_workbench_attempt WHERE attempt_id='legacy'")).one() == (
            "credential",
            None,
            None,
        )
        assert conn.execute(text("SELECT active_attempt FROM t_workbench_scope WHERE scope_id='scope'")).scalar_one() == "legacy"
        assert _schema_diff(conn) == []
    with sqlite_engine.begin() as conn:
        _migrate(conn, "downgrade", "0007")
        assert conn.execute(text("SELECT status FROM t_workbench_attempt WHERE attempt_id='legacy'")).scalar_one() == "PREPARED"
        _migrate(conn, "upgrade", "head")


def test_create_migration_preserves_existing_answer_binding(sqlite_engine: Engine) -> None:
    with sqlite_engine.begin() as conn:
        _migrate(conn, "upgrade", "0009")
        conn.execute(text("INSERT INTO t_workbench_scope (scope_id, active_attempt, revision) VALUES ('scope', 'answer', 1)"))
        conn.execute(
            text(
                "INSERT INTO t_workbench_attempt "
                "(attempt_id, scope_id, run_id, action, call_id, value_hmac, "
                "request_seq, fingerprint, status, revision, acknowledged, created_at) "
                "VALUES ('answer', 'scope', 'run', 'answer', 'call', 'binding', 2, 'fingerprint', 'PREPARED', 1, 0, CURRENT_TIMESTAMP)"
            )
        )
        _migrate(conn, "upgrade", "head")
        assert conn.execute(text("SELECT action, call_id, value_hmac, create_agent FROM t_workbench_attempt")).one() == (
            "answer",
            "call",
            "binding",
            None,
        )
        assert conn.execute(text("SELECT active_attempt FROM t_workbench_scope")).scalar_one() == "answer"
        assert _schema_diff(conn) == []
        _migrate(conn, "downgrade", "0009")
        assert conn.execute(text("SELECT value_hmac FROM t_workbench_attempt")).scalar_one() == "binding"
        _migrate(conn, "upgrade", "head")


@pytest.mark.skipif(not os.environ.get("RUNTIME_TEST_DATABASE_URL"), reason="未设置 RUNTIME_TEST_DATABASE_URL")
async def test_external_db_schema_matches_orm(client: object) -> None:
    """外部测试库（如 MySQL）经 lifespan 迁移后，表结构与 ORM 元数据无差异（依赖 client 夹具确保迁移已执行）。"""
    eng = create_async_engine(normalize_database_url(os.environ["RUNTIME_TEST_DATABASE_URL"]))
    try:
        async with eng.connect() as conn:
            assert await conn.run_sync(_schema_diff) == []
    finally:
        await eng.dispose()

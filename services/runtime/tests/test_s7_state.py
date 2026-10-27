"""S7 WP-A 状态底座：事件日志 seq CAS 与 fencing、transition CAS、租约原语、写工具操作登记、迁移 0004
与并发迁移锁。默认跑在会话 SQLite 上；设置 RUNTIME_TEST_DATABASE_URL（make test-mysql）时同一批用例跑在
MySQL 上，并额外覆盖 GET_LOCK 路径。"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, make_url, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from ai_contract import RunEventType, RunState
from runtime.state import db
from runtime.state.db import Run, ToolOperation, alembic_config
from runtime.state.eventlog import (
    EventLog,
    LeaseLost,
    OperationExists,
    RunNotFound,
    SeqConflict,
    StaleWriter,
    acquire_lease,
    current_fence,
    expired_unleased_runs,
    fence_scope,
    lease_info,
    release_lease,
    renew_lease,
)

_EXTERNAL = os.environ.get("RUNTIME_TEST_DATABASE_URL", "")
_IS_MYSQL = bool(_EXTERNAL) and make_url(_EXTERNAL).get_backend_name() == "mysql"


async def _new_run(*, durable: bool = False, state: RunState = RunState.SUBMITTED) -> str:
    rid = f"s7-{uuid4().hex[:16]}"
    async with db.SessionLocal() as s:
        s.add(Run(run_id=rid, agent_id="demo", agent_version="1", model_alias="m", subject="u", state=state.value, durable=durable))
        await s.commit()
    return rid


async def _append(run_id: str, type_: RunEventType = RunEventType.MODEL_RESPONSE, payload: dict[str, object] | None = None) -> int:
    async with db.SessionLocal() as s:
        ev = await EventLog(s).append(run_id, type_, payload or {})
        await s.commit()
    return ev.seq


async def _seqs(run_id: str) -> tuple[list[int], int, str]:
    async with db.SessionLocal() as s:
        events = await EventLog(s).list(run_id)
        run = await s.get(Run, run_id)
    assert run is not None
    return [e.seq for e in events], run.last_seq, run.state


# ---- seq CAS ----


async def test_concurrent_appends_keep_seq_contiguous() -> None:
    """多个写者并发追加：冲突者抛 SeqConflict，重试后 seq 连续 1..N、last_seq = N，无重复无空洞。"""
    rid = await _new_run()
    conflicts = 0

    async def writer(i: int) -> None:
        nonlocal conflicts
        for _ in range(50):
            async with db.SessionLocal() as s:
                try:
                    await EventLog(s).append(rid, RunEventType.MODEL_RESPONSE, {"i": i})
                    await s.commit()
                    return
                except SeqConflict:
                    conflicts += 1
                    await s.rollback()
            await asyncio.sleep(0.005)
        raise AssertionError("writer starved")

    await asyncio.gather(*(writer(i) for i in range(8)))
    seqs, last, _ = await _seqs(rid)
    assert seqs == list(range(1, 9))
    assert last == 8


async def test_stale_head_raises_seq_conflict_and_writes_nothing() -> None:
    """读到 last_seq 后被并发写者抢先：CAS 命中 0 行抛 SeqConflict（StaleWriter 子类），回滚后日志只有对方那条。"""
    rid = await _new_run()
    async with db.SessionLocal() as s1:
        log1 = EventLog(s1)
        head = await log1._head(rid)
        assert await _append(rid) == 1
        with pytest.raises(SeqConflict) as ei:
            await log1._advance(rid, head)
        assert isinstance(ei.value, StaleWriter)
        await s1.rollback()
    assert (await _seqs(rid))[:2] == ([1], 1)


async def test_append_missing_run_raises_run_not_found() -> None:
    async with db.SessionLocal() as s:
        with pytest.raises(RunNotFound):
            await EventLog(s).append("no-such-run", RunEventType.MODEL_RESPONSE, {})


async def test_cached_run_object_follows_append_and_transition() -> None:
    """同一会话里先 get 过 Run：append / transition 后 identity map 中的对象同步到新 last_seq / state。"""
    rid = await _new_run()
    async with db.SessionLocal() as s:
        run = await s.get(Run, rid)
        assert run is not None
        log = EventLog(s)
        await log.append(rid, RunEventType.RUN_CREATED, {})
        await log.transition(rid, RunState.WORKING, "start")
        assert (run.last_seq, run.state) == (2, RunState.WORKING.value)
        await s.commit()
    assert await _seqs(rid) == ([1, 2], 2, RunState.WORKING.value)


# ---- transition CAS ----


async def test_transition_cas_on_state() -> None:
    """读到 submitted 后被并发取消：带 state=src 的 CAS 失败抛 SeqConflict，状态保持 canceled。"""
    rid = await _new_run()
    async with db.SessionLocal() as s1:
        log1 = EventLog(s1)
        head = await log1._head(rid)
        async with db.SessionLocal() as s2:
            await EventLog(s2).transition(rid, RunState.CANCELED, "user")
            await s2.commit()
        with pytest.raises(SeqConflict):
            await log1._advance(rid, head, src=RunState.SUBMITTED, dst=RunState.WORKING)
        await s1.rollback()
    _, _, state = await _seqs(rid)
    assert state == RunState.CANCELED.value


async def test_transition_semantics_preserved() -> None:
    """已在目标状态返回 dst 且不写事件；非法迁移仍抛 ValueError；合法迁移写 RUN_STATE_CHANGED。"""
    rid = await _new_run()
    async with db.SessionLocal() as s:
        log = EventLog(s)
        assert await log.transition(rid, RunState.SUBMITTED) == RunState.SUBMITTED
        assert await log.transition(rid, RunState.WORKING, "start") == RunState.WORKING
        await s.commit()
    async with db.SessionLocal() as s:
        log = EventLog(s)
        await log.transition(rid, RunState.COMPLETED, "end_turn")
        await s.commit()
        with pytest.raises(ValueError, match="illegal transition"):
            await log.transition(rid, RunState.WORKING)
        events = await log.list(rid)
    assert [(e.type, e.payload["to"]) for e in events] == [
        (RunEventType.RUN_STATE_CHANGED, "working"),
        (RunEventType.RUN_STATE_CHANGED, "completed"),
    ]


async def test_transition_with_stale_session_never_loses_update() -> None:
    """会话先读过 Run（SQLite 读到最新，MySQL 可重复读快照）后被并发取消：迁移要么按最新状态判为非法，
    要么 CAS 失败，绝不会把 canceled 覆盖成 working。"""
    rid = await _new_run()
    async with db.SessionLocal() as s1:
        assert await s1.get(Run, rid) is not None
        async with db.SessionLocal() as s2:
            await EventLog(s2).transition(rid, RunState.CANCELED, "user")
            await s2.commit()
        with pytest.raises((ValueError, SeqConflict)):
            await EventLog(s1).transition(rid, RunState.WORKING, "start")
        await s1.rollback()
    assert (await _seqs(rid))[2] == RunState.CANCELED.value


# ---- fencing ----


async def test_fence_lost_after_takeover_raises_lease_lost() -> None:
    """旧持有者租约过期、被新 Worker 抢占（epoch+1）后，带旧 fence 的追加 / 迁移抛 LeaseLost；新 fence 正常写入。"""
    rid = await _new_run(durable=True)
    e1 = await acquire_lease(rid, "w1", 0.2)
    assert e1 == 1
    with fence_scope(e1, rid):
        assert current_fence(rid) == 1
        assert await _append(rid) == 1
    await asyncio.sleep(0.5)
    e2 = await acquire_lease(rid, "w2", 30)
    assert e2 == 2
    with fence_scope(e1, rid):
        with pytest.raises(LeaseLost):
            await _append(rid)
        async with db.SessionLocal() as s:
            with pytest.raises(LeaseLost):
                await EventLog(s).transition(rid, RunState.WORKING, "start")
    with fence_scope(e2):
        assert await _append(rid) == 2
        async with db.SessionLocal() as s:
            await EventLog(s).transition(rid, RunState.WORKING, "start")
            await s.commit()
    assert current_fence(rid) is None
    assert (await _seqs(rid))[:2] == ([1, 2, 3], 3)


async def test_fence_epoch_changed_mid_transaction_raises_lease_lost() -> None:
    """读完 head 后纪元才被推进（并发抢占）：CAS 命中 0 行，归因为 LeaseLost 而非 SeqConflict。"""
    rid = await _new_run(durable=True)
    assert await acquire_lease(rid, "w1", 0.2) == 1
    with fence_scope(1, rid):
        async with db.SessionLocal() as s1:
            log1 = EventLog(s1)
            head = await log1._head(rid)
            await asyncio.sleep(0.5)
            assert await acquire_lease(rid, "w2", 30) == 2
            with pytest.raises(LeaseLost):
                await log1._advance(rid, head)
            await s1.rollback()


async def test_fence_scoped_to_run_id() -> None:
    """fence_scope(epoch, run_id) 只约束该 Run；其他 Run 的写入不受影响。"""
    fenced, other = await _new_run(), await _new_run()
    with fence_scope(99, fenced):
        assert current_fence(other) is None
        assert await _append(other) == 1
        with pytest.raises(LeaseLost):
            await _append(fenced)


# ---- 租约原语 ----


async def test_lease_acquire_renew_release_and_epoch_monotonic() -> None:
    rid = await _new_run(durable=True)
    assert await acquire_lease(rid, "w1", 30) == 1
    assert await acquire_lease(rid, "w2", 30) is None  # 他人持有有效租约
    assert await acquire_lease(rid, "w1", 30) is None  # 重复投递到同一 Worker 也不重入
    assert await renew_lease(rid, "w1", 1, 30) is True
    assert await renew_lease(rid, "w1", 2, 30) is False  # epoch 不符
    assert await renew_lease(rid, "w2", 1, 30) is False  # 持有者不符
    assert await release_lease(rid, "w2", 1) is False
    assert await release_lease(rid, "w1", 1) is True
    owner, epoch, expires = await lease_info(rid)
    assert (owner, epoch, expires) == (None, 1, None)
    assert await acquire_lease(rid, "w2", 30) == 2  # 释放后可再抢，epoch 单调递增
    assert await renew_lease(rid, "w1", 1, 30) is False  # 旧持有者续期失败 → 自我隔离


async def test_lease_expiry_allows_takeover_and_reaper_lists_only_durable() -> None:
    durable = await _new_run(durable=True)
    legacy = await _new_run(durable=False)
    done = await _new_run(durable=True, state=RunState.COMPLETED)
    held = await _new_run(durable=True)
    assert await acquire_lease(held, "w1", 30) == 1
    assert await acquire_lease(durable, "w1", 0.2) == 1
    await asyncio.sleep(0.5)
    found = set(await expired_unleased_runs(10_000))
    assert durable in found  # 租约过期
    assert not {legacy, done, held} & found  # 非 durable / 终态 / 持有有效租约
    assert await renew_lease(durable, "w1", 1, 30) is True  # 过期但无人接管：原持有者仍可续期
    assert durable not in set(await expired_unleased_runs(10_000))
    assert await acquire_lease(done, "w1", 30) is None  # 终态不可租
    assert await acquire_lease("no-such-run", "w1", 30) is None


async def test_concurrent_acquire_has_single_winner() -> None:
    rid = await _new_run(durable=True)
    results = await asyncio.gather(*(acquire_lease(rid, f"w{i}", 30) for i in range(6)))
    assert sorted(r for r in results if r is not None) == [1]
    assert results.count(None) == 5


# ---- 写工具操作登记 ----


async def test_operation_recorded_with_tool_executing_and_deduplicated() -> None:
    rid = await _new_run(durable=True)
    epoch = await acquire_lease(rid, "w1", 30)
    assert epoch == 1
    op_id = f"op-{uuid4().hex}"
    with fence_scope(epoch, rid):
        async with db.SessionLocal() as s:
            log = EventLog(s)
            await log.record_operation(rid, op_id, "call-1", node="w1")
            await log.append(rid, RunEventType.TOOL_EXECUTING, {"call_id": "call-1", "operation_id": op_id})
            await s.commit()
    async with db.SessionLocal() as s:
        log = EventLog(s)
        with pytest.raises(OperationExists):
            await log.record_operation(rid, op_id, "call-1")
        await s.rollback()
    async with db.SessionLocal() as s:
        log = EventLog(s)
        assert await log.update_operation(op_id, "executed", "h" * 64) is True
        assert await log.update_operation("no-such-op", "executed") is False
        await s.commit()
    async with db.SessionLocal() as s:
        op = await EventLog(s).get_operation(op_id)
        assert op is not None
        assert (op.run_id, op.call_id, op.status, op.result_hash, op.node, op.epoch) == (rid, "call-1", "executed", "h" * 64, "w1", 1)
        assert [e.type for e in await EventLog(s).list(rid)] == [RunEventType.TOOL_EXECUTING]


async def test_operation_update_not_fenced_for_lost_lease_worker() -> None:
    """失租旧 Worker 仍能把执行结果写回登记表（供对账卡一键确认），但不能再追加事件。"""
    rid = await _new_run(durable=True)
    op_id = f"op-{uuid4().hex}"
    async with db.SessionLocal() as s:
        await EventLog(s).record_operation(rid, op_id, "c")
        await s.commit()
    with fence_scope(7, rid):
        async with db.SessionLocal() as s:
            log = EventLog(s)
            assert await log.update_operation(op_id, "executed", "x") is True
            await s.commit()
        with pytest.raises(LeaseLost):
            await _append(rid, RunEventType.TOOL_EXECUTED, {"call_id": "c"})
    async with db.SessionLocal() as s:
        op = await s.get(ToolOperation, op_id)
        assert op is not None and op.status == "executed" and op.epoch == 0


# ---- 迁移 ----


def test_migration_0004_backfills_existing_runs(tmp_path: Path) -> None:
    """在 0003 上已有 Run 的库升级到 0004：旧 Run durable=0、lease_epoch=0、无持有者；新表与索引齐全；可降级回 0003。"""
    eng = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    try:
        with eng.begin() as conn:
            cfg = alembic_config()
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "0003")
            conn.execute(
                text(
                    "INSERT INTO t_run (run_id, tenant_id, agent_id, agent_version, model_alias, subject, state, last_seq, created_at) "
                    "VALUES ('old', 'default', 'a', '1', 'm', 'u', 'working', 3, '2026-01-01 00:00:00')"
                )
            )
            command.upgrade(cfg, "head")
        with eng.connect() as conn:
            row = conn.execute(text("SELECT durable, lease_epoch, lease_owner, lease_expires_at, last_seq FROM t_run")).one()
            assert tuple(row) == (0, 0, None, None, 3)
            insp = inspect(conn)
            assert {"t_run_input", "t_run_credential", "t_tool_operation"} <= set(insp.get_table_names())
            assert {i["name"] for i in insp.get_indexes("t_run")} >= {
                "ix_run_tenant_subject_created",
                "ix_run_tenant_session_created",
                "ix_run_durable_state",
            }
        with eng.begin() as conn:
            cfg = alembic_config()
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, "0003")
        with eng.connect() as conn:
            insp = inspect(conn)
            assert "t_tool_operation" not in insp.get_table_names()
            assert "durable" not in {c["name"] for c in insp.get_columns("t_run")}
    finally:
        eng.dispose()


async def test_migrate_function_on_fresh_sqlite(tmp_path: Path) -> None:
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    try:
        await db.migrate(eng)
        await db.migrate(eng)  # 重复执行为空操作
        async with eng.connect() as conn:
            assert (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == "0011"
    finally:
        await eng.dispose()


async def test_init_db_respects_migrate_on_start(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []

    async def fake_migrate(*_: object, **__: object) -> None:
        called.append(True)

    monkeypatch.setattr(db, "migrate", fake_migrate)
    monkeypatch.setenv("RUNTIME_MIGRATE_ON_START", "false")
    await db.init_db()
    assert called == []
    monkeypatch.setenv("RUNTIME_MIGRATE_ON_START", "true")
    await db.init_db()
    assert called == [True]


def test_python_m_runtime_migrate(tmp_path: Path) -> None:
    """独立迁移 Job：`python -m runtime.migrate` 把 DATABASE_URL 指向的空库升级到 head。"""
    path = tmp_path / "job.db"
    env = os.environ | {"DATABASE_URL": f"sqlite+aiosqlite:///{path}", "RUNTIME_MIGRATE_ON_START": "false"}
    r = subprocess.run([sys.executable, "-m", "runtime.migrate"], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    eng = create_engine(f"sqlite:///{path}")
    try:
        with eng.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0011"
    finally:
        eng.dispose()


# ---- MySQL：GET_LOCK 串行化并发迁移 ----


@pytest.mark.skipif(not _IS_MYSQL, reason="GET_LOCK 路径仅在 MySQL（make test-mysql）下运行")
async def test_mysql_concurrent_migrations_on_empty_db() -> None:
    """三个"节点"同时对空库迁移：GET_LOCK 串行化，全部成功且只落到 head 一次（无"表已存在"失败）。"""
    base = db.normalize_database_url(_EXTERNAL)
    name = f"{base.database}_s7mig"
    server = create_async_engine(base._replace(database=None), poolclass=NullPool)
    try:
        async with server.begin() as conn:
            await conn.execute(text(f"DROP DATABASE IF EXISTS `{name}`"))
            await conn.execute(text(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4"))
        engines = [create_async_engine(base._replace(database=name), poolclass=NullPool) for _ in range(3)]
        try:
            await asyncio.gather(*(db.migrate(e) for e in engines))
            async with engines[0].connect() as conn:
                assert [r[0] for r in await conn.execute(text("SELECT version_num FROM alembic_version"))] == ["0011"]
        finally:
            for e in engines:
                await e.dispose()
    finally:
        async with server.begin() as conn:
            await conn.execute(text(f"DROP DATABASE IF EXISTS `{name}`"))
        await server.dispose()


@pytest.mark.skipif(not _IS_MYSQL, reason="GET_LOCK 路径仅在 MySQL（make test-mysql）下运行")
async def test_mysql_migrate_lock_timeout() -> None:
    """迁移锁被其他节点持有且超时：migrate 抛 RuntimeError，不在无锁状态下迁移；锁释放后正常完成。"""
    eng = create_async_engine(db.normalize_database_url(_EXTERNAL), poolclass=NullPool)
    try:
        async with eng.connect() as holder:
            assert (await holder.execute(text("SELECT GET_LOCK(:n, 0)"), {"n": db.MIGRATE_LOCK})).scalar() == 1
            with pytest.raises(RuntimeError, match="迁移锁"):
                await db.migrate(eng, lock_timeout_s=1)
            await holder.execute(text("SELECT RELEASE_LOCK(:n)"), {"n": db.MIGRATE_LOCK})
        await db.migrate(eng, lock_timeout_s=5)
    finally:
        await eng.dispose()

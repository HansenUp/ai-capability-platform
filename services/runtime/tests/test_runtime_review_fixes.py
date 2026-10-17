"""代码评审修复的回归测试：旧库（create_all 建表、无 alembic_version）启动迁移、续跑时 confidential 污点重建
（ADR-0010 脱敏）、事件日志重建 tool_use 消息的 reasoning 字段。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from test_runner_stop_reasons import ScriptedHarness, _events, _publish

from ai_contract import DoneEvent, Event, RunEvent, RunEventType, TextEvent, ToolSpec, ToolUseEvent, UsageEvent
from runtime.assemble import assembler
from runtime.loop import toolbroker
from runtime.run import invariant
from runtime.state import db

# ---------------- 旧库接入迁移 ----------------


def _current_revision(conn: Connection) -> str | None:
    return MigrationContext.configure(conn).get_current_revision()


def _legacy_schema(conn: Connection) -> None:
    """模拟 Phase 0 旧库：表结构与基线 0001 一致（当时由 create_all 建出），但没有 alembic_version。"""
    cfg = db.alembic_config()
    cfg.attributes["connection"] = conn
    command.upgrade(cfg, db.BASELINE_REVISION)
    conn.exec_driver_sql("DROP TABLE alembic_version")


async def test_init_db_stamps_legacy_create_all_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 0 旧库：四张表齐全且没有 alembic_version，init_db 先 stamp 0001 再 upgrade 到最新版本，启动成功。"""
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    try:
        async with eng.begin() as conn:
            await conn.run_sync(_legacy_schema)
        monkeypatch.setattr(db, "engine", eng)
        await db.init_db()
        await db.init_db()  # 已打标后再次启动照常是 no-op
        head = ScriptDirectory.from_config(db.alembic_config()).get_current_head()
        async with eng.connect() as conn:
            assert await conn.run_sync(_current_revision) == head
    finally:
        await eng.dispose()


async def test_init_db_refuses_partial_legacy_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """只有部分 Runtime 表且没有 alembic_version：无法确定基线，抛出说明需人工处理的错误，不自动打标。"""
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'partial.db'}")
    try:
        async with eng.begin() as conn:
            await conn.run_sync(lambda c: db.Base.metadata.create_all(c, tables=[db.Run.__table__]))
        monkeypatch.setattr(db, "engine", eng)
        with pytest.raises(RuntimeError, match="人工"):
            await db.init_db()
        async with eng.connect() as conn:
            assert await conn.run_sync(_current_revision) is None
    finally:
        await eng.dispose()


# ---------------- reasoning 随 tool_use 消息重建 ----------------


def _ev(seq: int, type_: RunEventType, payload: dict[str, Any]) -> RunEvent:
    return RunEvent(run_id="run_x", seq=seq, type=type_, payload=payload)


def test_rebuild_messages_carries_reasoning() -> None:
    """TOOL_REQUESTED 带 reasoning 时重建出的 tool_use 消息带同名顶层字段；不带时不出现该键；check 与运行期历史一致。"""
    log = [
        _ev(1, RunEventType.TOOL_REQUESTED, {"call_id": "c1", "tool": "echo", "input": {"a": 1}, "reasoning": "想一想"}),
        _ev(2, RunEventType.TOOL_RESULT, {"call_id": "c1", "status": "ok", "output": {"echo": 1}}),
        _ev(3, RunEventType.TOOL_REQUESTED, {"call_id": "c2", "tool": "echo", "input": {}}),
    ]
    initial = [{"role": "user", "content": "hi"}]
    msgs = invariant.rebuild_messages(log, initial)
    assert msgs[1] == {"role": "assistant", "tool_use": {"call_id": "c1", "name": "echo", "input": {"a": 1}}, "reasoning": "想一想"}
    assert "reasoning" not in msgs[3]
    assert invariant.check(log, initial, msgs) == invariant.message_hash(msgs)
    without = [m for m in msgs]
    without[1] = {k: v for k, v in msgs[1].items() if k != "reasoning"}
    with pytest.raises(invariant.InvariantViolation):
        invariant.check(log, initial, without)


# ---------------- 续跑时 confidential 污点重建（ADR-0010） ----------------


@pytest.fixture(scope="module")
def exporter(client: Any) -> InMemorySpanExporter:
    """向 TracerProvider 注入内存导出器（同步导出，span 结束即可读取）。"""
    from runtime.observe import tracing

    exp = InMemorySpanExporter()
    tracing.add_span_processor(SimpleSpanProcessor(exp))
    return exp


def test_resume_rebuilds_confidential_taint_and_redacts(
    client: Any, exporter: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """机密读取 → 高风险写挂起 → 批准 → 续跑：续跑新建的 RunCtx 从事件日志重建 confidential 污点，
    续跑后的 chat span 标记 ai.content_redacted 且不记补全原文；TOOL_RESULT 记录了工具的数据分类。"""
    secret = ToolSpec(name="kb.secret", description="机密知识库（测试）", data_class="confidential")
    monkeypatch.setitem(assembler.LOCAL_TOOLS, "kb.secret", secret)
    original = toolbroker.execute_tool

    async def _exec(tool: ToolSpec, tool_input: dict[str, Any], dry_run: bool = False) -> Any:
        if tool.name == "kb.secret":
            return {"doc": "机密正文"}
        return await original(tool, tool_input, dry_run)

    monkeypatch.setattr(toolbroker, "execute_tool", _exec)

    name = f"scripted-taint-{uuid.uuid4().hex[:6]}"
    close = ToolUseEvent(call_id="c-close-" + uuid.uuid4().hex[:6], tool_name="ticket.close_batch", input={"ids": ["T-1"]})
    turns: list[list[Event | Exception]] = [
        [
            ToolUseEvent(call_id="c-secret-" + uuid.uuid4().hex[:6], tool_name="kb.secret", input={}),
            DoneEvent(run_id="x", stop_reason="tool_use"),
        ],
        [close, DoneEvent(run_id="x", stop_reason="tool_use")],
        [TextEvent(delta="已关闭，机密正文摘要"), UsageEvent(model="scripted"), DoneEvent(run_id="x")],
    ]
    _publish(client, name, ScriptedHarness(name, turns), {"tools": ["kb.secret", "ticket.close_batch"]})
    msgs = [{"role": "user", "content": "查机密后关单"}]
    with client.stream("POST", "/v1/messages", json={"agent_id": name, "messages": msgs}) as r:
        assert r.status_code == 200
        run_id = r.headers["x-run-id"]
        evs = _events(r)
    card = next(e for e in evs if e["type"] == "card")
    assert evs[-1]["stop_reason"] == "awaiting"

    log = client.get(f"/v1/runs/{run_id}/events").json()
    secret_result = next(e for e in log if e["type"] == "tool.result" and e["payload"]["call_id"].startswith("c-secret-"))
    assert secret_result["payload"]["data_class"] == "confidential"

    body = {"call_id": card["payload"]["call_id"], "params_hash": card["payload"]["params_hash"]}
    assert client.post(f"/v1/runs/{run_id}/approve", json=body).status_code == 200
    exporter.clear()
    with client.stream("POST", f"/v1/runs/{run_id}/resume", json={"messages": msgs}) as r:
        assert r.status_code == 200
        evs = _events(r)
    assert evs[-1]["stop_reason"] == "end_turn"
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "completed"

    finished = exporter.get_finished_spans()
    root = next(s for s in finished if s.attributes and s.attributes.get("ai.run_id") == run_id)
    assert root.attributes and root.attributes.get("ai.resumed_from_trace_id")  # 确为续跑的那条 Trace
    chats = [s for s in finished if s.parent is not None and s.parent.span_id == root.context.span_id and s.name.startswith("chat ")]
    assert chats
    for span in chats:
        attrs = dict(span.attributes or {})
        assert attrs.get("ai.content_redacted") is True
        assert "gen_ai.completion" not in attrs and "gen_ai.prompt" not in attrs
    assert "机密正文" not in json.dumps([dict(s.attributes or {}) for s in finished], ensure_ascii=False)

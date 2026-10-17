"""S7 WP-C：DEF-1 双预算（令牌 + 墙钟）为主控、轮数上限降为兜底。

累计全部取自事件日志：令牌与 S5 token_budget 同口径（MODEL_RESPONSE 的 input + output），墙钟自 RUN_CREATED 起算并扣除
input-required / auth-required 区间；接管 / 续跑不重置。超预算 RUN_ERROR BUDGET_EXCEEDED、Run failed。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import update

from ai_contract import (
    DoneEvent,
    Event,
    HarnessCapabilities,
    HookSet,
    RunEvent,
    RunEventType,
    RunState,
    SessionHandle,
    SessionSpec,
    SkillDir,
    TextEvent,
    ToolSpec,
    ToolUseEvent,
    TurnInput,
    UsageEvent,
)
from runtime.harness import registry
from runtime.loop import runner
from runtime.run import invariant
from runtime.state import db
from runtime.state.db import AgentVersion, RunEventRow, RunInput
from runtime.state.eventlog import EventLog

SUBJECT = "u-budget"


class _Harness:
    """每轮按脚本产出事件；记录被调用的轮数。"""

    def __init__(self, name: str, turns: list[list[Event]]) -> None:
        self.name, self.turns, self.calls = name, turns, 0

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(name=self.name, version="test", transport="in-process", loop_owner="platform")

    async def open_session(self, spec: SessionSpec) -> SessionHandle:
        return SessionHandle(session_id=uuid.uuid4().hex, harness=self.name)

    async def resume(self, spec: SessionSpec, events: list[Event]) -> SessionHandle:
        return await self.open_session(spec)

    async def run_turn(
        self, session: SessionHandle, turn: TurnInput, tools: list[ToolSpec], skills: list[SkillDir], hooks: HookSet
    ) -> AsyncIterator[Event]:
        self.calls += 1
        for ev in self.turns[min(self.calls, len(self.turns)) - 1]:
            yield ev

    async def interrupt(self, session: SessionHandle, run_id: str) -> None:
        return None


async def _agent(config: dict[str, Any], harness: _Harness | None = None) -> AgentVersion:
    name = f"s7b-{uuid.uuid4().hex[:8]}"
    if harness is not None:
        harness.name = name
        registry.register(name, harness)
    av = AgentVersion(agent_id=name, version="1", status="published", system_prompt="x", config=config, harness=name if harness else None)
    async with db.SessionLocal() as s:
        s.add(av)
        await s.commit()
    return av


async def _run(agent: AgentVersion, content: str = "hi") -> str:
    msgs = [{"role": "user", "content": content}]
    rid = await runner.create_run(agent, SUBJECT, None, initial_messages=msgs)
    async with db.SessionLocal() as s:
        s.add(RunInput(run_id=rid, messages=msgs, fingerprint=invariant.message_hash(msgs)))
        await s.commit()
    return rid


async def _drive(rid: str) -> list[Event]:
    return [ev async for ev in runner.drive_durable(rid)]


async def _log(rid: str) -> list[RunEvent]:
    async with db.SessionLocal() as s:
        return await EventLog(s).list(rid)


async def _set_ts(rid: str, seq: int, ts: datetime) -> None:
    async with db.SessionLocal() as s:
        await s.execute(update(RunEventRow).where(RunEventRow.run_id == rid, RunEventRow.seq == seq).values(ts=ts))
        await s.commit()


def _err(log: list[RunEvent]) -> dict[str, Any]:
    return [e.payload for e in log if e.type == RunEventType.RUN_ERROR][-1]


TOOL_TURN: list[Event] = [
    TextEvent(delta="查一下"),
    ToolUseEvent(call_id="c1", tool_name="echo", input={"text": "x"}),
    UsageEvent(input_tokens=40, output_tokens=10),
    DoneEvent(run_id="r", stop_reason="tool_use"),
]
END_TURN: list[Event] = [
    TextEvent(delta="完成"),
    UsageEvent(input_tokens=1, output_tokens=1),
    DoneEvent(run_id="r", stop_reason="end_turn"),
]


# ---------------- 纯函数 ----------------


def _ev(seq: int, type_: RunEventType, ts: datetime, **payload: Any) -> RunEvent:
    return RunEvent(run_id="r", seq=seq, type=type_, payload=payload, ts=ts)


def test_wall_seconds_excludes_suspended_intervals() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    log = [
        _ev(1, RunEventType.RUN_CREATED, t0),
        _ev(2, RunEventType.RUN_STATE_CHANGED, t0 + timedelta(seconds=5), to="working"),
        _ev(3, RunEventType.RUN_STATE_CHANGED, t0 + timedelta(seconds=10), to="auth-required"),
        _ev(4, RunEventType.RUN_STATE_CHANGED, t0 + timedelta(seconds=110), to="working"),
        _ev(5, RunEventType.RUN_STATE_CHANGED, t0 + timedelta(seconds=120), to="input-required"),
    ]
    # 自首次进入 working（t0+5）起算：创建后排队的 5 s 不计
    assert runner.wall_seconds(log[:4], t0 + timedelta(seconds=130)) == pytest.approx(25)
    # 仍在挂起：到 now 为止的挂起时间同样扣除
    assert runner.wall_seconds(log, t0 + timedelta(seconds=200)) == pytest.approx(15)
    # 尚未进入过 working（仍在排队）：0
    assert runner.wall_seconds(log[:1], t0 + timedelta(seconds=500)) == 0.0
    assert runner.usage_tokens([_ev(1, RunEventType.MODEL_RESPONSE, t0, usage={"input_tokens": 3, "output_tokens": 4})]) == {
        "input_tokens": 3,
        "output_tokens": 4,
        "total_tokens": 7,
    }


@pytest.mark.parametrize(
    "raw",
    [[], {"max_total_tokens": 0}, {"max_wall_s": -1}, {"max_total_tokens": True}, {"max_rounds": 3}, {"max_total_tokens": 1, "x": 1}],
)
def test_budget_config_validation(raw: Any) -> None:
    with pytest.raises(ValueError):
        runner.budget_of(AgentVersion(config={"budget": raw}))


def test_budget_config_defaults() -> None:
    assert runner.budget_of(AgentVersion(config={})) is None
    b = runner.budget_of(AgentVersion(config={"budget": {"max_wall_s": 1.5}}))
    assert b == runner.Budget(None, 1.5, runner.BUDGET_MAX_ROUNDS)


# ---------------- 驱动 ----------------


async def test_token_budget_exceeded_fails_run_before_next_model_call() -> None:
    h = _Harness("", [TOOL_TURN, END_TURN])
    agent = await _agent({"tools": ["echo"], "budget": {"max_total_tokens": 50}}, h)
    rid = await _run(agent)
    evs = await _drive(rid)
    assert isinstance(evs[-1], DoneEvent) and evs[-1].stop_reason == "error"
    assert h.calls == 1  # 第二轮未调用模型
    log = await _log(rid)
    err = _err(log)
    assert err["code"] == "BUDGET_EXCEEDED" and err["budget"] == "tokens" and err["used"] == 50 and err["limit"] == 50
    assert log[-1].payload["to"] == RunState.FAILED
    assert sum(1 for e in log if e.type == RunEventType.MODEL_REQUEST) == 1


async def test_token_budget_accumulates_across_takeover() -> None:
    """接管不重置：日志里已有先前 Worker 的用量，续跑一开始即超预算，不再调用模型。"""
    h = _Harness("", [END_TURN])
    agent = await _agent({"tools": ["echo"], "budget": {"max_total_tokens": 100}}, h)
    rid = await _run(agent)
    async with db.SessionLocal() as s:
        log = EventLog(s)
        await log.transition(rid, RunState.WORKING, "start")
        for _ in range(2):  # 上一个 Worker 跑过两轮（共 120 tokens）后失联
            await log.append(rid, RunEventType.MODEL_REQUEST, {"model_alias": "x"})
            await log.append(rid, RunEventType.MODEL_RESPONSE, {"text": "", "usage": {"input_tokens": 50, "output_tokens": 10}})
        await s.commit()
    await _drive(rid)
    assert h.calls == 0
    err = _err(await _log(rid))
    assert err["code"] == "BUDGET_EXCEEDED" and err["used"] == 120


async def test_wall_budget_exceeded() -> None:
    agent = await _agent({"tools": ["echo"], "budget": {"max_wall_s": 30}})
    rid = await _run(agent)
    async with db.SessionLocal() as s:  # 60 s 前首次进入 working（如上一个 Worker 开始执行后失联）
        await EventLog(s).transition(rid, RunState.WORKING, "start")
        await s.commit()
    await _set_ts(rid, 2, datetime.now(UTC) - timedelta(seconds=60))
    evs = await _drive(rid)
    assert isinstance(evs[-1], DoneEvent) and evs[-1].stop_reason == "error"
    err = _err(await _log(rid))
    assert err["code"] == "BUDGET_EXCEEDED" and err["budget"] == "wall" and err["used"] >= 60


async def test_wall_budget_does_not_count_queue_time() -> None:
    """DEF-1：墙钟自首次进入 working 起算——创建后在队列里等了 100 s 的 Run 不会一开始就判超限。"""
    agent = await _agent({"tools": ["echo"], "budget": {"max_wall_s": 30}})
    rid = await _run(agent)
    await _set_ts(rid, 1, datetime.now(UTC) - timedelta(seconds=100))  # 创建（入队）于 100 s 前
    evs = await _drive(rid)
    assert isinstance(evs[-1], DoneEvent) and evs[-1].stop_reason == "end_turn"
    assert not [e for e in await _log(rid) if e.type == RunEventType.RUN_ERROR]


async def test_wall_budget_does_not_count_time_waiting_for_humans() -> None:
    agent = await _agent({"tools": ["echo"], "budget": {"max_wall_s": 30}})
    rid = await _run(agent)
    now = datetime.now(UTC)
    async with db.SessionLocal() as s:
        log = EventLog(s)
        await log.transition(rid, RunState.WORKING, "start")
        await log.transition(rid, RunState.INPUT_REQUIRED, "awaiting user input")
        await log.transition(rid, RunState.WORKING, "input answer")
        await s.commit()
    await _set_ts(rid, 1, now - timedelta(seconds=100))  # 创建于 100 s 前
    await _set_ts(rid, 2, now - timedelta(seconds=99))
    await _set_ts(rid, 3, now - timedelta(seconds=95))  # 挂起 94 s
    await _set_ts(rid, 4, now - timedelta(seconds=1))
    evs = await _drive(rid)
    assert isinstance(evs[-1], DoneEvent) and evs[-1].stop_reason == "end_turn"
    async with db.SessionLocal() as s:
        run = await s.get(db.Run, rid)
    assert run is not None and run.state == RunState.COMPLETED


async def test_rounds_cap_is_fallback_when_budget_configured() -> None:
    """配置 budget 后轮数上限为兜底（可用 max_rounds 覆盖），超出仍以 MAX_TOOL_ROUNDS 失败。"""
    loop_turn: list[Event] = [ToolUseEvent(call_id="c", tool_name="echo", input={}), DoneEvent(run_id="r", stop_reason="tool_use")]
    h = _Harness("", [loop_turn])
    agent = await _agent({"tools": ["echo"], "budget": {"max_total_tokens": 10**6, "max_rounds": 3}}, h)
    rid = await _run(agent)
    await _drive(rid)
    assert h.calls == 3 and _err(await _log(rid))["code"] == "MAX_TOOL_ROUNDS"


async def test_invalid_budget_config_fails_closed() -> None:
    agent = await _agent({"tools": ["echo"], "budget": {"max_total_tokens": -5}})
    rid = await _run(agent)
    evs = await _drive(rid)
    assert isinstance(evs[-1], DoneEvent) and evs[-1].stop_reason == "error"
    assert _err(await _log(rid))["code"] == "BUDGET_CONFIG_INVALID"

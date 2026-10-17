"""S7 WP-C：input-required 与写前登记。

- request_user_input：注入 / 挂起 → provide_input(answer) → 续跑完成；schema 不符 ValueError；非发起人拒绝；入参不合规作为工具错误。
- t_tool_operation 跨 Worker 去重：同一 operation 只执行一次；失租旧 Worker 结果写回登记行、接管者对账卡附登记状态；
  对账 done / skip / retry（retry 只允许下游声明幂等的工具）。
- MOCK_TURN_DELAY_S 故障注入与 active.interrupt。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from ai_contract import (
    CardEvent,
    CardType,
    DoneEvent,
    Event,
    InputRequiredEvent,
    RunEvent,
    RunEventType,
    RunState,
    SessionSpec,
    ToolUseEvent,
)
from runtime.assemble.assembler import LOCAL_TOOLS
from runtime.guard.gate import GateContext, evaluate
from runtime.loop import active, runner, toolbroker, user_input
from runtime.loop.toolbroker import RunCtx, handle_tool
from runtime.run import invariant
from runtime.state import db
from runtime.state.db import AgentVersion, RunInput, ToolOperation
from runtime.state.eventlog import EventLog, LeaseLost, acquire_lease, fence_scope

SUBJECT = "u-s7"


async def _agent(config: dict[str, Any]) -> AgentVersion:
    av = AgentVersion(agent_id=f"s7c-{uuid.uuid4().hex[:8]}", version="1", status="published", system_prompt="x", config=config)
    async with db.SessionLocal() as s:
        s.add(av)
        await s.commit()
    return av


async def _run(agent: AgentVersion, content: str) -> tuple[str, list[dict[str, Any]]]:
    msgs = [{"role": "user", "content": content}]
    rid = await runner.create_run(agent, SUBJECT, None, initial_messages=msgs)
    async with db.SessionLocal() as s:
        s.add(RunInput(run_id=rid, messages=msgs, fingerprint=invariant.message_hash(msgs)))
        await s.commit()
    return rid, msgs


async def _collect(gen: AsyncIterator[Event]) -> list[Event]:
    return [ev async for ev in gen]


async def _log(rid: str) -> list[RunEvent]:
    async with db.SessionLocal() as s:
        return await EventLog(s).list(rid)


async def _state(rid: str) -> str:
    async with db.SessionLocal() as s:
        run = await s.get(db.Run, rid)
    assert run is not None
    return run.state


def _types(log: list[RunEvent]) -> list[str]:
    return [e.type.value for e in log]


# ---------------- request_user_input ----------------

SCHEMA = {"type": "object", "properties": {"id": {"type": "string", "minLength": 2}}, "required": ["id"], "additionalProperties": False}


async def test_request_user_input_suspends_then_answer_resumes_to_completion() -> None:
    agent = await _agent({"tools": ["echo"], "user_input": True})
    rid, _ = await _run(agent, "!tool request_user_input " + json.dumps({"prompt": "哪个工单？", "schema": SCHEMA}, ensure_ascii=False))
    evs = await _collect(runner.drive_durable(rid))
    asked = [e for e in evs if isinstance(e, InputRequiredEvent)]
    assert len(asked) == 1 and asked[0].prompt == "哪个工单？" and asked[0].input_schema == SCHEMA
    assert isinstance(evs[-1], DoneEvent) and evs[-1].stop_reason == "awaiting"
    assert await _state(rid) == RunState.INPUT_REQUIRED
    log = await _log(rid)
    call_id = asked[0].call_id
    req = [e for e in log if e.type == RunEventType.INPUT_REQUESTED]
    assert req and req[0].payload == {"kind": "answer", "call_id": call_id, "prompt": "哪个工单？", "schema": SCHEMA}
    # 闸口前拦截：没有闸口决策、没有执行
    assert not [e for e in log if e.type in (RunEventType.TOOL_DECISION, RunEventType.TOOL_EXECUTING)]
    assert "request_user_input" in next(e for e in log if e.type == RunEventType.CONTEXT_ASSEMBLED).payload["tools"]

    with pytest.raises(user_input.InputForbidden):
        await user_input.provide_input(rid, "someone-else", "answer", call_id, {"id": "T-1"})
    with pytest.raises(ValueError, match="required"):
        await user_input.provide_input(rid, SUBJECT, "answer", call_id, {"x": 1})
    with pytest.raises(ValueError, match="shorter"):
        await user_input.provide_input(rid, SUBJECT, "answer", call_id, {"id": "T"})
    with pytest.raises(user_input.InputNotPending):
        await user_input.provide_input(rid, SUBJECT, "answer", "other-call", {"id": "T-1"})
    assert await _state(rid) == RunState.INPUT_REQUIRED  # 失败的提供不写任何事件

    out = await user_input.provide_input(rid, SUBJECT, "answer", call_id, {"id": "T-1001"})
    assert out == {"run_id": rid, "state": "working", "kind": "answer", "call_id": call_id}
    log = await _log(rid)
    assert _types(log)[-3:] == ["input.provided", "tool.result", "run.state_changed"]
    assert log[-2].payload["status"] == "ok" and log[-2].payload["output"] == {"id": "T-1001"}
    with pytest.raises(user_input.InputNotPending):  # 已回答
        await user_input.provide_input(rid, SUBJECT, "answer", call_id, {"id": "T-1001"})

    evs = await _collect(runner.drive_durable(rid))
    assert isinstance(evs[-1], DoneEvent) and evs[-1].stop_reason == "end_turn"
    assert await _state(rid) == RunState.COMPLETED


async def test_request_user_input_not_offered_without_config() -> None:
    agent = await _agent({"tools": ["echo"]})
    rid, _ = await _run(agent, '!tool request_user_input {"prompt": "x"}')
    await _collect(runner.drive_durable(rid))
    log = await _log(rid)
    res = [e for e in log if e.type == RunEventType.TOOL_RESULT]
    assert res[0].payload["output"] == {"error": "unknown tool"} and await _state(rid) == RunState.COMPLETED
    assert not [e for e in log if e.type == RunEventType.INPUT_REQUESTED]


async def test_request_user_input_bad_arguments_is_tool_error() -> None:
    agent = await _agent({"tools": ["echo"], "user_input": True})
    rid, _ = await _run(agent, '!tool request_user_input {"schema": "no"}')
    await _collect(runner.drive_durable(rid))
    res = [e for e in await _log(rid) if e.type == RunEventType.TOOL_RESULT]
    assert res[0].payload["status"] == "error" and "prompt" in res[0].payload["output"]["error"]
    assert await _state(rid) == RunState.COMPLETED


def test_validate_answer_subset() -> None:
    v = user_input.validate_answer
    v(None, "free text")
    with pytest.raises(ValueError):
        v(None, "  ")
    v({"type": "array", "items": {"type": "integer", "minimum": 1}, "maxItems": 2}, [1, 2])
    for bad in ([0], [1, 2, 3], ["1"], [True]):
        with pytest.raises(ValueError):
            v({"type": "array", "items": {"type": "integer", "minimum": 1}, "maxItems": 2}, bad)
    v({"enum": ["a", "b"]}, "a")
    with pytest.raises(ValueError):
        v({"enum": ["a", "b"]}, "c")


# ---------------- 写前登记：跨 Worker 去重 / 失租写回 / 对账 ----------------


def _rc(rid: str, agent: AgentVersion) -> RunCtx:
    spec = SessionSpec(
        run_id=rid,
        agent_id=agent.agent_id,
        agent_version=agent.version,
        model_alias=agent.model_alias,
        system_prompt="x",
        subject=SUBJECT,
        tenant_id=agent.tenant_id,
    )
    return RunCtx(
        run_id=rid,
        agent=agent,
        spec=spec,
        tools_by_name={"mail.send": LOCAL_TOOLS["mail.send"]},
        gate=GateContext(subject=SUBJECT, agent_id=agent.agent_id),
        history=[],
    )


class _SlowExec:
    """替换进程内开发执行器：计数，并在放行前阻塞（模拟慢写工具）。"""

    def __init__(self, fail_first: bool = False) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.fail_first = fail_first

    async def __call__(self, tool: Any, tool_input: dict[str, Any], dry_run: bool = False) -> Any:
        if dry_run:  # 影响预演不计入执行
            return {"impact_preview": {"n": 1}}
        self.calls += 1
        self.started.set()
        await self.release.wait()
        if self.fail_first and self.calls == 1:
            raise RuntimeError("connection reset mid-write")
        return {"sent": True, "n": self.calls}


async def _op(op_id: str) -> ToolOperation | None:
    async with db.SessionLocal() as s:
        return await s.get(ToolOperation, op_id)


async def test_same_operation_executes_once_across_two_executors(monkeypatch: pytest.MonkeyPatch) -> None:
    """两个执行者对同一调用：先登记者执行；后到者主键冲突、登记仍为 executing → 结果未知对账卡，绝不重复执行。"""
    agent = await _agent({"tools": ["mail.send"]})
    rid, _ = await _run(agent, "x")
    slow = _SlowExec()
    monkeypatch.setattr(toolbroker, "_dev_execute", slow)
    call = ToolUseEvent(call_id="c-dup", tool_name="mail.send", input={"to": "a@b"})
    async with db.SessionLocal() as s:
        log = EventLog(s)
        await log.transition(rid, RunState.WORKING, "start")
        await log.append(rid, RunEventType.TOOL_REQUESTED, {"call_id": "c-dup", "tool": "mail.send", "input": call.input})
        await s.commit()

    first = asyncio.create_task(_collect(handle_tool(_rc(rid, agent), call, already_logged=True)))
    await asyncio.wait_for(slow.started.wait(), 5)
    second = await _collect(handle_tool(_rc(rid, agent), call, already_logged=True))
    card = next(e for e in second if isinstance(e, CardEvent))
    assert card.card_type == CardType.RECONCILIATION_REQUIRED and card.payload["operation"]["status"] == "executing"
    slow.release.set()
    await first
    assert slow.calls == 1
    log = await _log(rid)
    executed = [e for e in log if e.type == RunEventType.TOOL_EXECUTED]
    assert len(executed) == 1
    op = await _op(str(executed[0].idempotency_key))
    assert op is not None and op.status == "executed" and op.result_hash and op.node


async def test_operation_already_executed_elsewhere_is_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    """登记行已是 executed（他人执行完成、TOOL_EXECUTED 未写进本 Run）：去重返回，不执行。"""
    agent = await _agent({"tools": ["mail.send"]})
    rid, _ = await _run(agent, "x")
    slow = _SlowExec()
    slow.release.set()
    monkeypatch.setattr(toolbroker, "_dev_execute", slow)
    call = ToolUseEvent(call_id="c-done", tool_name="mail.send", input={"to": "a@b"})
    key = (
        await evaluate(GateContext(subject=SUBJECT, agent_id=agent.agent_id), LOCAL_TOOLS["mail.send"], call.input, call.call_id)
    ).idempotency_key
    assert key
    async with db.SessionLocal() as s:
        log = EventLog(s)
        await log.transition(rid, RunState.WORKING, "start")
        await log.record_operation(rid, key, call.call_id, status="executed", node="other")
        await s.commit()
    evs = await _collect(handle_tool(_rc(rid, agent), call))
    assert slow.calls == 0
    assert evs[-1].output == {"deduplicated": True}  # type: ignore[union-attr]


async def test_lease_lost_writer_records_result_and_takeover_reconciles(monkeypatch: pytest.MonkeyPatch) -> None:
    """W1 执行写工具期间失租；W2 接管 → 对账卡（附登记状态）；W1 执行结果写回登记行，但写事件抛 LeaseLost；
    发起人对账 done → 续跑完成，全程只执行一次、只有一条 TOOL_EXECUTED。"""
    agent = await _agent({"tools": ["mail.send"]})
    rid, msgs = await _run(agent, '!tool mail.send {"to": "a@b"}')
    slow = _SlowExec()
    monkeypatch.setattr(toolbroker, "_dev_execute", slow)
    e1 = await acquire_lease(rid, "w1", 0.2)
    assert e1 is not None

    async def w1() -> list[Event]:
        with fence_scope(e1, rid):
            return await _collect(runner.drive(rid, agent, SUBJECT, msgs))

    t1 = asyncio.create_task(w1())
    await asyncio.wait_for(slow.started.wait(), 5)
    await asyncio.sleep(0.3)
    e2 = await acquire_lease(rid, "w2", 30)
    assert e2 is not None and e2 > e1
    with fence_scope(e2, rid):
        evs = await _collect(runner.drive(rid, agent, SUBJECT, msgs))
    card = next(e for e in evs if isinstance(e, CardEvent))
    assert card.card_type == CardType.RECONCILIATION_REQUIRED and card.payload["operation"]["status"] == "executing"
    assert card.payload["operation"]["epoch"] == e1
    assert await _state(rid) == RunState.INPUT_REQUIRED

    slow.release.set()
    with pytest.raises(LeaseLost):
        await t1
    op_id = card.payload["operation_id"]
    op = await _op(op_id)
    assert op is not None and op.status == "executed" and op.result_hash  # 失租旧 Worker 的结果写回登记行
    assert not [e for e in await _log(rid) if e.type == RunEventType.TOOL_EXECUTED]

    # 再次接管时对账卡带上已写回的状态
    with fence_scope(e2, rid):
        async with db.SessionLocal() as s:
            await EventLog(s).transition(rid, RunState.WORKING, "test")  # 模拟仍在 working 时被另一 Worker 驱动
            await s.commit()
        evs = await _collect(runner.drive(rid, agent, SUBJECT, msgs))
    card = next(e for e in evs if isinstance(e, CardEvent))
    assert card.payload["operation"]["status"] == "executed"

    with pytest.raises(user_input.InputNotPending):
        await user_input.provide_input(rid, SUBJECT, "reconcile", "wrong-call", action="done")
    await user_input.provide_input(rid, SUBJECT, "reconcile", card.payload["call_id"], action="done")
    with fence_scope(e2, rid):
        evs = await _collect(runner.drive(rid, agent, SUBJECT, msgs))
    assert isinstance(evs[-1], DoneEvent) and evs[-1].stop_reason == "end_turn"
    log = await _log(rid)
    executed = [e for e in log if e.type == RunEventType.TOOL_EXECUTED]
    assert len(executed) == 1 and executed[0].idempotency_key == op_id and slow.calls == 1
    seqs = [e.seq for e in log]
    assert seqs == list(range(1, len(seqs) + 1))


async def test_reconcile_skip_and_retry_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """skip 补写 RESULT(error) 后续跑完成；retry 对未声明幂等的工具拒绝（ValueError）。"""
    agent = await _agent({"tools": ["mail.send"]})
    rid, msgs = await _run(agent, '!tool mail.send {"to": "a@b"}')
    slow = _SlowExec(fail_first=True)
    slow.release.set()
    monkeypatch.setattr(toolbroker, "_dev_execute", slow)
    evs = await _collect(runner.drive(rid, agent, SUBJECT, msgs))
    card = next(e for e in evs if isinstance(e, CardEvent))
    op = await _op(card.payload["operation_id"])
    assert op is not None and op.status == "unknown"
    with pytest.raises(ValueError, match="idempotency"):
        await user_input.provide_input(rid, SUBJECT, "reconcile", card.payload["call_id"], action="retry")
    with pytest.raises(ValueError, match="action"):
        await user_input.provide_input(rid, SUBJECT, "reconcile", card.payload["call_id"], action="redo")
    await user_input.provide_input(rid, SUBJECT, "reconcile", card.payload["call_id"], action="skip")
    op = await _op(card.payload["operation_id"])
    assert op is not None and op.status == "skipped"
    evs = await _collect(runner.drive(rid, agent, SUBJECT, msgs))
    assert isinstance(evs[-1], DoneEvent) and evs[-1].stop_reason == "end_turn" and slow.calls == 1


async def test_reconcile_retry_reexecutes_idempotent_tool_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """下游声明幂等（requires_idempotency_key）的写工具：retry → 续跑时认领登记行、以同一幂等键重做一次。"""
    agent = await _agent({"tools": ["ticket.close_batch"]})
    rid, msgs = await _run(agent, '!tool ticket.close_batch {"ids": ["T-1"]}')
    slow = _SlowExec(fail_first=True)
    slow.release.set()
    monkeypatch.setattr(toolbroker, "_dev_execute", slow)
    await _collect(runner.drive(rid, agent, SUBJECT, msgs))
    assert await _state(rid) == RunState.AUTH_REQUIRED and slow.calls == 0  # 高风险写先审批
    log = await _log(rid)
    decision = next(e for e in log if e.type == RunEventType.TOOL_DECISION)
    async with db.SessionLocal() as s:
        el = EventLog(s)
        await el.append(
            rid,
            RunEventType.TOOL_APPROVED,
            {
                "call_id": decision.payload["call_id"],
                "approver": "boss",
                "params_hash": decision.payload["params_hash"],
                "expires_at": "2999-01-01T00:00:00+00:00",
            },
        )
        await el.transition(rid, RunState.WORKING, "approved")
        await s.commit()
    evs = await _collect(runner.drive(rid, agent, SUBJECT, msgs))
    card = next(e for e in evs if isinstance(e, CardEvent))
    assert card.card_type == CardType.RECONCILIATION_REQUIRED and slow.calls == 1
    await user_input.provide_input(rid, SUBJECT, "reconcile", card.payload["call_id"], action="retry")
    op = await _op(card.payload["operation_id"])
    assert op is not None and op.status == "retry"
    evs = await _collect(runner.drive(rid, agent, SUBJECT, msgs))
    assert isinstance(evs[-1], DoneEvent) and evs[-1].stop_reason == "end_turn"
    assert slow.calls == 2
    log = await _log(rid)
    executed = [e for e in log if e.type == RunEventType.TOOL_EXECUTED]
    assert len(executed) == 1 and executed[0].idempotency_key == card.payload["operation_id"]
    op = await _op(card.payload["operation_id"])
    assert op is not None and op.status == "executed"


# ---------------- 故障注入：MOCK_TURN_DELAY_S 与 active.interrupt ----------------


async def test_mock_turn_delay_is_interruptible(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCK_TURN_DELAY_S", "20")
    agent = await _agent({"tools": ["echo"]})
    rid, msgs = await _run(agent, "hello")
    assert await active.interrupt("no-such-run") is False
    task = asyncio.create_task(_collect(runner.drive(rid, agent, SUBJECT, msgs)))
    for _ in range(200):
        if active.get(rid) is not None:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    assert await active.interrupt(rid) is True
    evs = await asyncio.wait_for(task, 5)
    assert loop.time() - t0 < 2
    assert isinstance(evs[-1], DoneEvent) and evs[-1].stop_reason == "cancelled"
    assert await _state(rid) == RunState.CANCELED


# ---------------- 联调：WP-B 真实 Worker + drive_durable + mock harness ----------------


async def test_worker_drives_durable_run_through_user_input_to_completion() -> None:
    """入队 → 真实 Worker（fakeredis 队列）抢租约、在 fence 内调用 drive_durable → request_user_input 挂起 →
    provide_input → 再入队 → 续跑完成；租约已释放，事件 seq 连续。"""
    fakeredis = pytest.importorskip("fakeredis")
    from sqlalchemy import update

    from runtime.state.eventlog import lease_info
    from runtime.worker.queue import RedisStreamQueue
    from runtime.worker.settings import WorkerSettings
    from runtime.worker.worker import Worker

    ws = WorkerSettings(
        redis_url=None,
        role="all",
        concurrency=2,
        lease_ttl_s=5,
        heartbeat_s=0.1,
        claim_idle_s=60,
        claim_interval_s=60,
        reaper_s=1e6,
        max_deliveries=5,
        isolate_grace_s=0.3,
    )
    agent = await _agent({"tools": ["echo"], "user_input": True})
    rid, _ = await _run(agent, '!tool request_user_input {"prompt": "继续吗？", "schema": {"type": "boolean"}}')
    async with db.SessionLocal() as s:
        await s.execute(update(db.Run).where(db.Run.run_id == rid).values(durable=True))
        await s.commit()
    q = RedisStreamQueue(fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer(), decode_responses=True))
    worker = asyncio.create_task(Worker(q, ws).run())

    async def until_state(state: RunState) -> None:
        for _ in range(500):
            if await _state(rid) == state and (await lease_info(rid))[0] is None:
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"run did not reach {state}: {await _state(rid)}")

    try:
        await q.enqueue(rid, "created")
        await until_state(RunState.INPUT_REQUIRED)
        call_id = next(e for e in await _log(rid) if e.type == RunEventType.INPUT_REQUESTED).payload["call_id"]
        with pytest.raises(ValueError):
            await user_input.provide_input(rid, SUBJECT, "answer", call_id, "yes")
        await user_input.provide_input(rid, SUBJECT, "answer", call_id, True)
        await q.enqueue(rid, "input")
        await until_state(RunState.COMPLETED)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    log = await _log(rid)
    assert [e.seq for e in log] == list(range(1, len(log) + 1))
    assert (await lease_info(rid))[1] == 2  # 两次投递各抢一次租约

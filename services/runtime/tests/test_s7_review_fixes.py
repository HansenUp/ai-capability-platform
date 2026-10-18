"""S7 独立评审修复的回归用例。

- #1 丢唤醒：持有者挂起并提交后、释放租约前 /input 把 Run 迁回 working 并入队（该投递因租约仍被持有被 ACK 跳过），
  释放租约时补投，Run 最终完成；InMemory 模式也周期运行 reaper。
- #2 内联执行失租后从本次已提交的 seq 起尾随，不重复下发已下发的输出。
- #5 失租中断只命中本 epoch 的会话；失租中断导致的 harness cancelled 按 LeaseLost 处理，不把 Run 错迁 canceled。
- #6 并发审批 / 拒绝的 StaleWriter 统一 409。
- #8 心跳续期卡住超过 TTL：主动置 lost 并中断。
- #9 毒消息收尾须先抢到租约，并删除存储的凭据；快照缺失失败同样删凭据。
- #10 写操作幂等键（operation_id）以 Run 限定作用域。
- #11 request_user_input 的 schema 大小 / 深度上限，回答嵌套过深 400。
- #12 /v1/messages 创建 Run 即同事务持租。
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
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
    TurnInput,
)
from runtime import credentials
from runtime.api import messages
from runtime.assemble.assembler import LOCAL_TOOLS
from runtime.guard.gate import IDEMPOTENCY_KEY_MAX, GateContext, evaluate
from runtime.harness import registry
from runtime.loop import active, runner, user_input
from runtime.run import invariant
from runtime.state import db
from runtime.state.db import AgentVersion, RunCredential, RunInput
from runtime.state.eventlog import (
    EventLog,
    LeaseLost,
    SeqConflict,
    acquire_lease,
    expired_unleased_runs,
    fence_scope,
    lease_info,
)
from runtime.worker import control, queue
from runtime.worker import lease as lease_mod
from runtime.worker.queue import InMemoryQueue
from runtime.worker.settings import WorkerSettings
from runtime.worker.worker import Worker

SUBJECT = "u-s7fix"
_KEY = "test-signing-key-0123456789abcdef"  # noqa: S105 —— 测试签名密钥（不验签，只用于构造 exp）

WS = WorkerSettings(
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


def _token(ttl_s: float = 3600) -> str:
    return jwt.encode({"sub": SUBJECT, "exp": int(time.time() + ttl_s), "nonce": uuid.uuid4().hex}, _KEY, algorithm="HS256")


async def _agent(config: dict[str, Any], harness: str | None = None) -> AgentVersion:
    av = AgentVersion(
        agent_id=f"s7f-{uuid.uuid4().hex[:8]}", version="1", status="published", system_prompt="x", config=config, harness=harness
    )
    async with db.SessionLocal() as s:
        s.add(av)
        await s.commit()
    return av


async def _durable_run(agent: AgentVersion, content: str = "hi", token: str | None = None) -> str:
    msgs = [{"role": "user", "content": content}]
    return await runner.create_run(agent, SUBJECT, None, initial_messages=msgs, durable=True, credential=token)


async def _log(rid: str) -> list[RunEvent]:
    async with db.SessionLocal() as s:
        return await EventLog(s).list(rid)


async def _run_row(rid: str) -> db.Run:
    async with db.SessionLocal() as s:
        run = await s.get(db.Run, rid)
    assert run is not None
    return run


async def _until(pred: Any, within: float = 5.0) -> None:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if await pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


# ---------------- #1 丢唤醒 ----------------


async def test_input_while_lease_held_is_redelivered_on_release() -> None:
    """持有者把 Run 迁到 input-required 并提交后、释放租约前：发起人回答（Run 迁回 working）并入队——这条投递抢不到租约被
    ACK 跳过；持有者释放租约时发现本 fence 之后有他人写入且 Run 可执行，补投（reason=released），Run 最终完成。"""
    agent = await _agent({"tools": ["echo"], "user_input": True})
    rid = await _durable_run(agent, '!tool request_user_input {"prompt": "继续吗？", "schema": {"type": "boolean"}}')
    suspended, release_gate = asyncio.Event(), asyncio.Event()
    drives: list[int] = []

    async def drive(run_id: str) -> AsyncIterator[Any]:
        if run_id != rid:
            return
        drives.append(1)
        async for ev in runner.drive_durable(run_id):
            yield ev
        if len(drives) == 1:  # 第一次驱动：挂起后先不返回（仍持租约），等测试在此期间提交回答并入队
            suspended.set()
            await release_gate.wait()

    q = InMemoryQueue()
    saved = (queue._queue, queue._redis)
    queue.configure(q)  # 补投走进程级队列：指向本用例的队列（会话级 TestClient 的 Worker 消费的是另一个对象）
    worker = asyncio.create_task(Worker(q, WS, drive=drive).run())
    try:
        await q.enqueue(rid, "created")
        await asyncio.wait_for(suspended.wait(), 10)
        assert (await _run_row(rid)).state == RunState.INPUT_REQUIRED
        assert (await lease_info(rid))[0] is not None  # 仍持有租约
        call_id = next(e for e in await _log(rid) if e.type == RunEventType.INPUT_REQUESTED).payload["call_id"]
        await user_input.provide_input(rid, SUBJECT, "answer", call_id, True)
        await q.enqueue(rid, "input")
        await _until(lambda: _true(len(q) == 0))
        await asyncio.sleep(0.2)  # "input" 投递已被处理：抢不到租约，ACK 跳过
        assert len(drives) == 1
        release_gate.set()
        await _until(lambda: _state_is(rid, RunState.COMPLETED), 10)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        queue.configure(*saved)
    assert len(drives) == 2
    log = await _log(rid)
    assert [e.seq for e in log] == list(range(1, len(log) + 1))


async def test_inmemory_worker_runs_periodic_reaper() -> None:
    """InMemory 模式（无 XAUTOCLAIM）也周期运行 reaper：启动之后才出现的、durable 且无有效租约的 working Run 会被重投执行。"""
    driven: list[str] = []
    ws = WorkerSettings(**{**WS.__dict__, "reaper_s": 0.1})
    q = InMemoryQueue()

    async def drive(run_id: str) -> AsyncIterator[Any]:
        driven.append(run_id)
        return
        yield

    worker = asyncio.create_task(Worker(q, ws, drive=drive).run())
    try:
        await asyncio.sleep(0.2)  # 启动时的一次扫描已过
        rid = f"s7f-{uuid.uuid4().hex[:16]}"
        async with db.SessionLocal() as s:
            s.add(db.Run(run_id=rid, agent_id="demo", agent_version="1", model_alias="m", subject="u", state="working", durable=True))
            await s.commit()
        await _until(lambda: _true(rid in driven), 5)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await _retire(rid)  # 不留给其他用例的 reaper


async def _true(v: bool) -> bool:
    return v


async def _retire(rid: str) -> None:
    """用例结束时把仍可执行的持久化 Run 置为终态：不留给后续用例里 Worker 启动时的 reaper 扫描。"""
    async with db.SessionLocal() as s:
        await s.execute(update(db.Run).where(db.Run.run_id == rid).values(state="canceled", lease_owner=None, lease_expires_at=None))
        await s.commit()


async def _state_is(rid: str, state: RunState) -> bool:
    return (await _run_row(rid)).state == state.value


# ---------------- #2 内联执行失租后从已写 seq 起尾随 ----------------


async def test_inline_fallback_tails_from_committed_seq_without_duplicates() -> None:
    """内联执行写入模型响应 A（已下发）后租约被他人接管，他人写入 B 并完成；内联写入随即 LeaseLost，转为尾随——
    尾随自本次已提交的 seq 之后开始，A 不会再以日志行重复下发。"""
    agent = await _agent({"tools": ["echo"]})
    rid = await _durable_run(agent)
    seq_a: list[int] = []

    async def takeover() -> None:  # 他人（无 fence 上下文）：抢走租约并把 Run 跑完
        async with db.SessionLocal() as s:
            await s.execute(update(db.Run).where(db.Run.run_id == rid).values(lease_owner="other", lease_epoch=db.Run.lease_epoch + 1))
            log = EventLog(s)
            await log.append(rid, RunEventType.MODEL_RESPONSE, {"text": "B"})
            await log.append(rid, RunEventType.RUN_DONE, {"stop_reason": "end_turn"})
            await log.transition(rid, RunState.COMPLETED, "end_turn")
            await s.commit()

    async def make_drive() -> AsyncIterator[Event]:
        async with db.SessionLocal() as s:
            log = EventLog(s)
            await log.transition(rid, RunState.WORKING, "start")
            seq_a.append((await log.append(rid, RunEventType.MODEL_RESPONSE, {"text": "A"})).seq)
            await s.commit()
        yield TextEvent(delta="A")
        await asyncio.create_task(takeover(), context=contextvars.Context())
        async with db.SessionLocal() as s:
            await EventLog(s).append(rid, RunEventType.MODEL_RESPONSE, {"text": "A2"})  # LeaseLost
            await s.commit()
        yield TextEvent(delta="never")

    items = [x async for x in messages.run_or_tail(rid, make_drive)]
    texts = [x.delta for x in items if isinstance(x, TextEvent)]
    tailed = [x for x in items if isinstance(x, RunEvent)]
    assert texts == ["A"]
    assert tailed and all(e.seq > seq_a[0] for e in tailed)
    assert [e.payload["text"] for e in tailed if e.type == RunEventType.MODEL_RESPONSE] == ["B"]
    assert isinstance(items[-1], DoneEvent) and items[-1].stop_reason == "end_turn"


# ---------------- #5 失租中断按 epoch ----------------


class _Session:
    pass


class _RecHarness:
    def __init__(self) -> None:
        self.interrupted = 0

    async def interrupt(self, session: Any, run_id: str) -> None:
        self.interrupted += 1


async def test_lease_lost_interrupt_spares_new_holder_in_same_process() -> None:
    """同进程内旧持有者（epoch 1）失租、新持有者（epoch 2）已登记：按 epoch 中断只命中旧会话，不误伤新持有者。"""
    rid = f"s7f-{uuid.uuid4().hex[:12]}"
    old_h, new_h = _RecHarness(), _RecHarness()
    old_s, new_s = _Session(), _Session()
    active.register(rid, old_h, old_s, 1)  # type: ignore[arg-type]
    active.register(rid, new_h, new_s, 2)  # type: ignore[arg-type]  # 同一 run_id 后登记者覆盖
    try:
        assert await control.interrupt_local(rid, epoch=1) is False
        assert new_h.interrupted == 0 and active.interrupt_reason(rid, new_s) is None  # type: ignore[arg-type]
        assert await control.interrupt_local(rid, epoch=2) is True
        assert new_h.interrupted == 1 and active.interrupt_reason(rid, new_s) == active.REASON_LEASE_LOST  # type: ignore[arg-type]
    finally:
        active.unregister(rid, new_s)  # type: ignore[arg-type]


class _BlockUntilInterrupt:
    """run_turn 阻塞到被 interrupt，然后以 done(cancelled) 收尾。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.entered = asyncio.Event()
        self._wake: dict[str, asyncio.Event] = {}

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(name=self.name, version="test", transport="in-process", loop_owner="platform")

    async def open_session(self, spec: SessionSpec) -> SessionHandle:
        sid = uuid.uuid4().hex
        self._wake[sid] = asyncio.Event()
        return SessionHandle(session_id=sid, harness=self.name)

    async def resume(self, spec: SessionSpec, events: list[Event]) -> SessionHandle:
        return await self.open_session(spec)

    async def run_turn(
        self, session: SessionHandle, turn: TurnInput, tools: list[ToolSpec], skills: list[SkillDir], hooks: HookSet
    ) -> AsyncIterator[Event]:
        self.entered.set()
        await self._wake[session.session_id].wait()
        yield DoneEvent(run_id="x", stop_reason="cancelled")

    async def interrupt(self, session: SessionHandle, run_id: str) -> None:
        ev = self._wake.get(session.session_id)
        if ev is not None:
            ev.set()


async def test_lease_lost_interrupt_is_not_mapped_to_canceled() -> None:
    """失租隔离中断了 harness（done(cancelled)）而 Run 并未被取消：runner 抛 LeaseLost，不写 RUN_DONE、不迁 canceled。"""
    name = f"s7f-block-{uuid.uuid4().hex[:6]}"
    harness = _BlockUntilInterrupt(name)
    registry.register(name, harness)  # type: ignore[arg-type]
    agent = await _agent({}, harness=name)
    rid = await _durable_run(agent)
    epoch = await acquire_lease(rid, "me", 30)
    assert epoch is not None

    async def drive() -> list[Event]:
        with fence_scope(epoch, rid):
            return [ev async for ev in runner.drive_durable(rid)]

    task = asyncio.create_task(drive())
    await asyncio.wait_for(harness.entered.wait(), 10)
    assert await control.interrupt_local(rid, epoch=epoch + 1) is False  # 其他纪元：不命中
    assert await control.interrupt_local(rid, epoch=epoch) is True
    with pytest.raises(LeaseLost):
        await asyncio.wait_for(task, 10)
    assert (await _run_row(rid)).state == RunState.WORKING
    assert not [e for e in await _log(rid) if e.type == RunEventType.RUN_DONE]
    await _retire(rid)


# ---------------- #6 并发审批 / 拒绝 → 409 ----------------


def test_concurrent_approve_or_reject_maps_stale_writer_to_409(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    msgs = [{"role": "user", "content": '!tool ticket.close_batch {"ids":["T-1"]}'}]
    with client.stream("POST", "/v1/messages", json={"agent_id": "demo", "messages": msgs}) as r:
        run_id = r.headers["x-run-id"]
        evs = [json.loads(line[5:]) for line in r.iter_lines() if line.startswith("data:")]
    card = next(e for e in evs if e["type"] == "card")
    body = {"call_id": card["payload"]["call_id"], "params_hash": card["payload"]["params_hash"]}
    original = EventLog.append

    async def racing(self: EventLog, *a: Any, **kw: Any) -> Any:  # 另一位审批人抢先推进了 last_seq
        raise SeqConflict("concurrent approver")

    monkeypatch.setattr(EventLog, "append", racing)
    assert client.post(f"/v1/runs/{run_id}/approve", json=body).status_code == 409
    assert client.post(f"/v1/runs/{run_id}/reject", json=body).status_code == 409
    monkeypatch.setattr(EventLog, "append", original)
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "auth-required"


# ---------------- #8 心跳超时 ----------------


async def test_renew_hanging_beyond_ttl_isolates(monkeypatch: pytest.MonkeyPatch) -> None:
    """续期调用卡住（数据库不可达）：距上次续期成功超过 TTL 即主动置 lost，并只中断本 epoch 的会话。"""
    rid = f"s7f-{uuid.uuid4().hex[:16]}"
    async with db.SessionLocal() as s:
        s.add(db.Run(run_id=rid, agent_id="demo", agent_version="1", model_alias="m", subject="u", state="working", durable=False))
        await s.commit()

    async def hang(*_: Any) -> bool:
        await asyncio.Event().wait()
        return True

    monkeypatch.setattr(lease_mod, "renew_lease", hang)
    ws = WorkerSettings(**{**WS.__dict__, "lease_ttl_s": 0.4, "heartbeat_s": 0.1})
    h, sess = _RecHarness(), _Session()
    t0 = time.monotonic()
    async with lease_mod.hold(rid, "me", ws=ws) as held:
        assert held is not None
        active.register(rid, h, sess, held.epoch)  # type: ignore[arg-type]
        try:
            await asyncio.wait_for(held.lost.wait(), 3)
        finally:
            active.unregister(rid, sess)  # type: ignore[arg-type]
    assert 0.35 <= time.monotonic() - t0 < 2.5
    assert h.interrupted == 1


# ---------------- #9 终态凭据 ----------------


async def test_poisoned_run_needs_lease_and_deletes_credential() -> None:
    agent = await _agent({"tools": ["echo"]})
    rid = await _durable_run(agent, token=_token())
    epoch = await acquire_lease(rid, "busy-holder", 30)
    assert epoch is not None
    assert await queue.fail_poisoned(rid, 6) is False  # 他人持有有效租约：不越权判死
    assert (await _run_row(rid)).state == RunState.SUBMITTED
    async with db.SessionLocal() as s:
        await s.execute(update(db.Run).where(db.Run.run_id == rid).values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)))
        await s.commit()
    assert await queue.fail_poisoned(rid, 6) is True
    run = await _run_row(rid)
    assert run.state == RunState.FAILED and run.lease_owner is None
    assert await credentials.load_credential(rid) is None
    errs = [e.payload for e in await _log(rid) if e.type == RunEventType.RUN_ERROR]
    assert errs[-1]["code"] == queue.POISONED


async def test_snapshot_missing_failure_deletes_credential() -> None:
    agent = await _agent({"tools": ["echo"]})
    rid = await _durable_run(agent, token=_token())
    async with db.SessionLocal() as s:
        stored = await s.get(RunInput, rid)
        await s.delete(stored)
        await s.commit()
    evs = [ev async for ev in runner.drive_durable(rid)]
    assert isinstance(evs[-1], DoneEvent) and evs[-1].stop_reason == "error"
    async with db.SessionLocal() as s:
        assert await s.get(RunCredential, rid) is None


# ---------------- #10 幂等键以 Run 限定作用域 ----------------


async def test_idempotency_key_is_scoped_to_run() -> None:
    tool = LOCAL_TOOLS["mail.send"]
    args = {"to": "ops@example.com"}
    a = await evaluate(GateContext(subject=SUBJECT, agent_id="agent-x", run_id="run_a"), tool, args, "c1")
    b = await evaluate(GateContext(subject=SUBJECT, agent_id="agent-x", run_id="run_b"), tool, args, "c1")
    again = await evaluate(GateContext(subject=SUBJECT, agent_id="agent-x", run_id="run_a"), tool, args, "c1")
    assert a.idempotency_key and a.idempotency_key.startswith("run_a:c1:")
    assert a.idempotency_key == again.idempotency_key  # 同一 Run 内重放 / 接管：同一个键
    assert a.idempotency_key != b.idempotency_key  # 不同 Run 的同名调用：不同的操作
    long = await evaluate(GateContext(subject=SUBJECT, agent_id="agent-x", run_id="run_" + "f" * 20), tool, args, "x" * 200)
    assert long.idempotency_key and len(long.idempotency_key) <= IDEMPOTENCY_KEY_MAX


# ---------------- #11 request_user_input 上限 ----------------


def test_request_user_input_schema_limits_and_deep_answer() -> None:
    deep: dict[str, Any] = {"type": "string"}
    for _ in range(9):
        deep = {"type": "object", "properties": {"x": deep}}
    with pytest.raises(ValueError, match="deeper"):
        user_input.parse_request({"prompt": "q", "schema": deep})
    big = {"type": "string", "enum": ["v" * 100 for _ in range(200)]}
    with pytest.raises(ValueError, match="larger"):
        user_input.parse_request({"prompt": "q", "schema": big})
    assert user_input.parse_request({"prompt": "q", "schema": {"type": "object"}}) == ("q", {"type": "object"})
    value: Any = 1
    for _ in range(5000):
        value = [value]
    assert user_input.json_depth(value) == 5000  # 迭代实现，不触发 RecursionError


def test_deeply_nested_answer_is_400(client: Any) -> None:
    from runtime.api import messages as api_messages  # noqa: F401 —— 确保路由已加载

    agent_cfg = {"tools": ["echo"], "user_input": True}
    name = f"s7f-ask-{uuid.uuid4().hex[:6]}"
    assert client.post("/admin/agents", json={"agent_id": name, "version": "1", "config": agent_cfg}).status_code == 201
    assert client.post(f"/admin/agents/{name}/versions/1/publish", json={"eval_passed": True}).status_code == 200
    ask = '!tool request_user_input {"prompt": "随便说"}'
    with client.stream("POST", "/v1/messages", json={"agent_id": name, "messages": [{"role": "user", "content": ask}]}) as r:
        run_id = r.headers["x-run-id"]
        evs = [json.loads(line[5:]) for line in r.iter_lines() if line.startswith("data:")]
    call_id = next(e for e in evs if e["type"] == "input_required")["call_id"]
    value: Any = "x"
    for _ in range(40):
        value = {"a": value}
    r2 = client.post(f"/v1/runs/{run_id}/input", json={"kind": "answer", "call_id": call_id, "value": value})
    assert r2.status_code == 400 and "nested" in r2.json()["detail"]


# ---------------- #12 /v1/messages 创建即持租 ----------------


async def test_create_run_with_lease_owner_is_leased_in_same_transaction() -> None:
    agent = await _agent({"tools": ["echo"]})
    msgs = [{"role": "user", "content": "hi"}]
    rid = await runner.create_run(agent, SUBJECT, None, initial_messages=msgs, durable=True, lease_owner="inline-node")
    owner, epoch, expires = await lease_info(rid)
    assert (owner, epoch) == ("inline-node", runner.FIRST_LEASE_EPOCH) and expires is not None
    assert rid not in await expired_unleased_runs(limit=1000)  # reaper 看不到无租约窗口
    assert await acquire_lease(rid, "worker", 30) is None  # Worker 抢不走
    async with db.SessionLocal() as s:
        stored = await s.get(RunInput, rid)
    assert stored is not None and stored.fingerprint == invariant.message_hash(msgs)
    async with lease_mod.hold(rid, "inline-node", epoch=runner.FIRST_LEASE_EPOCH) as held:  # 内联执行直接接管该租约
        assert held is not None and held.epoch == runner.FIRST_LEASE_EPOCH
    assert (await lease_info(rid))[0] is None
    await _retire(rid)

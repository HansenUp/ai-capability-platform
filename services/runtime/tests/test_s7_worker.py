"""S7 WP-B 队列与 Worker：fakeredis + 会话 SQLite。

覆盖：入队→领取→执行→ACK；重复投递只有一个租约持有者；租约过期接管（TTL 1 s、冻结心跳模拟节点死亡）epoch 递增
且旧持有者写入抛 LeaseLost；续期失败自我隔离；死信；reaper 重投；跨节点取消转发与心跳兜底；InMemory 模式。
runner.drive_durable（WP-C）用桩代替：桩按真实 runner 的方式经事件日志写入，因此 fence 行为是真实的。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import update

from ai_contract import RunEventType, RunState
from runtime.loop import active
from runtime.state import db
from runtime.state.db import Run
from runtime.state.eventlog import EventLog, LeaseLost, acquire_lease, current_fence, lease_info
from runtime.worker import control
from runtime.worker import queue as queue_mod
from runtime.worker.queue import DEAD_STREAM, GROUP, STREAM, Delivery, InMemoryQueue, RedisStreamQueue
from runtime.worker.settings import WorkerSettings, problems
from runtime.worker.worker import Worker, start_background

fakeredis = pytest.importorskip("fakeredis")

WS = WorkerSettings(
    redis_url=None,
    role="all",
    concurrency=4,
    lease_ttl_s=5,
    heartbeat_s=0.1,
    claim_idle_s=60,
    claim_interval_s=60,
    reaper_s=1e6,  # 周期 reaper 关闭（会扫到其他用例遗留的持久化 Run），reaper 用例显式调用 reap_once
    max_deliveries=5,
    isolate_grace_s=0.3,
)


@pytest.fixture
def server() -> Any:
    return fakeredis.FakeServer()


def _client(server: Any) -> Any:
    return fakeredis.FakeAsyncRedis(server=server, decode_responses=True)


class SpyQueue(RedisStreamQueue):
    """记录 ACK 的 RedisStreamQueue。"""

    def __init__(self, client: Any) -> None:
        super().__init__(client)
        self.acked: list[str] = []

    async def ack(self, delivery: Delivery) -> None:
        self.acked.append(delivery.run_id)
        await super().ack(delivery)


class FakeHarness:
    """只实现 interrupt 的 harness 替身：记录中断，并可在中断时唤醒被阻塞的 drive。"""

    def __init__(self, on_interrupt: asyncio.Event | None = None) -> None:
        self.interrupted: list[str] = []
        self.on_interrupt = on_interrupt

    async def interrupt(self, session: Any, run_id: str) -> None:
        self.interrupted.append(run_id)
        if self.on_interrupt is not None:
            self.on_interrupt.set()


class StubDrive:
    """drive_durable 的桩：submitted→working，按需阻塞在 gate 上，然后写一条模型响应并迁到 completed。
    阻塞期间登记到 loop.active（供中断）；醒来发现 Run 已 canceled 则像 runner 一样直接收尾。"""

    def __init__(self, runs: set[str], gates: list[asyncio.Event | None] | None = None, harness: FakeHarness | None = None) -> None:
        self.runs = runs
        self.gates = list(gates or [])
        self.harness = harness
        self.calls: list[tuple[str, int | None]] = []
        self.errors: list[BaseException] = []

    async def __call__(self, run_id: str) -> AsyncIterator[Any]:
        if run_id not in self.runs:
            return
        self.calls.append((run_id, current_fence(run_id)))
        gate = self.gates.pop(0) if self.gates else None
        session = object()
        try:
            async with db.SessionLocal() as s:
                await EventLog(s).transition(run_id, RunState.WORKING, "start")
                await s.commit()
            yield {"type": "started"}
            if gate is not None:
                if self.harness is not None:
                    active.register(run_id, self.harness, session, current_fence(run_id))  # type: ignore[arg-type]
                await gate.wait()
            async with db.SessionLocal() as s:
                run = await s.get(Run, run_id)
                assert run is not None
                if run.state == RunState.CANCELED.value:
                    return
                log = EventLog(s)
                await log.append(run_id, RunEventType.MODEL_RESPONSE, {"text": "ok"})
                await log.transition(run_id, RunState.COMPLETED, "end_turn")
                await s.commit()
            yield {"type": "done"}
        except BaseException as e:
            self.errors.append(e)
            raise
        finally:
            active.unregister(run_id, session)  # type: ignore[arg-type]


async def _new_run(*, durable: bool = True, state: RunState = RunState.SUBMITTED) -> str:
    rid = f"s7w-{uuid4().hex[:16]}"
    async with db.SessionLocal() as s:
        s.add(Run(run_id=rid, agent_id="demo", agent_version="1", model_alias="m", subject="u", state=state.value, durable=durable))
        await s.commit()
    return rid


async def _run_row(run_id: str) -> Run:
    async with db.SessionLocal() as s:
        run = await s.get(Run, run_id)
    assert run is not None
    return run


async def _events(run_id: str) -> list[Any]:
    async with db.SessionLocal() as s:
        return await EventLog(s).list(run_id)


async def _pending(client: Any) -> int:
    return int((await client.xpending(STREAM, GROUP))["pending"])


async def _until(cond: Callable[[], Awaitable[bool]], within: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + within
    while not await cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


@contextlib.asynccontextmanager
async def _running(*workers: Worker) -> AsyncIterator[list[asyncio.Task[None]]]:
    tasks = [asyncio.create_task(w.run()) for w in workers]
    try:
        yield tasks
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _state(run_id: str) -> str:
    return (await _run_row(run_id)).state


# ---- 基本流程 ----


async def test_enqueue_claim_execute_ack(server: Any) -> None:
    """入队 → XREADGROUP 领取 → 抢租约（epoch 1）并在 fence 内驱动 → 释放租约 → XACK。"""
    client = _client(server)
    q = RedisStreamQueue(client)
    rid = await _new_run()
    drive = StubDrive({rid})
    await q.enqueue(rid, "created")
    async with _running(Worker(q, WS, drive=drive)):

        async def done() -> bool:
            return await _state(rid) == RunState.COMPLETED.value and await _pending(client) == 0

        await _until(done)
    assert drive.calls == [(rid, 1)]  # drive 处于 fence_scope(epoch=1, run_id) 内
    assert await lease_info(rid) == (None, 1, None)  # 已释放，epoch 单调不回退
    assert [e.seq for e in await _events(rid)] == [1, 2, 3]


async def test_duplicate_delivery_single_lease_holder(server: Any) -> None:
    """同一 Run 投递两次、两个 Worker 并发领取：只有一个抢到租约并执行，另一个不处理直接 ACK。"""
    client = _client(server)
    q1, q2 = SpyQueue(client), SpyQueue(_client(server))
    rid = await _new_run()
    gate = asyncio.Event()
    drive = StubDrive({rid}, gates=[gate])
    await q1.enqueue(rid, "created")
    await q1.enqueue(rid, "created")
    async with _running(Worker(q1, WS, drive=drive), Worker(q2, WS, drive=drive)):

        async def duplicate_acked() -> bool:
            return len(drive.calls) == 1 and len(q1.acked) + len(q2.acked) == 1

        await _until(duplicate_acked)  # 持有者仍在执行时，重复消息已被 ACK 跳过
        gate.set()

        async def done() -> bool:
            return await _state(rid) == RunState.COMPLETED.value and await _pending(client) == 0

        await _until(done)
    assert len(drive.calls) == 1
    assert sorted(q1.acked + q2.acked) == [rid, rid]


# ---- 接管与隔离 ----


async def test_lease_expiry_takeover_bumps_epoch(server: Any) -> None:
    """节点 A 领取后心跳冻结（模拟被杀）：TTL 1 s 过期、消息空闲超时后 B 经 XAUTOCLAIM 接管，epoch 1→2，
    B 完成 Run；A 恢复后写入抛 LeaseLost、不 ACK；事件 seq 连续。"""
    client = _client(server)
    qa, qb = SpyQueue(client), SpyQueue(_client(server))
    rid = await _new_run()
    gate_a = asyncio.Event()
    drive = StubDrive({rid}, gates=[gate_a])
    ws_a = dataclasses.replace(WS, lease_ttl_s=1, heartbeat_s=1000)  # 心跳冻结：不续期、不刷新消息空闲
    ws_b = dataclasses.replace(WS, claim_idle_s=1.2, claim_interval_s=0.2)
    await qa.enqueue(rid, "created")
    async with _running(Worker(qa, ws_a, drive=drive)):
        await _until(lambda: _true(len(drive.calls) == 1))
        async with _running(Worker(qb, ws_b, drive=drive)):

            async def taken_over() -> bool:
                return await _state(rid) == RunState.COMPLETED.value and qb.acked == [rid]

            await _until(taken_over)
            gate_a.set()  # A "复活"，继续往下写
            await _until(lambda: _true(bool(drive.errors)))
    assert [epoch for _, epoch in drive.calls] == [1, 2]
    assert isinstance(drive.errors[0], LeaseLost)
    assert qa.acked == []  # 失租者不 ACK
    assert await _pending(client) == 0
    assert [e.seq for e in await _events(rid)] == [1, 2, 3]  # working(A) + 响应 + completed（B），无 A 的残留写入
    assert (await lease_info(rid))[1] == 2


async def _true(v: bool) -> bool:
    return v


async def test_renew_failure_self_isolates(server: Any) -> None:
    """持有期间租约被他人抢占：下一次心跳续期失败 → 中断本进程 harness、宽限期后取消 drive、不 ACK。"""
    client = _client(server)
    q = SpyQueue(client)
    rid = await _new_run()
    harness = FakeHarness()
    drive = StubDrive({rid}, gates=[asyncio.Event()], harness=harness)  # gate 永不打开：模拟卡住的模型轮次
    await q.enqueue(rid, "created")
    async with _running(Worker(q, WS, drive=drive)):
        await _until(lambda: _true(active.get(rid) is not None))
        async with db.SessionLocal() as s:  # 模拟租约过期后被其他节点抢占
            await s.execute(update(Run).where(Run.run_id == rid).values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)))
            await s.commit()
        assert await acquire_lease(rid, "intruder", 30) == 2
        await _until(lambda: _true(harness.interrupted == [rid] and bool(drive.errors)))
        assert isinstance(drive.errors[0], asyncio.CancelledError)
        await asyncio.sleep(0.2)
    assert q.acked == []
    assert await _pending(client) == 1  # 留给接管者
    owner, epoch, _ = await lease_info(rid)
    assert (owner, epoch) == ("intruder", 2)  # 旧持有者的释放落空，没有清掉接管者的租约


# ---- 毒消息与 reaper ----


async def test_poison_message_goes_to_dead_letter(server: Any) -> None:
    """投递次数超过 MAX_DELIVERIES：进死信流、原消息 ACK、Run failed（RUN_POISONED）。"""
    client = _client(server)
    q = RedisStreamQueue(client)
    rid = await _new_run()
    await q.enqueue(rid, "created")
    assert [d.run_id for d in await q.read("ghost", 1, 10)] == [rid]  # 第 1 次投递，从不 ACK
    claimed = await q.autoclaim("c1", 0, 10, max_deliveries=2)
    assert [(d.run_id, d.deliveries) for d in claimed] == [(rid, 2)]
    assert await q.autoclaim("c2", 0, 10, max_deliveries=2) == []  # 第 3 次 > 2：毒消息
    dead = await client.xrange(DEAD_STREAM)
    assert [(f["run_id"], f["deliveries"]) for _, f in dead] == [(rid, "3")]
    assert await _pending(client) == 0
    assert await _state(rid) == RunState.FAILED.value
    errors = [e.payload for e in await _events(rid) if e.type == RunEventType.RUN_ERROR]
    assert errors and errors[0]["code"] == "RUN_POISONED"


async def test_reaper_requeues_unleased_durable_runs(server: Any) -> None:
    """reaper 只重投 durable、可执行且无有效租约的 Run；去重窗口内不重复投递。"""
    client = _client(server)
    q = RedisStreamQueue(client)
    orphan = await _new_run()
    legacy = await _new_run(durable=False)
    held = await _new_run()
    assert await acquire_lease(held, "someone", 30) == 1
    done = await _new_run(state=RunState.CANCELED)
    w = Worker(q, WS, drive=StubDrive(set()))

    async def queued() -> list[str]:
        return [f["run_id"] for _, f in await client.xrange(STREAM)]

    assert await w.reap_once() >= 1
    ids = await queued()
    assert orphan in ids
    assert legacy not in ids and held not in ids and done not in ids
    await w.reap_once()
    assert (await queued()).count(orphan) == 1


async def test_worker_autoclaims_orphaned_message(server: Any) -> None:
    """消费者领取后死亡（未 ACK、无租约）：存活 Worker 经 XAUTOCLAIM 接管并完成。"""
    client = _client(server)
    q = SpyQueue(client)
    rid = await _new_run()
    await q.enqueue(rid, "created")
    await q.read("dead-consumer", 1, 10)
    drive = StubDrive({rid})
    async with _running(Worker(q, dataclasses.replace(WS, claim_idle_s=0.1, claim_interval_s=0.1), drive=drive)):
        await _until(lambda: _true(q.acked == [rid]))
    assert await _state(rid) == RunState.COMPLETED.value
    assert await _pending(client) == 0


# ---- 取消 ----


async def test_cross_node_cancel_forwarding(server: Any) -> None:
    """publish_cancel：本地命中则直接中断，并经 runtime:cancel 广播，各节点订阅者都收到。"""
    got_a: list[str] = []
    got_b: list[str] = []

    def recorder(sink: list[str]) -> Callable[[str], Awaitable[bool]]:
        async def interrupt(run_id: str) -> bool:
            sink.append(run_id)
            return True

        return interrupt

    la = control.CancelListener(_client(server), recorder(got_a))
    lb = control.CancelListener(_client(server), recorder(got_b))
    tasks = [asyncio.create_task(la.run()), asyncio.create_task(lb.run())]
    try:
        await asyncio.wait_for(asyncio.gather(la.ready.wait(), lb.ready.wait()), 2)
        rid = f"s7w-{uuid4().hex[:8]}"
        harness = FakeHarness()
        session = object()
        active.register(rid, harness, session)  # type: ignore[arg-type]
        try:
            assert await control.publish_cancel(rid, client=_client(server)) is True
        finally:
            active.unregister(rid, session)  # type: ignore[arg-type]
        assert harness.interrupted == [rid]
        await _until(lambda: _true(got_a == [rid] and got_b == [rid]))
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_heartbeat_detects_cancel_without_pubsub(server: Any) -> None:
    """取消消息丢失时的兜底：心跳读到 canceled → 中断 harness → drive 收尾 → 释放租约并 ACK。"""
    client = _client(server)
    q = SpyQueue(client)
    rid = await _new_run()
    gate = asyncio.Event()
    harness = FakeHarness(on_interrupt=gate)
    drive = StubDrive({rid}, gates=[gate], harness=harness)
    await q.enqueue(rid, "created")
    async with _running(Worker(q, WS, drive=drive)):
        await _until(lambda: _true(active.get(rid) is not None))
        async with db.SessionLocal() as s:  # API 侧取消（不带 fence），不发布 pub/sub
            await EventLog(s).transition(rid, RunState.CANCELED, "user")
            await s.commit()
        await _until(lambda: _true(q.acked == [rid]))
    assert harness.interrupted == [rid]
    assert await _state(rid) == RunState.CANCELED.value
    assert (await lease_info(rid))[0] is None


# ---- InMemory 模式与配置 ----


async def test_inmemory_mode_keeps_single_process_behavior() -> None:
    """未配 REDIS_URL：进程级队列为 InMemoryQueue；Worker 照常领取执行；取消只做本地中断。"""
    from runtime.worker.settings import worker_settings

    if worker_settings.redis_url:
        pytest.skip("REDIS_URL configured in test environment")
    assert isinstance(queue_mod.get_queue(), InMemoryQueue)
    q = InMemoryQueue()
    rid = await _new_run()
    drive = StubDrive({rid})
    await q.enqueue(rid, "created")
    async with _running(Worker(q, WS, drive=drive)):
        await _until(lambda: _true(len(drive.calls) == 1))

        async def done() -> bool:
            return await _state(rid) == RunState.COMPLETED.value and (await lease_info(rid))[0] is None

        await _until(done)
    assert len(q) == 0
    harness = FakeHarness()
    session = object()
    active.register(rid, harness, session)  # type: ignore[arg-type]
    try:
        assert await control.publish_cancel(rid) is True
    finally:
        active.unregister(rid, session)  # type: ignore[arg-type]
    assert harness.interrupted == [rid]


async def test_inmemory_enqueue_dedupe() -> None:
    q = InMemoryQueue()
    assert await q.enqueue("r1", "reaper", dedupe_s=60) is True
    assert await q.enqueue("r1", "reaper", dedupe_s=60) is False
    assert await q.enqueue("r1", "created") is True
    assert [d.run_id for d in await q.read("c", 10, 10)] == ["r1", "r1"]


def test_role_settings_validation() -> None:
    assert problems(dataclasses.replace(WS, role="all"), "dev") == []
    assert any("REDIS_URL" in p for p in problems(dataclasses.replace(WS, role="api"), "dev"))
    assert any("REDIS_URL" in p for p in problems(dataclasses.replace(WS, role="worker"), "dev"))
    assert any("RUNTIME_ROLE" in p for p in problems(dataclasses.replace(WS, role="bogus"), "dev"))
    assert any("生产" in p for p in problems(WS, "prod"))
    assert "redis" not in repr(dataclasses.replace(WS, redis_url="redis://:secret@h:6379/0"))  # 口令不进 repr


async def test_start_background_by_role(server: Any) -> None:
    """api 角色不启动 Worker；配了 Redis 时所有角色都订阅取消频道。"""
    bg = await start_background(dataclasses.replace(WS, role="api"))
    assert bg.worker is None and bg.tasks == []
    await bg.stop()
    client = _client(server)
    saved = (queue_mod._queue, queue_mod._redis)  # 会话级 TestClient 的 Worker 正在消费进程级 InMemoryQueue，用后恢复
    queue_mod.configure(RedisStreamQueue(client), client)
    try:
        bg = await start_background(dataclasses.replace(WS, role="worker"), drive=StubDrive(set()))
        assert bg.worker is not None and len(bg.tasks) == 2
        await bg.stop()
        assert queue_mod._redis is None and queue_mod._queue is None  # 停止后清空 Redis 单例
    finally:
        queue_mod.configure(*saved)

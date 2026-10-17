"""Run Worker（S7 §1）：领取 → 抢租约 → 持租约驱动 → 释放租约 → ACK。

投递与确认语义（至少一次投递，租约去重）：

- 抢不到租约（他人持有有效租约 / Run 已不可执行 / 不存在）→ 不处理，ACK。持有者若随后死亡，其自己的未确认消息
  会被 XAUTOCLAIM 接管，reaper 也会按库重投，因此 ACK 重复消息是安全的。
- 驱动正常结束（完成、失败、挂起等待输入 / 审批、取消）→ 释放租约，释放成功才 ACK；释放落空说明租约已被接管，
  不 ACK，交给接管者。
- LeaseLost（写入时发现纪元已变）或心跳续期失败 → 自我隔离：中断 harness、不再写入、不 ACK。
- SeqConflict → 重读状态：已不可执行（canceled / 终态 / 挂起）则收尾 ACK；否则同一租约内重试一次，仍冲突则不 ACK。
- 其他异常 → 释放租约、不 ACK：消息空闲超时后被 XAUTOCLAIM 重新投递，超过 MAX_DELIVERIES 进死信。

另有两个后台循环：每 CLAIM_INTERVAL_S 以空闲槽位数 XAUTOCLAIM（min-idle = CLAIM_IDLE_S）；每 REAPER_S 扫
``expired_unleased_runs`` 重投（同一 Run 在去重窗口内只投一次）。InMemory 模式（单进程）无 XAUTOCLAIM，reaper 照常运行
（启动时先扫一次）——它是 InMemory 下丢投递（如 SeqConflict 未 ACK、补投失败）的唯一兜底；测试里经 REAPER_S 调大间隔。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from runtime.state.eventlog import LEASABLE_STATES, LeaseLost, SeqConflict, expired_unleased_runs
from runtime.worker import lease as lease_mod
from runtime.worker.control import CancelListener
from runtime.worker.queue import Delivery, InMemoryQueue, RunQueue, configure, get_queue, redis_client
from runtime.worker.settings import WorkerSettings, worker_settings

logger = logging.getLogger(__name__)

DriveFn = Callable[[str], AsyncIterator[Any]]
READ_BLOCK_MS = 1000  # XREADGROUP 单次阻塞时长
ERROR_BACKOFF_S = 1.0  # 队列读取出错后的退避


def default_drive(run_id: str) -> AsyncIterator[Any]:
    """持久化续跑入口（WP-C：runtime.loop.runner.drive_durable）；调用方已处于 fence_scope 内。"""
    from runtime.loop import runner

    fn = getattr(runner, "drive_durable", None)
    if fn is None:
        raise RuntimeError("runtime.loop.runner.drive_durable 尚未实现")
    it: AsyncIterator[Any] = fn(run_id)
    return it


class Worker:
    """单进程内的 Run 消费者；并发上限 WORKER_CONCURRENCY。``run()`` 一直运行到被取消。"""

    def __init__(
        self, queue: RunQueue, ws: WorkerSettings | None = None, *, drive: DriveFn | None = None, consumer: str | None = None
    ) -> None:
        self.q = queue
        self.ws = ws or worker_settings
        self.drive_fn = drive or default_drive
        self.consumer = consumer or lease_mod.new_owner()  # 同时作为租约持有者标识
        self._slots = asyncio.Semaphore(self.ws.concurrency)
        self._tasks: set[asyncio.Task[None]] = set()
        self._durable_queue = not isinstance(queue, InMemoryQueue)  # 有投递计数与接管的队列（Redis）

    # ---- 主循环 ----

    async def run(self) -> None:
        loops = [asyncio.create_task(self._consume_loop(), name="worker-consume")]
        if self._durable_queue:
            loops.append(asyncio.create_task(self._claim_loop(), name="worker-autoclaim"))
        loops.append(asyncio.create_task(self._reaper_loop(), name="worker-reaper"))
        try:
            if not self._durable_queue:
                await self._reap_quietly()  # 单进程：启动时先把上次进程遗留的持久化 Run 重新投递，之后按 REAPER_S 周期扫
            await asyncio.gather(*loops)
        finally:
            for t in loops:
                t.cancel()
            await asyncio.gather(*loops, return_exceptions=True)
            await self.drain()

    async def drain(self) -> None:
        """取消并等待在途 Run（各自在 finally 中释放租约，不 ACK，由接管 / reaper 续跑）。"""
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def _spawn(self, d: Delivery) -> None:
        """为一条已占用槽位的投递启动处理任务；任务结束归还槽位。"""
        t = asyncio.create_task(self._handle(d), name=f"worker-run-{d.run_id}")
        self._tasks.add(t)

        def _done(task: asyncio.Task[None]) -> None:
            self._tasks.discard(task)
            self._slots.release()

        t.add_done_callback(_done)

    async def _consume_loop(self) -> None:
        while True:
            await self._slots.acquire()
            try:
                ds = await self.q.read(self.consumer, 1, READ_BLOCK_MS)
            except asyncio.CancelledError:
                self._slots.release()
                raise
            except Exception:
                self._slots.release()
                logger.warning("run queue read failed", exc_info=True)
                await asyncio.sleep(ERROR_BACKOFF_S)
                continue
            if not ds:
                self._slots.release()
                continue
            self._spawn(ds[0])
            for extra in ds[1:]:  # count=1 时不会发生；防御性地占用槽位
                await self._slots.acquire()
                self._spawn(extra)

    async def claim_once(self) -> int:
        """按空闲槽位数 XAUTOCLAIM 一次，返回接管并开始处理的条数。"""
        n = 0
        while n < self.ws.concurrency and not self._slots.locked():
            await self._slots.acquire()
            n += 1
        if n == 0:
            return 0
        try:
            ds = await self.q.autoclaim(self.consumer, self.ws.claim_idle_s, n, self.ws.max_deliveries)
        except Exception:
            logger.warning("run queue autoclaim failed", exc_info=True)
            ds = []
        for d in ds[:n]:
            self._spawn(d)
        for _ in range(n - min(n, len(ds))):
            self._slots.release()
        return min(n, len(ds))

    async def _claim_loop(self) -> None:
        while True:
            await asyncio.sleep(self.ws.claim_interval_s)
            await self.claim_once()

    async def reap_once(self) -> int:
        """reaper：把持久化、可执行且无有效租约的 Run 重新投递（兜底丢消息）；同一 Run 在去重窗口内只投一次。"""
        dedupe_s = max(self.ws.claim_idle_s, 3 * self.ws.reaper_s)
        n = 0
        for run_id in await expired_unleased_runs(limit=100):
            if await self.q.enqueue(run_id, "reaper", dedupe_s=dedupe_s):
                n += 1
        if n:
            logger.info("reaper re-enqueued %d run(s)", n)
        return n

    async def _reap_quietly(self) -> None:
        try:
            await self.reap_once()
        except Exception:
            logger.warning("reaper scan failed", exc_info=True)

    async def _reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(self.ws.reaper_s)
            await self._reap_quietly()

    # ---- 单条投递 ----

    async def _handle(self, d: Delivery) -> None:
        try:
            ack = await self.process(d)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("run %s: worker failed; message left pending for redelivery", d.run_id)
            return
        if ack:
            try:
                await self.q.ack(d)
            except Exception:
                logger.warning("ack for run %s failed; duplicate delivery will be skipped by lease", d.run_id, exc_info=True)

    async def process(self, d: Delivery) -> bool:
        """处理一条投递，返回是否应 ACK。"""
        if not d.run_id:
            return True

        async def touch() -> None:
            await self.q.touch(self.consumer, d)

        async with lease_mod.hold(d.run_id, self.consumer, ws=self.ws, on_beat=touch) as held:
            if held is None:
                return True  # 他人持有有效租约 / Run 不可执行 / 不存在：不处理
            ok = await self._drive_held(held)
        return ok and held.released

    async def _drive_once(self, run_id: str) -> None:
        async for _ in self.drive_fn(run_id):
            pass  # 事件已由 runner 写入事件日志；这里只需把生成器驱动到底

    async def _watchdog(self, held: lease_mod.Lease, task: asyncio.Task[None]) -> None:
        """失租或发现取消后（harness 已被中断），给 drive 宽限期自行收尾，超时则取消 drive 任务。"""
        waits = [asyncio.create_task(held.lost.wait()), asyncio.create_task(held.canceled.wait())]
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            await asyncio.sleep(self.ws.isolate_grace_s)
            if not task.done():
                logger.warning("run %s: drive did not stop within grace period; cancelling", held.run_id)
                task.cancel()
        finally:
            for w in waits:
                w.cancel()

    async def _drive_held(self, held: lease_mod.Lease) -> bool:
        """在已持有的租约（及 fence）内驱动 Run；返回是否应 ACK（还需租约释放成功）。"""
        for attempt in (1, 2):
            task = asyncio.create_task(self._drive_once(held.run_id), name=f"drive-{held.run_id}")  # 继承 fence 上下文
            watchdog = asyncio.create_task(self._watchdog(held, task))
            try:
                await task
                return not held.lost.is_set()
            except LeaseLost:
                logger.warning("run %s: lease epoch %d lost while writing; aborting without ack", held.run_id, held.epoch)
                held.lost.set()
                return False
            except SeqConflict:
                state = await lease_mod.run_state(held.run_id)
                if state not in LEASABLE_STATES:
                    return True  # 已取消 / 终态 / 挂起：收尾
                if attempt == 2:
                    logger.warning("run %s: repeated seq conflict; leaving message for redelivery", held.run_id)
                    return False
                logger.info("run %s: seq conflict, retrying once", held.run_id)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if task.cancelled() and (current is None or not current.cancelling()):
                    return not held.lost.is_set()  # watchdog 取消了 drive：取消的 Run 收尾 ACK，失租不 ACK
                raise
            finally:
                watchdog.cancel()
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(BaseException):
                        await asyncio.wait({task}, timeout=5)
        return False


class Background:
    """main.py lifespan / ``python -m runtime.worker`` 启动的后台组件集合。"""

    def __init__(self, tasks: list[asyncio.Task[None]], worker: Worker | None) -> None:
        self.tasks = tasks
        self.worker = worker

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        client = redis_client()
        if client is not None:  # Redis 客户端绑定当前事件循环：关闭并清空单例，下次启动按配置重建
            with contextlib.suppress(Exception):
                await client.aclose()
            configure(None)


async def start_background(ws: WorkerSettings | None = None, *, drive: DriveFn | None = None) -> Background:
    """按 RUNTIME_ROLE 启动后台组件：worker / all 启动 Worker（含 XAUTOCLAIM 与 reaper）；
    配了 Redis 时所有角色都订阅跨节点取消频道（API 节点上也可能有内联执行的 Run）。"""
    ws = ws or worker_settings
    tasks: list[asyncio.Task[None]] = []
    worker: Worker | None = None
    if ws.runs_worker:
        worker = Worker(get_queue(ws), ws, drive=drive)
        tasks.append(asyncio.create_task(worker.run(), name="runtime-worker"))
    client = redis_client(ws)
    if client is not None:
        tasks.append(asyncio.create_task(CancelListener(client).run(), name="runtime-cancel-listener"))
    return Background(tasks, worker)

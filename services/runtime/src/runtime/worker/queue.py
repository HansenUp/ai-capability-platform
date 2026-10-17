"""Run 投递队列（S7 §1，D105 / D117）：MySQL 权威、Redis 只投递。

消息只含 ``{run_id, reason}``——不带消息内容、不带令牌；Worker 收到后以租约（t_run.lease_*）决定谁真正执行。
投递语义是"至少一次"：重复投递由租约去重（已有有效租约持有者则 ACK 跳过），丢消息由 reaper 扫库重投兜底。

- ``RedisStreamQueue``：Stream ``runtime:runs``、消费组 ``workers``；XREADGROUP 领取、XACK 确认、
  XCLAIM JUSTID 刷新空闲（心跳）、XAUTOCLAIM 接管空闲超时的消息；同一消息投递次数 > MAX_DELIVERIES 时转入
  死信流 ``runtime:runs:dead``、ACK 原消息并把 Run 置 failed（RUN_POISONED）。
- ``InMemoryQueue``：未配 REDIS_URL 时的进程内实现（单进程，RUNTIME_ROLE=all），无投递计数与接管。

模块级 ``get_queue()`` / ``enqueue()`` 供 API（WP-D）调用。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, NamedTuple, Protocol

from ai_contract import RunEventType, RunState
from runtime import credentials
from runtime.config import settings
from runtime.state import db
from runtime.state.db import Run
from runtime.state.eventlog import LEASABLE_STATES, EventLog, RunNotFound, StaleWriter, acquire_lease, fence_scope, release_lease
from runtime.worker.settings import WorkerSettings, worker_settings

logger = logging.getLogger(__name__)

STREAM = "runtime:runs"  # Run 投递流
GROUP = "workers"  # 消费组
DEAD_STREAM = "runtime:runs:dead"  # 死信流
DEDUPE_PREFIX = "runtime:runs:dedupe:"  # reaper 重投去重键前缀（SET NX EX）
STREAM_MAXLEN = 100_000  # 投递流近似上限：已确认的旧消息会被裁掉；万一裁到未确认消息，由 reaper 兜底
POISONED = "RUN_POISONED"  # 毒消息错误码


class Delivery(NamedTuple):
    """一次投递：消息 ID、Run、投递原因、该消息累计投递次数（首次领取为 1）。"""

    msg_id: str
    run_id: str
    reason: str
    deliveries: int = 1


class RunQueue(Protocol):
    """Run 投递队列协议（Worker 与 API 只依赖此协议）。"""

    async def enqueue(self, run_id: str, reason: str, *, dedupe_s: float | None = None) -> bool:
        """投递一条 ``{run_id, reason}``；给定 dedupe_s 时同一 Run 在该时长内只投递一次。返回是否真正投递。"""
        ...

    async def read(self, consumer: str, count: int, block_ms: int) -> list[Delivery]:
        """以 consumer 身份领取新消息，最多等待 block_ms。"""
        ...

    async def ack(self, delivery: Delivery) -> None:
        """确认消息（处理完毕或无需处理）。"""
        ...

    async def touch(self, consumer: str, delivery: Delivery) -> None:
        """刷新消息空闲时间（心跳），防止被他人 XAUTOCLAIM。"""
        ...

    async def autoclaim(self, consumer: str, min_idle_s: float, count: int, max_deliveries: int) -> list[Delivery]:
        """接管空闲超过 min_idle_s 的未确认消息；投递次数超过 max_deliveries 的转死信，不返回。"""
        ...

    async def close(self) -> None: ...


async def fail_poisoned(run_id: str, deliveries: int, ttl_s: float | None = None) -> bool:
    """毒消息收尾：先抢 Run 的租约（他人持有有效租约 = 正被驱动，不越权判死，只记日志），在其 fence 内记
    RUN_ERROR(RUN_POISONED)、迁到 failed 并删除存储的发起人凭据（同一事务；seq / state / epoch 三重 CAS，不覆盖他人写入），
    最后释放租约。返回是否迁移成功；Run 不存在、已不可执行、被他人持有或并发写冲突时只记日志。"""
    owner = f"{settings.node_id}:poison"
    epoch = await acquire_lease(run_id, owner, ttl_s if ttl_s is not None else worker_settings.lease_ttl_s)
    if epoch is None:
        logger.warning("poisoned run %s is leased by another holder or no longer runnable; not failing it", run_id)
        return False
    try:
        with fence_scope(epoch, run_id):
            async with db.SessionLocal() as s:
                run = await s.get(Run, run_id)
                if run is None or run.state not in LEASABLE_STATES:
                    return False
                log = EventLog(s)
                await log.append(
                    run_id,
                    RunEventType.RUN_ERROR,
                    {"code": POISONED, "message": f"run message delivered {deliveries} times without completion"},
                )
                await log.transition(run_id, RunState.FAILED, "poisoned")
                await credentials.delete_credential(run_id, s)  # 终态即删存储的发起人凭据（D113），与迁移同事务
                await s.commit()
        return True
    except (RunNotFound, StaleWriter, ValueError):
        logger.warning("could not mark poisoned run %s failed", run_id, exc_info=True)
        return False
    finally:
        try:
            await release_lease(run_id, owner, epoch)
        except Exception:
            logger.warning("lease release for poisoned run %s failed; it will expire", run_id, exc_info=True)


class InMemoryQueue:
    """进程内队列：未配 REDIS_URL 时使用，保持单进程行为。

    用 deque + 轮询实现（不绑定事件循环，测试里多个事件循环共用同一进程级实例也安全）；
    无消费组、无投递计数，autoclaim 永远为空，ack / touch 为空操作。"""

    POLL_S = 0.02

    def __init__(self) -> None:
        self._items: deque[Delivery] = deque()
        self._dedupe: dict[str, float] = {}
        self._seq = 0

    async def enqueue(self, run_id: str, reason: str, *, dedupe_s: float | None = None) -> bool:
        now = time.monotonic()
        if dedupe_s is not None:
            if self._dedupe.get(run_id, 0.0) > now:
                return False
            self._dedupe = {k: v for k, v in self._dedupe.items() if v > now}
            self._dedupe[run_id] = now + dedupe_s
        self._seq += 1
        self._items.append(Delivery(f"mem-{self._seq}", run_id, reason))
        return True

    async def read(self, consumer: str, count: int, block_ms: int) -> list[Delivery]:
        deadline = time.monotonic() + block_ms / 1000
        while not self._items and time.monotonic() < deadline:  # noqa: ASYNC110 —— 轮询而非 Event：不绑定事件循环（见类说明）
            await asyncio.sleep(self.POLL_S)
        out: list[Delivery] = []
        while self._items and len(out) < count:
            out.append(self._items.popleft())
        return out

    async def ack(self, delivery: Delivery) -> None:
        return None

    async def touch(self, consumer: str, delivery: Delivery) -> None:
        return None

    async def autoclaim(self, consumer: str, min_idle_s: float, count: int, max_deliveries: int) -> list[Delivery]:
        return []

    async def close(self) -> None:
        return None

    def __len__(self) -> int:
        return len(self._items)


def _is_nogroup(exc: Exception) -> bool:
    return "NOGROUP" in str(exc)


class RedisStreamQueue:
    """Redis Streams 实现。client 为 ``redis.asyncio.Redis``（decode_responses=True）或测试用 fakeredis 客户端。"""

    def __init__(self, client: Any, *, stream: str = STREAM, group: str = GROUP, dead_stream: str = DEAD_STREAM) -> None:
        self.r = client
        self.stream = stream
        self.group = group
        self.dead_stream = dead_stream
        self._group_ready = False

    async def ensure_group(self) -> None:
        """创建消费组（从流头开始，已有则忽略）；Redis 被清空（FLUSHALL）后会在下一次 NOGROUP 时重建。"""
        try:
            await self.r.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except Exception as e:  # redis.ResponseError: BUSYGROUP
            if "BUSYGROUP" not in str(e):
                raise
        self._group_ready = True

    async def enqueue(self, run_id: str, reason: str, *, dedupe_s: float | None = None) -> bool:
        if dedupe_s is not None and not await self.r.set(DEDUPE_PREFIX + run_id, "1", nx=True, px=max(1, int(dedupe_s * 1000))):
            return False
        await self.r.xadd(self.stream, {"run_id": run_id, "reason": reason}, maxlen=STREAM_MAXLEN, approximate=True)
        return True

    async def read(self, consumer: str, count: int, block_ms: int) -> list[Delivery]:
        if not self._group_ready:
            await self.ensure_group()
        started = time.monotonic()
        try:
            resp = await self.r.xreadgroup(self.group, consumer, {self.stream: ">"}, count=count, block=block_ms)
        except Exception as e:
            if _is_nogroup(e):
                self._group_ready = False
                return []
            raise
        if not resp and time.monotonic() - started < block_ms / 2000:
            # 服务端没有阻塞就返回了空（如 fakeredis 忽略 BLOCK）：退避一下，避免消费循环空转占满事件循环
            await asyncio.sleep(min(0.05, block_ms / 1000))
        out: list[Delivery] = []
        for _stream, entries in resp or []:
            for msg_id, fields in entries:
                out.append(Delivery(msg_id, fields.get("run_id", ""), fields.get("reason", ""), 1))
        return out

    async def ack(self, delivery: Delivery) -> None:
        await self.r.xack(self.stream, self.group, delivery.msg_id)

    async def touch(self, consumer: str, delivery: Delivery) -> None:
        """XCLAIM … JUSTID：把消息空闲时间清零并保持归属本消费者，不增加投递计数。"""
        await self.r.xclaim(self.stream, self.group, consumer, min_idle_time=0, message_ids=[delivery.msg_id], justid=True)

    async def _deliveries(self, msg_id: str) -> int:
        rows = await self.r.xpending_range(self.stream, self.group, min=msg_id, max=msg_id, count=1)
        return int(rows[0]["times_delivered"]) if rows else 1

    async def autoclaim(self, consumer: str, min_idle_s: float, count: int, max_deliveries: int) -> list[Delivery]:
        if count <= 0:
            return []
        if not self._group_ready:
            await self.ensure_group()
        try:
            resp = await self.r.xautoclaim(
                self.stream, self.group, consumer, min_idle_time=int(min_idle_s * 1000), start_id="0-0", count=count
            )
        except Exception as e:
            if _is_nogroup(e):
                self._group_ready = False
                return []
            raise
        out: list[Delivery] = []
        for msg_id, fields in resp[1]:
            if not fields:  # 流已被裁剪、消息体不在了：只能确认掉，由 reaper 兜底
                await self.r.xack(self.stream, self.group, msg_id)
                continue
            d = Delivery(msg_id, fields.get("run_id", ""), fields.get("reason", ""), await self._deliveries(msg_id))
            if d.deliveries > max_deliveries:
                await self.dead_letter(d)
                continue
            out.append(d)
        return out

    async def dead_letter(self, d: Delivery) -> None:
        """毒消息：写死信流、ACK 原消息、Run 置 failed（RUN_POISONED）。"""
        logger.error("run %s message %s delivered %d times: moving to %s", d.run_id, d.msg_id, d.deliveries, self.dead_stream)
        await self.r.xadd(
            self.dead_stream,
            {"run_id": d.run_id, "reason": d.reason, "msg_id": d.msg_id, "deliveries": str(d.deliveries)},
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )
        await self.r.xack(self.stream, self.group, d.msg_id)
        if d.run_id:
            await fail_poisoned(d.run_id, d.deliveries)

    async def close(self) -> None:
        await self.r.aclose()


# ---- 进程级单例 ----

_queue: RunQueue | None = None
_redis: Any = None


def redis_client(ws: WorkerSettings | None = None) -> Any:
    """进程级共享的 Redis 客户端（未配 REDIS_URL 返回 None）。连接串含口令，不写日志。"""
    global _redis
    ws = ws or worker_settings
    if _redis is None and ws.redis_url:
        import redis.asyncio as aioredis

        _redis = aioredis.from_url(ws.redis_url, decode_responses=True, health_check_interval=30)
    return _redis


def get_queue(ws: WorkerSettings | None = None) -> RunQueue:
    """进程级队列：配了 REDIS_URL 用 RedisStreamQueue，否则 InMemoryQueue。"""
    global _queue
    if _queue is None:
        client = redis_client(ws)
        _queue = RedisStreamQueue(client) if client is not None else InMemoryQueue()
    return _queue


def configure(queue: RunQueue | None, client: Any = None) -> None:
    """替换进程级队列 / Redis 客户端（测试注入；传 None 恢复为按配置惰性创建）。"""
    global _queue, _redis
    _queue = queue
    _redis = client


async def enqueue(run_id: str, reason: str = "created") -> None:
    """投递一个 Run 给 Worker 执行（API 创建 Run、审批通过、/input 后调用）。消息只含 run_id 与原因。"""
    await get_queue().enqueue(run_id, reason)

"""Run 租约持有（S7 §1，D105 / D106）：抢租约 → 心跳续期 → fence 写入 → 释放。

``hold(run_id)`` 是 Worker 与 API 内联执行（/v1/messages、/resume，WP-D）共用的入口::

    async with lease.hold(run_id) as held:
        if held is None:      # 他人持有有效租约 / Run 不可执行
            ...
        else:                 # 已在 fence_scope(held.epoch, run_id) 内；此后创建的任务继承 fence
            ...

心跳每 HEARTBEAT_S：``renew_lease``（失败 = 租约已被他人抢占 → 置 ``lost``、只中断本 epoch 的 harness 会话，写入随即因
fence 抛 LeaseLost，即自我隔离）；续期本身有超时，距上次续期成功的本地单调时间超过 LEASE_TTL_S（数据库不可达 / 卡住）
同样置 ``lost`` 并中断——此时租约很可能已被他人接管。成功后调用 on_beat（Worker 用来 XCLAIM JUSTID 刷新消息空闲）；
再读 Run state，发现 canceled 即置 ``canceled`` 并中断 harness（跨节点取消的兜底）。

释放租约后（释放成功、未失租、持有期间正常退出）重读 Run：仍可执行（submitted / working）、durable 且本 fence 之后
有他人写入（如持有者刚挂起，/input 或审批即把 Run 迁回 working 并入队，而那条投递因租约仍被持有被 ACK 跳过）时补投一次
（reason=released），避免丢唤醒。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import uuid4

from sqlalchemy import select

from ai_contract import RunState
from runtime.config import settings
from runtime.state import db
from runtime.state.db import Run
from runtime.state.eventlog import LEASABLE_STATES, FenceWrites, acquire_lease, fence_scope, release_lease, renew_lease
from runtime.worker import control, queue
from runtime.worker.settings import WorkerSettings, worker_settings

logger = logging.getLogger(__name__)


def new_owner() -> str:
    """租约持有者 / 消费者标识：节点 + 进程 + 随机后缀（同一进程内多个 Worker 也互不相同）。"""
    return f"{settings.node_id}:{os.getpid()}:{uuid4().hex[:8]}"


@dataclass
class Lease:
    """一次成功抢到的租约。lost：续期失败（已被他人接管）；canceled：心跳发现 Run 已取消；released：退出时释放成功。"""

    run_id: str
    owner: str
    epoch: int
    lost: asyncio.Event = field(default_factory=asyncio.Event)
    canceled: asyncio.Event = field(default_factory=asyncio.Event)
    released: bool = False
    writes: FenceWrites = field(default_factory=FenceWrites)  # 本 fence 下已提交的最大 seq（见 eventlog.FenceWrites）
    renewed_at: float = field(default_factory=time.monotonic)  # 最近一次抢到 / 续期成功的本地单调时间


async def run_state(run_id: str) -> str | None:
    """读 Run 当前状态（不存在返回 None）。"""
    async with db.SessionLocal() as s:
        return (await s.execute(select(Run.state).where(Run.run_id == run_id))).scalar_one_or_none()


async def run_head(run_id: str) -> tuple[str, bool, int] | None:
    """读 Run 的 (state, durable, last_seq)；不存在返回 None。"""
    async with db.SessionLocal() as s:
        row = (await s.execute(select(Run.state, Run.durable, Run.last_seq).where(Run.run_id == run_id))).first()
    return None if row is None else (str(row[0]), bool(row[1]), int(row[2]))


async def _isolate(lease: Lease, why: str) -> None:
    """失租自我隔离：置 lost，只中断本 epoch 在本进程的 harness 会话（同进程里接管该 Run 的新持有者不受影响）。"""
    logger.warning("lease for run %s (epoch %d) lost (%s): isolating", lease.run_id, lease.epoch, why)
    lease.lost.set()
    await control.interrupt_local(lease.run_id, epoch=lease.epoch)


async def _heartbeat(lease: Lease, ws: WorkerSettings, on_beat: Callable[[], Awaitable[None]] | None) -> None:
    while True:
        await asyncio.sleep(ws.heartbeat_s)
        # 续期有超时：最多等到心跳间隔与租约剩余本地时间中的较小者，避免数据库卡住时心跳无限挂起
        started = time.monotonic()  # 续期成功时以发起时刻计（数据库按执行时刻 + TTL 续期，取早不取晚，偏保守）
        remaining = ws.lease_ttl_s - (started - lease.renewed_at)
        try:
            ok = await asyncio.wait_for(
                renew_lease(lease.run_id, lease.owner, lease.epoch, ws.lease_ttl_s), timeout=max(0.05, min(ws.heartbeat_s, remaining))
            )
        except Exception:  # 数据库暂不可达 / 超时：未超过 TTL 时下次再试；写入另有 fence 兜底
            logger.warning("lease renew for run %s failed (transient)", lease.run_id, exc_info=True)
            if time.monotonic() - lease.renewed_at >= ws.lease_ttl_s:
                await _isolate(lease, f"no successful renewal within {ws.lease_ttl_s:g}s")
                return
            continue
        if not ok:
            await _isolate(lease, "renewal rejected")
            return
        lease.renewed_at = started
        if on_beat is not None:
            try:
                await on_beat()
            except Exception:
                logger.warning("heartbeat hook for run %s failed", lease.run_id, exc_info=True)
        if not lease.canceled.is_set():
            try:
                state = await run_state(lease.run_id)
            except Exception:
                logger.warning("state check for run %s failed", lease.run_id, exc_info=True)
                continue
            if state == RunState.CANCELED.value:
                lease.canceled.set()
                await control.interrupt_local(lease.run_id)


async def _redeliver_if_woken(lease: Lease) -> None:
    """释放租约后补投（防丢唤醒）：Run 仍可执行、durable，且本 fence 写入之后有他人写入（last_seq 更大）时入队一次。
    本 fence 下没写过任何事件（last_seq=0）时不补投——那不是"挂起后被唤醒"，交给原有投递 / reaper。"""
    try:
        head = await run_head(lease.run_id)
        if head is None:
            return
        state, durable, last_seq = head
        if durable and state in LEASABLE_STATES and lease.writes.last_seq and last_seq > lease.writes.last_seq:
            logger.info("run %s became runnable while held (seq %d > %d): re-enqueue", lease.run_id, last_seq, lease.writes.last_seq)
            await queue.enqueue(lease.run_id, "released")
    except Exception:  # 补投失败：durable Run 无租约时 reaper 兜底重投
        logger.warning("re-enqueue of run %s after release failed; the reaper will pick it up", lease.run_id, exc_info=True)


@asynccontextmanager
async def hold(
    run_id: str,
    owner: str | None = None,
    *,
    ws: WorkerSettings | None = None,
    on_beat: Callable[[], Awaitable[None]] | None = None,
    epoch: int | None = None,
) -> AsyncIterator[Lease | None]:
    """抢占并持有 Run 租约；抢不到时产出 None。持有期间当前上下文处于 ``fence_scope(epoch, run_id)``，
    退出时停止心跳并释放租约（失租时释放自然落空，不会影响接管者）；正常退出且释放成功后按需补投（见 _redeliver_if_woken）。

    epoch 非 None：调用方已经以 owner 持有该纪元的租约（如 /v1/messages 创建 Run 时同事务置租，eventlog.claim_new_run），
    直接接管心跳与释放，不再抢占。"""
    ws = ws or worker_settings
    owner = owner or new_owner()
    if epoch is None:
        epoch = await acquire_lease(run_id, owner, ws.lease_ttl_s)
    if epoch is None:
        yield None
        return
    lease = Lease(run_id, owner, epoch)
    beat = asyncio.create_task(_heartbeat(lease, ws, on_beat), name=f"lease-heartbeat-{run_id}")
    clean = False
    try:
        with fence_scope(epoch, run_id) as writes:
            lease.writes = writes
            yield lease
        clean = True
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await beat
        try:
            lease.released = await release_lease(run_id, owner, epoch)
        except Exception:
            logger.warning("lease release for run %s failed; it will expire", run_id, exc_info=True)
        if clean and lease.released and not lease.lost.is_set():
            await _redeliver_if_woken(lease)

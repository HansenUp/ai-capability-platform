"""跨节点控制通道（S7 §1，D110）：取消转发。

API 把 Run CAS 迁到 canceled 后调用 ``publish_cancel(run_id)``：先尝试中断本进程内的 harness，再
``PUBLISH runtime:cancel {run_id}``；每个节点（api / worker / all）运行 ``CancelListener`` 订阅该频道，
收到后中断本进程内正在驱动该 Run 的 harness。pub/sub 不保证送达，Worker 心跳读 Run state 发现 canceled
时再中断一次兜底。未配 REDIS_URL（InMemory 模式）时只做本地中断。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from runtime.loop import active
from runtime.worker.queue import redis_client

logger = logging.getLogger(__name__)

CANCEL_CHANNEL = "runtime:cancel"


async def interrupt_local(run_id: str, epoch: int | None = None) -> bool:
    """中断本进程内正在驱动该 Run 的 harness 会话；不在本进程返回 False。中断失败只记日志（尽力而为）。

    epoch 非 None（失租自我隔离）：只中断以该租约纪元登记的会话，并标记中断原因为失租——同进程里已接管该 Run 的
    新持有者（另一 epoch）不受影响，runner 也不会把这次中断当作用户取消。epoch 为 None（取消转发）：中断该 Run 的会话。"""
    if epoch is not None:
        return await active.interrupt(run_id, epoch=epoch, reason=active.REASON_LEASE_LOST)
    return await active.interrupt(run_id, reason=active.REASON_CANCEL)


async def publish_cancel(run_id: str, client: Any = None) -> bool:
    """跨节点取消（须在 Run 已迁到 canceled 并提交之后调用）。返回本进程是否命中正在运行的 Run。
    发布失败只记日志：心跳读到 canceled 会兜底中断。"""
    hit = await interrupt_local(run_id)
    r = client if client is not None else redis_client()
    if r is not None:
        try:
            await r.publish(CANCEL_CHANNEL, run_id)
        except Exception:
            logger.warning("publish cancel for run %s failed; relying on heartbeat fallback", run_id, exc_info=True)
    return hit


class CancelListener:
    """订阅 ``runtime:cancel``，对每条消息调用 interrupt（默认 interrupt_local）。断线后退避重连。"""

    RETRY_S = 1.0

    def __init__(self, client: Any, interrupt: Callable[[str], Awaitable[bool]] | None = None) -> None:
        self.r = client
        self.interrupt = interrupt or interrupt_local
        self.ready = asyncio.Event()  # 已完成订阅（测试用于等待订阅生效）

    async def run(self) -> None:
        while True:
            try:
                await self._listen()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("cancel channel subscription failed; retrying", exc_info=True)
                self.ready.clear()
                await asyncio.sleep(self.RETRY_S)

    async def _listen(self) -> None:
        ps = self.r.pubsub()
        try:
            await ps.subscribe(CANCEL_CHANNEL)
            self.ready.set()
            while True:
                msg = await ps.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg is None or msg.get("type") != "message":
                    continue
                run_id = msg.get("data")
                if isinstance(run_id, bytes):
                    run_id = run_id.decode()
                if isinstance(run_id, str) and run_id:
                    try:
                        await self.interrupt(run_id)
                    except Exception:
                        logger.warning("interrupt for canceled run %s failed", run_id, exc_info=True)
        finally:
            try:
                await ps.aclose()
            except Exception:
                logger.debug("pubsub close failed", exc_info=True)

"""进程内活动 Run 注册表：记录"本进程此刻正在驱动的 Run"及其 harness 会话，供取消 API 中断。

drive 打开（或重放恢复）harness 会话后登记 `run_id → (harness, session)`，drive 结束时（无论正常结束、
挂起等待审批、出错还是客户端断开）注销。`POST /v1/runs/{id}/cancel` 把 Run 迁移到 canceled 后，
若该 Run 正在本进程运行，就经这里拿到会话并调用 `harness.interrupt`，让阻塞中的模型轮次尽快收尾。

只覆盖单进程：多节点部署下取消经控制通道（runtime.worker.control，PUBLISH runtime:cancel）转发到持有该 Run 的
节点，由该节点调用 `interrupt(run_id)`；失租自我隔离时同样经它中断本进程内的 harness——登记时记下持租约纪元（fence epoch），
失租中断只命中该纪元的会话（同进程内接管同一 Run 的新持有者不受影响），并记下中断原因供 runner 区分"用户取消"与"失租"。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ai_contract import HarnessAdapter, SessionHandle

logger = logging.getLogger(__name__)

REASON_CANCEL = "cancel"  # 用户取消（取消 API / 跨节点转发 / 心跳发现 canceled）
REASON_LEASE_LOST = "lease_lost"  # 失租自我隔离


@dataclass
class _Entry:
    harness: HarnessAdapter
    session: SessionHandle
    epoch: int | None  # 登记时所处的租约纪元（无 fence 为 None）
    reason: str | None = None  # 最近一次中断的原因（未被中断为 None）


_ACTIVE: dict[str, _Entry] = {}  # run_id → 正在驱动该 Run 的 harness 会话


def register(run_id: str, harness: HarnessAdapter, session: SessionHandle, epoch: int | None = None) -> None:
    """登记一个正在本进程驱动的 Run（同一 run_id 后登记者覆盖先登记者）；epoch 为驱动方持有的租约纪元。"""
    _ACTIVE[run_id] = _Entry(harness, session, epoch)


def unregister(run_id: str, session: SessionHandle) -> None:
    """注销 Run；只在登记的仍是同一个会话时才移除，避免误删同一 Run 随后一次 drive 的登记。"""
    entry = _ACTIVE.get(run_id)
    if entry is not None and entry.session is session:
        del _ACTIVE[run_id]


def get(run_id: str) -> tuple[HarnessAdapter, SessionHandle] | None:
    """返回正在驱动该 Run 的 (harness, 会话)；不在本进程运行时返回 None。"""
    entry = _ACTIVE.get(run_id)
    return None if entry is None else (entry.harness, entry.session)


def interrupt_reason(run_id: str, session: SessionHandle) -> str | None:
    """该会话最近一次被中断的原因（REASON_CANCEL / REASON_LEASE_LOST）；未被中断或已不是登记的会话返回 None。"""
    entry = _ACTIVE.get(run_id)
    return entry.reason if entry is not None and entry.session is session else None


async def interrupt(run_id: str, *, epoch: int | None = None, reason: str = REASON_CANCEL) -> bool:
    """中断本进程内正在驱动该 Run 的 harness 会话（尽力而为，异常只记日志）；该 Run 不在本进程运行时返回 False。
    epoch 非 None 时只中断以该纪元登记的会话（失租隔离用），纪元不符返回 False。"""
    entry = _ACTIVE.get(run_id)
    if entry is None or (epoch is not None and entry.epoch != epoch):
        return False
    entry.reason = reason
    await interrupt_quietly(entry.harness, entry.session, run_id)
    return True


async def interrupt_quietly(harness: HarnessAdapter, session: SessionHandle, run_id: str) -> None:
    """调用 harness.interrupt 并吞掉其普通异常（只记日志）：中断是尽力而为的清理动作，
    失败不应覆盖 Run 已经确定的终态或掩盖调用方原本的异常。"""
    try:
        await harness.interrupt(session, run_id)
    except Exception:
        logger.warning("harness.interrupt failed for run %s", run_id, exc_info=True)

"""进程内活动 Run 注册表：记录"本进程此刻正在驱动的 Run"及其 harness 会话，供取消 API 中断。

drive 打开（或重放恢复）harness 会话后登记 `run_id → (harness, session)`，drive 结束时（无论正常结束、
挂起等待审批、出错还是客户端断开）注销。`POST /v1/runs/{id}/cancel` 把 Run 迁移到 canceled 后，
若该 Run 正在本进程运行，就经这里拿到会话并调用 `harness.interrupt`，让阻塞中的模型轮次尽快收尾。

只覆盖单进程：多节点部署下取消需经 Run 队列 / 控制通道转发到持有该 Run 的节点（S7 工作包）。
"""

from __future__ import annotations

import logging

from ai_contract import HarnessAdapter, SessionHandle

logger = logging.getLogger(__name__)

_ACTIVE: dict[str, tuple[HarnessAdapter, SessionHandle]] = {}  # run_id → 正在驱动该 Run 的 (harness, 会话)


def register(run_id: str, harness: HarnessAdapter, session: SessionHandle) -> None:
    """登记一个正在本进程驱动的 Run（同一 run_id 后登记者覆盖先登记者）。"""
    _ACTIVE[run_id] = (harness, session)


def unregister(run_id: str, session: SessionHandle) -> None:
    """注销 Run；只在登记的仍是同一个会话时才移除，避免误删同一 Run 随后一次 drive 的登记。"""
    entry = _ACTIVE.get(run_id)
    if entry is not None and entry[1] is session:
        del _ACTIVE[run_id]


def get(run_id: str) -> tuple[HarnessAdapter, SessionHandle] | None:
    """返回正在驱动该 Run 的 (harness, 会话)；不在本进程运行时返回 None。"""
    return _ACTIVE.get(run_id)


async def interrupt_quietly(harness: HarnessAdapter, session: SessionHandle, run_id: str) -> None:
    """调用 harness.interrupt 并吞掉其普通异常（只记日志）：中断是尽力而为的清理动作，
    失败不应覆盖 Run 已经确定的终态或掩盖调用方原本的异常。"""
    try:
        await harness.interrupt(session, run_id)
    except Exception:
        logger.warning("harness.interrupt failed for run %s", run_id, exc_info=True)

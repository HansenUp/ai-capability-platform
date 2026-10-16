"""Append-only Run event log + projection of the unified event history used for replay/checkpoint.
（追加写 Run 事件日志的读写封装，以及把 Run 事件日志投影为统一 Event 流（供 harness.resume
用于重放/checkpoint）的转换逻辑。）
"""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ai_contract import Event, RunEvent, RunEventType, RunState, can_transition
from runtime.state.db import Run, RunEventRow

_EVENTS = TypeAdapter(list[Event])  # 用于把投影出的 dict 列表按判别字段校验/解析为统一 Event 列表


class EventLog:
    """围绕单个 AsyncSession 的 Run 事件日志读写门面，封装 seq 分配、状态迁移与幂等键查重。"""

    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def append(
        self, run_id: str, type_: RunEventType, payload: dict[str, Any] | None = None, idempotency_key: str | None = None
    ) -> RunEvent:
        """追加一条事件：seq 取该 run 当前 last_seq + 1，并原子地把 Run.last_seq 同步更新，
        保证同一 run 内的 seq 严格单调递增、不重不漏。"""
        run = await self.s.get(Run, run_id)
        if run is None:
            raise KeyError(run_id)
        seq = run.last_seq + 1
        row = RunEventRow(run_id=run_id, seq=seq, type=type_.value, payload=payload or {}, idempotency_key=idempotency_key)
        self.s.add(row)
        await self.s.execute(update(Run).where(Run.run_id == run_id).values(last_seq=seq))
        await self.s.flush()
        return RunEvent(run_id=run_id, seq=seq, type=type_, payload=row.payload, idempotency_key=idempotency_key, ts=row.ts)

    async def transition(self, run_id: str, dst: RunState, reason: str = "") -> RunState:
        """把 Run 状态迁移到 dst：若已经处于 dst 则直接返回（幂等）；否则校验迁移合法性
        （非法迁移抛 ValueError），合法时更新 Run.state 并追加一条 RUN_STATE_CHANGED 事件。"""
        run = await self.s.get(Run, run_id)
        if run is None:
            raise KeyError(run_id)
        src = RunState(run.state)
        if src == dst:
            return dst
        if not can_transition(src, dst):
            raise ValueError(f"illegal transition {src} -> {dst}")
        run.state = dst.value
        await self.append(run_id, RunEventType.RUN_STATE_CHANGED, {"from": src.value, "to": dst.value, "reason": reason})
        return dst

    async def list(self, run_id: str, since_seq: int = 0) -> list[RunEvent]:
        """按 seq 升序返回该 run 自 since_seq 之后的全部事件（用于重放/增量查询）。"""
        rows = (
            await self.s.execute(
                select(RunEventRow).where(RunEventRow.run_id == run_id, RunEventRow.seq > since_seq).order_by(RunEventRow.seq)
            )
        ).scalars()
        return [
            RunEvent(run_id=r.run_id, seq=r.seq, type=RunEventType(r.type), payload=r.payload, idempotency_key=r.idempotency_key, ts=r.ts)
            for r in rows
        ]

    async def has_idempotency_key(self, key: str) -> bool:
        """判断某个幂等键是否已经对应过一次真正的 TOOL_EXECUTED 事件（用于避免重复执行写操作）。"""
        row = (
            await self.s.execute(
                select(RunEventRow.seq).where(RunEventRow.idempotency_key == key, RunEventRow.type == RunEventType.TOOL_EXECUTED.value)
            )
        ).first()
        return row is not None

    @staticmethod
    def project_unified_events(log: list[RunEvent]) -> list[Event]:
        """Turn the Run event log into the unified Event stream a harness can be resumed from.
        （把 Run 事件日志投影为统一的 Event 流，供 harness.resume 用于重放重建会话——
        这是"checkpoint 通过重放实现"这一机制在数据层面的具体转换：只挑选模型文本响应、
        工具请求、工具结果三类事件转换为对应的统一事件，其余管理类事件（如状态迁移）不参与投影。）
        """
        out: list[dict[str, Any]] = []
        for ev in log:
            if ev.type == RunEventType.MODEL_RESPONSE and ev.payload.get("text"):
                out.append({"type": "text", "delta": ev.payload["text"]})
            elif ev.type == RunEventType.TOOL_REQUESTED:
                out.append(
                    {
                        "type": "tool_use",
                        "call_id": ev.payload["call_id"],
                        "tool_name": ev.payload["tool"],
                        "input": ev.payload.get("input", {}),
                    }
                )
            elif ev.type == RunEventType.TOOL_RESULT:
                out.append(
                    {
                        "type": "tool_result",
                        "call_id": ev.payload["call_id"],
                        "status": ev.payload.get("status", "ok"),
                        "output": ev.payload.get("output"),
                        "output_ref": ev.payload.get("output_ref"),
                    }
                )
        return _EVENTS.validate_python(out)

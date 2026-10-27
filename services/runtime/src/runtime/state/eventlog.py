"""Append-only Run event log + projection of the unified event history used for replay/checkpoint.
（追加写 Run 事件日志的读写封装，以及把 Run 事件日志投影为统一 Event 流（供 harness.resume
用于重放/checkpoint）的转换逻辑。）

S7 单写者 fencing（D106/D107）：每次追加都以一条条件 UPDATE 推进 t_run.last_seq——
``UPDATE t_run SET last_seq=n [, state=dst] WHERE run_id=? AND last_seq=n-1 [AND state=src] [AND lease_epoch=fence]``，
与插入事件行同事务；命中 0 行即抛 StaleWriter（LeaseLost：租约纪元已变，持有者应立即中止；SeqConflict：
并发写者抢先或状态已变，调用方须 rollback 后在新事务内重读，已 canceled 则收尾，否则可重试一次）。
fence 由 Worker 经 ``fence_scope(epoch)`` 注入（contextvar）；API 侧写入不带 fence，但同样受 seq CAS 约束。
fence_scope 产出一个 ``FenceWrites``：记录本 fence 下**已提交**的最大 seq（事务提交后才计入，回滚不计），
持有者据此判断"自己之后是否有他人写入"（释放租约时补投、内联执行失租后从已写位置起尾随）。
同文件提供租约原语（acquire/renew/release/expired_unleased_runs）与写工具操作登记（t_tool_operation）。
"""

from __future__ import annotations

import builtins
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple, cast

from pydantic import TypeAdapter
from sqlalchemy import ColumnElement, CursorResult, and_, delete, event, func, or_, select, text, true, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.util import identity_key

from ai_contract import Event, RunEvent, RunEventType, RunState, can_transition
from runtime.state import db
from runtime.state.db import Run, RunCredential, RunEventRow, ToolOperation

_EVENTS = TypeAdapter(list[Event])  # 用于把投影出的 dict 列表按判别字段校验/解析为统一 Event 列表

LEASABLE_STATES = (RunState.SUBMITTED.value, RunState.WORKING.value)  # 可被抢占租约 / reaper 重投的状态
TERMINAL_STATES = tuple(state.value for state in RunState if state.terminal)


class RunNotFound(KeyError):
    """事件日志操作的目标 Run 不存在。继承 KeyError 保持兼容；API 层统一映射为 HTTP 404。"""


class StaleWriter(RuntimeError):
    """本次写入的前提已失效（CAS 命中 0 行），当前事务里的写入不可提交，调用方须 rollback。"""


class LeaseLost(StaleWriter):
    """fence 纪元与 t_run.lease_epoch 不一致：租约已被他人抢占，持有者应立即中止、不再写入。"""


class SeqConflict(StaleWriter):
    """last_seq 或 state 已被并发写者推进：rollback 后在新事务内重读，已 canceled 收尾，否则可重试一次。"""


class OperationExists(RuntimeError):
    """t_tool_operation 主键（operation_id）已存在：该写操作已被登记过，走去重或对账；当前事务须 rollback。"""


class FenceWrites:
    """一个 fence 下已提交写入的进度：last_seq 为已随事务提交的最大 seq（0 表示尚未写入）。
    同一 fence_scope 内派生的任务共享同一对象（contextvar 复制的是引用）。"""

    __slots__ = ("last_seq",)

    def __init__(self) -> None:
        self.last_seq = 0


class _Fence(NamedTuple):
    epoch: int
    run_id: str | None  # None 表示对本上下文内所有 Run 的写入生效
    writes: FenceWrites


_fence: ContextVar[_Fence | None] = ContextVar("runtime_eventlog_fence", default=None)
_PENDING = "runtime_fence_pending"  # session.info 键：本事务内 fence 写入的 {id(writes): (writes, seq)}，提交后计入


@contextmanager
def fence_scope(epoch: int, run_id: str | None = None) -> Iterator[FenceWrites]:
    """在当前上下文（asyncio 任务）内为事件日志写入加 fence：此后的 append / transition 额外要求
    ``lease_epoch = epoch``，否则抛 LeaseLost。给定 run_id 时只约束该 Run 的写入。供 WP-B Worker 持租约驱动 Run 时使用。
    产出的 FenceWrites 记录本 fence 下已提交的最大 seq。"""
    writes = FenceWrites()
    token = _fence.set(_Fence(epoch, run_id, writes))
    try:
        yield writes
    finally:
        _fence.reset(token)


@event.listens_for(Session, "after_commit")
def _fence_writes_committed(session: Session) -> None:
    """事务提交后把本事务内 fence 写入的 seq 计入各自的 FenceWrites。"""
    for writes, seq in session.info.pop(_PENDING, {}).values():
        writes.last_seq = max(writes.last_seq, seq)


@event.listens_for(Session, "after_rollback")
def _fence_writes_rolled_back(session: Session) -> None:
    """事务回滚：本事务内的 fence 写入作废，不计入。"""
    session.info.pop(_PENDING, None)


def current_fence(run_id: str) -> int | None:
    """当前上下文对 run_id 生效的 fence 纪元；无 fence 返回 None。"""
    f = _fence.get()
    if f is None or (f.run_id is not None and f.run_id != run_id):
        return None
    return f.epoch


class _Head(NamedTuple):
    last_seq: int
    state: str
    lease_epoch: int


class EventLog:
    """围绕单个 AsyncSession 的 Run 事件日志读写门面，封装 seq 分配（CAS + fencing）、状态迁移与幂等键查重。"""

    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def _head(self, run_id: str, *, locking: bool = False) -> _Head:
        """读 t_run 的 (last_seq, state, lease_epoch)。直接查列而非经 identity map，避免拿到会话里缓存的旧值；
        locking=True 时用共享锁读（MySQL 下读到最新已提交版本，用于 CAS 失败后的归因）。"""
        q = select(Run.last_seq, Run.state, Run.lease_epoch).where(Run.run_id == run_id)
        if locking:
            q = q.with_for_update(read=True)
        row = (await self.s.execute(q)).first()
        if row is None:
            raise RunNotFound(run_id)
        return _Head(int(row[0]), str(row[1]), int(row[2]))

    async def _advance(self, run_id: str, head: _Head, *, src: RunState | None = None, dst: RunState | None = None) -> int:
        """CAS 推进 last_seq（可选同时把 state 从 src 迁到 dst），成功返回新 seq；命中 0 行抛 LeaseLost / SeqConflict。"""
        fence = current_fence(run_id)
        if fence is not None and head.lease_epoch != fence:
            raise LeaseLost(f"run {run_id}: lease epoch {head.lease_epoch} != fence {fence}")
        seq = head.last_seq + 1
        values: dict[str, Any] = {"last_seq": seq}
        if dst is not None:
            values["state"] = dst.value
            if dst.terminal:
                values.update(lease_owner=None, lease_expires_at=None)
        conds: list[ColumnElement[bool]] = [Run.run_id == run_id, Run.last_seq == head.last_seq]
        if src is not None:
            conds.append(Run.state == src.value)
        if fence is not None:
            conds.append(Run.lease_epoch == fence)
        res = cast(
            CursorResult[Any],
            await self.s.execute(update(Run).where(*conds).values(**values).execution_options(synchronize_session=False)),
        )
        if res.rowcount != 1:
            now = await self._head(run_id, locking=True)
            if fence is not None and now.lease_epoch != fence:
                raise LeaseLost(f"run {run_id}: lease epoch {now.lease_epoch} != fence {fence}")
            raise SeqConflict(f"run {run_id}: expected last_seq={head.last_seq} state={src}, now last_seq={now.last_seq} state={now.state}")
        # 会话里若缓存着该 Run 对象，同步其已提交值（不标脏），保证随后的 s.get(Run) 读到新值
        cached = self.s.identity_map.get(identity_key(Run, run_id))
        if cached is not None:
            for key, value in values.items():
                set_committed_value(cached, key, value)
        f = _fence.get()
        if fence is not None and f is not None:  # 提交后才计入 FenceWrites（见 _fence_writes_committed）
            self.s.info.setdefault(_PENDING, {})[id(f.writes)] = (f.writes, seq)
        return seq

    async def _insert(
        self, run_id: str, seq: int, type_: RunEventType, payload: dict[str, Any] | None, idempotency_key: str | None
    ) -> RunEvent:
        row = RunEventRow(run_id=run_id, seq=seq, type=type_.value, payload=payload or {}, idempotency_key=idempotency_key)
        self.s.add(row)
        await self.s.flush()
        return RunEvent(run_id=run_id, seq=seq, type=type_, payload=row.payload, idempotency_key=idempotency_key, ts=row.ts)

    async def append(
        self, run_id: str, type_: RunEventType, payload: dict[str, Any] | None = None, idempotency_key: str | None = None
    ) -> RunEvent:
        """追加一条事件：seq = last_seq + 1，以 CAS（及 fence）推进 t_run.last_seq 后插入事件行，同一事务内完成，
        保证同一 run 内的 seq 严格单调递增、不重不漏。并发写者抢先抛 SeqConflict，租约已失抛 LeaseLost。"""
        head = await self._head(run_id)
        seq = await self._advance(run_id, head)
        return await self._insert(run_id, seq, type_, payload, idempotency_key)

    async def transition(self, run_id: str, dst: RunState, reason: str = "") -> RunState:
        """把 Run 状态迁移到 dst：若已经处于 dst 则直接返回（幂等）；否则校验迁移合法性
        （非法迁移抛 ValueError），合法时以同一条 CAS UPDATE（附加 ``state=src``）更新 state 与 last_seq，
        并追加一条 RUN_STATE_CHANGED 事件。终态同时清空租约并删除凭据，由调用方统一提交或回滚。
        读到的状态已被并发改变时抛 SeqConflict（不返回 False：
        现有调用方都不检查返回值，静默失败会让 Run 停在错误状态）。"""
        head = await self._head(run_id)
        src = RunState(head.state)
        if src == dst:
            return dst
        if not can_transition(src, dst):
            raise ValueError(f"illegal transition {src} -> {dst}")
        seq = await self._advance(run_id, head, src=src, dst=dst)
        await self._insert(run_id, seq, RunEventType.RUN_STATE_CHANGED, {"from": src.value, "to": dst.value, "reason": reason}, None)
        if dst.terminal:
            await self.s.execute(delete(RunCredential).where(RunCredential.run_id == run_id))
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

    # ---- 写工具操作登记（t_tool_operation，D111） ----

    async def record_operation(
        self, run_id: str, operation_id: str, call_id: str, *, status: str = "executing", node: str | None = None
    ) -> ToolOperation:
        """在本会话当前事务内登记一次写工具操作（应与 TOOL_EXECUTING 的 append 同事务、先于它调用并一起提交）。
        epoch 取当前 fence（无 fence 为 0）。operation_id 已存在抛 OperationExists——此时调用方须 rollback，
        不得提交同事务里的 TOOL_EXECUTING，转去重（已 executed）或对账。"""
        if await self.s.get(ToolOperation, operation_id) is not None:
            raise OperationExists(operation_id)
        op = ToolOperation(
            operation_id=operation_id, run_id=run_id, call_id=call_id, status=status, node=node, epoch=current_fence(run_id) or 0
        )
        self.s.add(op)
        try:
            await self.s.flush()
        except IntegrityError as exc:  # 并发登记：另一事务抢先插入同一主键
            raise OperationExists(operation_id) from exc
        return op

    async def get_operation(self, operation_id: str) -> ToolOperation | None:
        """按 operation_id 读登记行（去重 / 对账用）。"""
        return await self.s.get(ToolOperation, operation_id)

    async def update_operation(self, operation_id: str, status: str, result_hash: str | None = None, *, pending_only: bool = False) -> bool:
        """更新登记行的状态与结果指纹（不受 fence 约束：失租旧 Worker 的执行结果也要写回，供对账卡一键确认）。
        返回是否命中。"""
        values: dict[str, Any] = {"status": status}
        if result_hash is not None:
            values["result_hash"] = result_hash
        statement = update(ToolOperation).where(ToolOperation.operation_id == operation_id)
        if pending_only:
            statement = statement.where(ToolOperation.status.in_(("executing", "unknown")))
        res = cast(
            CursorResult[Any],
            await self.s.execute(statement.values(**values).execution_options(synchronize_session=False)),
        )
        cached = self.s.identity_map.get(identity_key(ToolOperation, operation_id))
        if cached is not None and res.rowcount == 1:
            for k, v in values.items():
                set_committed_value(cached, k, v)
        return res.rowcount == 1

    @staticmethod
    def project_unified_events(log: builtins.list[RunEvent]) -> builtins.list[Event]:
        """Turn the Run event log into the unified Event stream a harness can be resumed from.
        （把 Run 事件日志投影为统一的 Event 流，供 harness.resume 用于重放重建会话——
        这是"checkpoint 通过重放实现"这一机制在数据层面的具体转换：只挑选模型文本响应、
        工具请求、工具结果三类事件转换为对应的统一事件，其余管理类事件（如状态迁移）不参与投影。）
        """
        out: builtins.list[dict[str, Any]] = []  # 本类的 list 方法遮蔽了内置名，故用 builtins.list
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


# ---- 租约原语（供 WP-B Worker / reaper；每个函数自开会话、单独提交；时间一律取数据库时钟） ----


def _clock(dialect: str, ttl_s: float = 0) -> Any:
    """数据库时钟：MySQL 用 UTC_TIMESTAMP(6)（加 ttl 用 TIMESTAMPADD），各节点以同一时钟判断到期；
    SQLite 无可比的服务端时钟（单机开发 / 测试），用本进程 UTC 时间兼容实现。"""
    if dialect == "mysql":
        now = func.utc_timestamp(6)
        return func.timestampadd(text("MICROSECOND"), int(ttl_s * 1_000_000), now) if ttl_s else now
    return datetime.now(UTC) + timedelta(seconds=ttl_s)


def _unleased(dialect: str) -> ColumnElement[bool]:
    """无有效租约：无持有者，或到期时间已过（数据库时钟）。"""
    return or_(Run.lease_owner.is_(None), Run.lease_expires_at.is_(None), Run.lease_expires_at < _clock(dialect))


async def acquire_lease(run_id: str, owner: str, ttl_s: float) -> int | None:
    """抢占 Run 的租约：state ∈ {submitted, working} 且无有效租约时，置持有者为 owner、epoch+1、到期 = now+ttl；
    成功返回新 epoch（作为 fence_scope 的参数），Run 不存在 / 不可租 / 他人持有有效租约返回 None。"""
    async with db.SessionLocal() as s:
        dialect = s.get_bind().dialect.name
        res = cast(
            CursorResult[Any],
            await s.execute(
                update(Run)
                .where(Run.run_id == run_id, Run.state.in_(LEASABLE_STATES), _unleased(dialect))
                .values(lease_owner=owner, lease_epoch=Run.lease_epoch + 1, lease_expires_at=_clock(dialect, ttl_s))
                .execution_options(synchronize_session=False)
            ),
        )
        if res.rowcount != 1:
            await s.rollback()
            return None
        epoch = (await s.execute(select(Run.lease_epoch).where(Run.run_id == run_id))).scalar_one()
        await s.commit()
        return int(epoch)


async def claim_new_run(s: AsyncSession, run_id: str, owner: str, ttl_s: float) -> int:
    """在调用方事务内为刚插入（尚未提交）的 Run 直接置租约（owner、epoch+1、到期 = now+ttl，数据库时钟），返回新 epoch。
    供 /v1/messages 创建即持租：与 t_run 插入同事务提交，reaper / Worker 在此之前看不到无租约的窗口。"""
    dialect = s.get_bind().dialect.name
    await s.execute(
        update(Run)
        .where(Run.run_id == run_id)
        .values(lease_owner=owner, lease_epoch=Run.lease_epoch + 1, lease_expires_at=_clock(dialect, ttl_s))
        .execution_options(synchronize_session=False)
    )
    return int((await s.execute(select(Run.lease_epoch).where(Run.run_id == run_id))).scalar_one())


async def renew_lease(run_id: str, owner: str, epoch: int, ttl_s: float) -> bool:
    """续期（心跳）：仅当持有者与 epoch 都未变时把到期时间推到 now+ttl；返回 False 表示租约已失，应自我隔离。
    终态拒绝续期；调用方区分同纪元终态清理与真正失租，并继续兜底取消中断。"""
    async with db.SessionLocal() as s:
        dialect = s.get_bind().dialect.name
        res = cast(
            CursorResult[Any],
            await s.execute(
                update(Run)
                .where(Run.run_id == run_id, Run.lease_owner == owner, Run.lease_epoch == epoch, Run.state.not_in(TERMINAL_STATES))
                .values(lease_expires_at=_clock(dialect, ttl_s))
                .execution_options(synchronize_session=False)
            ),
        )
        await s.commit()
        return res.rowcount == 1


async def release_lease(run_id: str, owner: str, epoch: int) -> bool:
    """释放匹配的租约；相同 epoch 的终态已原子清空时也返回成功，接管后的旧 epoch 不能命中。"""
    async with db.SessionLocal() as s:
        res = cast(
            CursorResult[Any],
            await s.execute(
                update(Run)
                .where(
                    Run.run_id == run_id,
                    Run.lease_epoch == epoch,
                    or_(
                        Run.lease_owner == owner,
                        and_(Run.state.in_(TERMINAL_STATES), Run.lease_owner.is_(None), Run.lease_expires_at.is_(None)),
                    ),
                )
                .values(lease_owner=None, lease_expires_at=None)
                .execution_options(synchronize_session=False)
            ),
        )
        await s.commit()
        return res.rowcount == 1


async def expired_unleased_runs(limit: int = 100) -> builtins.list[str]:
    """reaper 用：durable=1、state ∈ {submitted, working} 且无有效租约的 Run，按创建时间升序，最多 limit 个。"""
    async with db.SessionLocal() as s:
        dialect = s.get_bind().dialect.name
        rows = await s.execute(
            select(Run.run_id)
            .where(Run.durable == true(), Run.state.in_(LEASABLE_STATES), _unleased(dialect))
            .order_by(Run.created_at, Run.run_id)
            .limit(limit)
        )
        return [str(r) for r in rows.scalars()]


async def lease_info(run_id: str) -> tuple[str | None, int, datetime | None]:
    """读取 (lease_owner, lease_epoch, lease_expires_at)；Run 不存在抛 RunNotFound。"""
    async with db.SessionLocal() as s:
        row = (await s.execute(select(Run.lease_owner, Run.lease_epoch, Run.lease_expires_at).where(Run.run_id == run_id))).first()
    if row is None:
        raise RunNotFound(run_id)
    return row[0], int(row[1]), row[2]

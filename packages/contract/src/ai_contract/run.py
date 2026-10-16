"""Run 状态机（采用 A2A v1.0 task-state 命名，即 A2A 状态命名规范）以及追加写（append-only）的
Run 事件日志模型。状态命名与 A2A 对齐，便于将来接入，但仅命名对齐不构成协议兼容：
A2A 的消息、产物与流事件仍需独立的协议适配层（后置工作包），而事件日志是整个 Run
生命周期的唯一事实来源（single source of truth），也是 checkpoint 重放的数据基础。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class RunState(StrEnum):
    """States named after the A2A task lifecycle. Naming alignment only; A2A protocol compatibility
    still requires a separate adapter layer for messages, artifacts and stream events.
    （状态命名与 A2A 任务生命周期对齐；仅命名对齐，协议兼容仍需独立适配层。）
    （状态命名对齐 A2A task 生命周期，使 Run API 同时可作为 A2A task 端点使用。）
    """

    SUBMITTED = "submitted"  # 已提交，尚未开始执行
    WORKING = "working"  # 正在执行（驱动模型/工具循环）
    AUTH_REQUIRED = "auth-required"  # awaiting human approval of a high-risk write（等待人工审批高风险写操作）
    INPUT_REQUIRED = "input-required"  # awaiting user clarification（等待用户澄清输入）
    COMPLETED = "completed"  # 已正常完成，终态
    FAILED = "failed"  # 已失败，终态
    CANCELED = "canceled"  # 已取消，终态

    @property
    def terminal(self) -> bool:
        """是否为终态（终态不再允许任何状态迁移）。"""
        return self in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELED}


# 合法状态迁移表：key 为源状态，value 为该状态允许迁移到的目标状态集合；终态迁移到任何状态都非法。
TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.SUBMITTED: frozenset({RunState.WORKING, RunState.CANCELED, RunState.FAILED}),
    RunState.WORKING: frozenset({RunState.AUTH_REQUIRED, RunState.INPUT_REQUIRED, RunState.COMPLETED, RunState.FAILED, RunState.CANCELED}),
    RunState.AUTH_REQUIRED: frozenset({RunState.WORKING, RunState.FAILED, RunState.CANCELED}),
    RunState.INPUT_REQUIRED: frozenset({RunState.WORKING, RunState.FAILED, RunState.CANCELED}),
    RunState.COMPLETED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELED: frozenset(),
}


def can_transition(src: RunState, dst: RunState) -> bool:
    """判断从 src 状态是否允许迁移到 dst 状态。"""
    return dst in TRANSITIONS[src]


class RunEventType(StrEnum):
    """Run 事件日志中每一行记录的事件类型，覆盖 Run 生命周期的全部关键节点。"""

    RUN_CREATED = "run.created"  # Run 已创建
    RUN_STATE_CHANGED = "run.state_changed"  # Run 状态发生迁移
    CONTEXT_ASSEMBLED = "context.assembled"  # 本轮上下文已按预算组装完成
    MODEL_REQUEST = "model.request"  # 即将发起一次模型请求
    MODEL_RESPONSE = "model.response"  # 收到模型响应
    TOOL_REQUESTED = "tool.requested"  # 模型请求调用工具
    TOOL_DECISION = "tool.decision"  # 工具闸口（gate）做出的决策
    TOOL_APPROVED = "tool.approved"  # 工具调用已获人工批准
    TOOL_REJECTED = "tool.rejected"  # 工具调用被人工拒绝
    TOOL_EXECUTING = "tool.executing"  # 工具即将执行（写前记录，用于识别"已执行但未落日志"的结果未知窗口）
    TOOL_EXECUTED = "tool.executed"  # 工具已实际执行
    TOOL_RESULT = "tool.result"  # 工具执行结果已产生
    CONTEXT_COMPACTED = "context.compacted"  # 上下文历史被压缩/摘要
    INPUT_REQUESTED = "input.requested"  # Run 请求用户补充输入（与 InputRequiredEvent 对应）
    INPUT_PROVIDED = "input.provided"  # 用户已补充输入，Run 可由 input-required 续跑
    RUN_ERROR = "run.error"  # Run 出现错误
    RUN_DONE = "run.done"  # Run 正常结束


class RunEvent(BaseModel):
    """One row of the append-only event log. (run_id, seq) is the primary key.
    （追加写事件日志中的一行记录；(run_id, seq) 组合为主键，seq 在同一 run 内严格递增，
    是重放/断点续跑与幂等判断的基础。）
    """

    model_config = {"extra": "forbid"}

    run_id: str  # 所属 Run 标识
    seq: int  # 同一 run 内严格递增的序号
    type: RunEventType  # 事件类型
    payload: dict[str, Any] = Field(default_factory=dict)  # 事件负载数据
    idempotency_key: str | None = None  # 幂等键，用于避免同一写操作被重复执行
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))  # 事件产生时间（UTC）

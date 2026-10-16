"""统一流式事件模型（Runtime Contract 六要素之一：Event Stream 事件流）。

同一套事件类型在三处被复用：Harness → Runtime（harness 产出模型/工具事件）、
Runtime → SSE 客户端（前端通过 Server-Sent Events 接收）、以及 Run 事件日志（作为
`state.eventlog` 投影 replay 所依赖的统一事件表示）。三处共用同一模型可以避免适配层
的重复转换与语义漂移。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


class EventType(StrEnum):
    """事件类型判别值，用作 `Event` 联合类型的 discriminator（判别字段）。"""

    TEXT = "text"  # 模型输出的文本增量
    THINKING = "thinking"  # 模型的思维链/推理增量（如支持）
    TOOL_USE = "tool_use"  # 模型请求调用一个工具
    TOOL_RESULT = "tool_result"  # 工具执行结果回传给模型
    CARD = "card"  # 富交互卡片（如审批预览）
    USAGE = "usage"  # 本轮的 token 用量与成本统计
    ERROR = "error"  # 错误事件
    DONE = "done"  # 本次 turn/run 结束标记


class _Base(BaseModel):
    """所有事件模型的公共基类：禁止未声明字段，保证事件 schema 的严格性。"""

    model_config = {"extra": "forbid"}


class TextEvent(_Base):
    """模型文本输出的增量片段（流式追加）。"""

    type: Literal[EventType.TEXT] = EventType.TEXT
    delta: str  # 本次追加的文本片段


class ThinkingEvent(_Base):
    """模型思维链/推理过程的增量片段（流式追加）。"""

    type: Literal[EventType.THINKING] = EventType.THINKING
    delta: str  # 本次追加的思维文本片段


class ToolUseEvent(_Base):
    """模型请求调用某个工具（harness 产出，经由 gate 判定后由 runtime 执行）。"""

    type: Literal[EventType.TOOL_USE] = EventType.TOOL_USE
    call_id: str  # 本次调用的唯一标识，贯穿请求/决策/执行/结果全流程
    tool_name: str  # 目标工具名
    input: dict[str, Any] = Field(default_factory=dict)  # 工具调用入参


class ToolResultEvent(_Base):
    """工具执行结果（或被拒绝/待审批的状态），回传给模型继续对话。"""

    type: Literal[EventType.TOOL_RESULT] = EventType.TOOL_RESULT
    call_id: str  # 对应的调用标识
    status: Literal["ok", "error", "denied", "pending_approval"] = "ok"  # 执行结果状态
    output: Any = None  # 结果内容（可能是已截断/摘要后的内容）
    output_ref: str | None = None  # set when the raw output was offloaded to the run workspace（原始结果外置到工作区时的引用）


class CardEvent(_Base):
    """富交互卡片事件，用于向客户端展示结构化内容（如高风险操作的影响预览）。"""

    type: Literal[EventType.CARD] = EventType.CARD
    card_type: str  # e.g. "impact_preview", "table", "link"（卡片类型）
    payload: dict[str, Any] = Field(default_factory=dict)  # 卡片渲染所需的数据


class UsageEvent(_Base):
    """一轮模型调用的 token 用量与成本统计，用于计费和观测。"""

    type: Literal[EventType.USAGE] = EventType.USAGE
    input_tokens: int = 0  # 输入 token 数
    output_tokens: int = 0  # 输出 token 数
    cache_read_tokens: int = 0  # 命中提示缓存的 token 数
    cost: float = 0.0  # 本轮调用成本
    model: str | None = None  # 实际使用的模型标识


class ErrorEvent(_Base):
    """运行期错误事件。"""

    type: Literal[EventType.ERROR] = EventType.ERROR
    code: str  # 错误码
    message: str  # 错误说明
    retryable: bool = False  # 是否可重试


class DoneEvent(_Base):
    """标记一次 turn（或整个 run）结束，携带停止原因。"""

    type: Literal[EventType.DONE] = EventType.DONE
    run_id: str  # 所属 Run 标识
    trace_id: str | None = None  # 可观测性追踪 ID（如 Langfuse trace）
    # 停止原因：正常结束 / 需要调用工具 / 达到最大 token / 被取消 / 出错 / 等待审批或用户输入
    stop_reason: Literal["end_turn", "tool_use", "max_tokens", "cancelled", "error", "awaiting"] = "end_turn"


# Event 为带判别字段的联合类型：Pydantic 依据 "type" 字段自动选择具体子类型进行解析/校验。
Event = Annotated[
    TextEvent | ThinkingEvent | ToolUseEvent | ToolResultEvent | CardEvent | UsageEvent | ErrorEvent | DoneEvent,
    Field(discriminator="type"),
]

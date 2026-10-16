"""Runtime Contract（运行时契约）包的对外导出入口。

本包是事件模型（Event）、Run 状态机（RunState / A2A 任务状态命名）与 Harness 适配器协议
（HarnessAdapter，对应 Runtime Contract 六要素：会话 session、事件流 event stream、工具注入
tool injection、中断 interrupt、hooks、checkpoint）的唯一权威来源（single source of truth）。

平台内的其他所有包（runtime、console、gateway、adapters、SDKs）都依赖本包，并且不得重复声明
这些类型，以保证跨服务的契约一致性。
"""

from .events import (
    CardEvent,
    DoneEvent,
    ErrorEvent,
    Event,
    EventType,
    TextEvent,
    ThinkingEvent,
    ToolResultEvent,
    ToolUseEvent,
    UsageEvent,
)
from .harness import HarnessAdapter, HarnessCapabilities, HookSet, SessionHandle, SessionSpec, TurnInput, capability_tier
from .run import TRANSITIONS, RunEvent, RunEventType, RunState, can_transition
from .tools import RiskLevel, SandboxLevel, SkillDir, ToolSpec

__all__ = [
    "CardEvent",
    "DoneEvent",
    "ErrorEvent",
    "Event",
    "EventType",
    "TextEvent",
    "ThinkingEvent",
    "ToolResultEvent",
    "ToolUseEvent",
    "UsageEvent",
    "HarnessAdapter",
    "HarnessCapabilities",
    "HookSet",
    "SessionHandle",
    "SessionSpec",
    "TurnInput",
    "capability_tier",
    "RunEvent",
    "RunEventType",
    "RunState",
    "TRANSITIONS",
    "can_transition",
    "RiskLevel",
    "SandboxLevel",
    "SkillDir",
    "ToolSpec",
]

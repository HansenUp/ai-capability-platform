"""Runtime Contract（运行时契约）包的对外导出入口。

本包是事件模型（Event）、Run 状态机（RunState / A2A 任务状态命名）与 Harness 适配器协议
（HarnessAdapter，对应 Runtime Contract 六要素：会话 session、事件流 event stream、工具注入
tool injection、中断 interrupt、hooks、checkpoint）的唯一权威来源（single source of truth）。

平台内的其他所有包（runtime、console、gateway、adapters、SDKs）都依赖本包，并且不得重复声明
这些类型，以保证跨服务的契约一致性。
"""

from .errors import ErrorDetail
from .events import (
    CardEvent,
    CardType,
    DoneEvent,
    ErrorEvent,
    Event,
    EventType,
    InputRequiredEvent,
    TextEvent,
    ThinkingEvent,
    ThinkingVisibility,
    ToolResultEvent,
    ToolUseEvent,
    UsageEvent,
)
from .harness import (
    HOOK_OVERRIDABLE,
    HarnessAdapter,
    HarnessCapabilities,
    HookPoint,
    HookSet,
    SessionHandle,
    SessionSpec,
    TurnInput,
    capability_tier,
)
from .images import IMAGE_MEDIA_TYPES, IMAGE_REF_TYPE, ImageRefError, image_ref_path, is_image_ref, load_image_ref, sniff_media_type
from .run import TRANSITIONS, RunEvent, RunEventType, RunState, can_transition
from .tools import RiskLevel, SandboxLevel, SkillDir, ToolSpec

# 契约版本号：Contract V1 冻结后，任何变更须走接口变更单或 ADR，破坏性变更只在阶段边界合并（见 CONTRACT_V1.md）。
CONTRACT_VERSION = "1.3.0"

__all__ = [
    "CONTRACT_VERSION",
    "CardEvent",
    "CardType",
    "DoneEvent",
    "ErrorDetail",
    "ErrorEvent",
    "Event",
    "EventType",
    "InputRequiredEvent",
    "TextEvent",
    "ThinkingEvent",
    "ThinkingVisibility",
    "ToolResultEvent",
    "ToolUseEvent",
    "UsageEvent",
    "HarnessAdapter",
    "HarnessCapabilities",
    "HookSet",
    "HookPoint",
    "HOOK_OVERRIDABLE",
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
    "IMAGE_MEDIA_TYPES",
    "IMAGE_REF_TYPE",
    "ImageRefError",
    "image_ref_path",
    "is_image_ref",
    "load_image_ref",
    "sniff_media_type",
]

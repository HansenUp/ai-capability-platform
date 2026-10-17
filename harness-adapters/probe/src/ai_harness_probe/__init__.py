"""Harness 适配器契约探针套件（S5 §1.3，决策 D85）。只依赖 ai-contract。

用法：适配器提供 `factory(script: ModelScript) -> AsyncContextManager[ProbeTarget]`（假模型按脚本应答并记录
收到的规范化消息），然后 `report = await run_probes(factory)`；`report.passed` 为上架门禁。
"""

from .probes import (
    ALL,
    ENFORCING_HOOK_CHECKS,
    EVENT_STREAM,
    HOOK_DENY_REACHES_MODEL,
    HOOK_NO_MODEL_CALL_AFTER_DENY,
    HOOK_NO_SELF_EXECUTION,
    HOOKS,
    INTERRUPT,
    PROBES,
    REPLAY_RESUME,
    TOOL_ROUNDTRIP,
    ProbeFn,
    ProbeReport,
    ProbeResult,
    run_probes,
)
from .script import ModelReply, ModelScript, ScriptedModel, ScriptedToolCall, ScriptedUsage, ScriptExhausted
from .sim import DEFAULT_SPEC, HookCall, PlatformSim, SimStop, ToolExecution, ToolHandler, messages_hash
from .target import BasicTarget, NormalizedMessages, ProbeFactory, ProbeTarget

__all__ = [
    "ALL",
    "ENFORCING_HOOK_CHECKS",
    "EVENT_STREAM",
    "HOOK_DENY_REACHES_MODEL",
    "HOOK_NO_MODEL_CALL_AFTER_DENY",
    "HOOK_NO_SELF_EXECUTION",
    "HOOKS",
    "INTERRUPT",
    "PROBES",
    "REPLAY_RESUME",
    "TOOL_ROUNDTRIP",
    "ProbeFn",
    "ProbeReport",
    "ProbeResult",
    "run_probes",
    "ModelReply",
    "ModelScript",
    "ScriptedModel",
    "ScriptedToolCall",
    "ScriptedUsage",
    "ScriptExhausted",
    "DEFAULT_SPEC",
    "HookCall",
    "PlatformSim",
    "SimStop",
    "ToolExecution",
    "ToolHandler",
    "messages_hash",
    "BasicTarget",
    "NormalizedMessages",
    "ProbeFactory",
    "ProbeTarget",
]

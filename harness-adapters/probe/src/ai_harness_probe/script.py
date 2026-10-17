"""脚本化模型应答（ModelScript）：探针用它替代真实模型。

被测适配器在探针模式下把"模型"换成按脚本应答的假模型（mock 脚本模式、litellm 假 LLMClient 传输层、
stdio echo 脚本模式、claude-sdk ScriptedCliTransport）。第 i 次模型调用（从 0 起，跨 run_turn、跨
restart() 连续计数）返回 `ModelScript.reply_for(i)`；假模型同时把这次调用实际收到的消息规范化后记入
`ProbeTarget.received()`（格式见 ai_harness_probe.target）。
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel, Field


class ScriptedToolCall(BaseModel):
    """模型在一次调用中请求的一个工具调用。适配器必须原样保留 call_id 与 input 上报为 ToolUseEvent。"""

    model_config = {"extra": "forbid"}

    call_id: str  # 调用标识：适配器上报的 ToolUseEvent.call_id 必须与之相同
    name: str  # 工具名（平台注入的 ToolSpec.name）
    input: dict[str, Any] = Field(default_factory=dict)  # 工具入参


class ScriptedUsage(BaseModel):
    """一次模型调用的用量；探针据此核对适配器上报的 UsageEvent 总量。"""

    model_config = {"extra": "forbid"}

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0  # input_tokens 的子集


class ModelReply(BaseModel):
    """一次模型调用的完整应答，按 thinking → text → tool_calls → usage 的顺序流式产出。

    text / thinking 是增量片段列表（每个元素对应一次流式 delta），探针只比较拼接后的全文。
    hang_after_text 为 True 时，假模型在产出全部 text 片段后挂起、不再结束本次调用，直到被中断
    （用于"流式输出中"中断探针）；此时 tool_calls 与 usage 不会产出。
    """

    model_config = {"extra": "forbid"}

    thinking: list[str] = Field(default_factory=list)  # 思维链增量片段
    text: list[str] = Field(default_factory=list)  # 文本增量片段
    tool_calls: list[ScriptedToolCall] = Field(default_factory=list)  # 本次调用请求的工具（多个时适配器须串行成多轮）
    usage: ScriptedUsage = Field(default_factory=ScriptedUsage)  # 本次调用的用量
    hang_after_text: bool = False  # 产出 text 后挂起直到被中断

    @property
    def full_text(self) -> str:
        """拼接后的全部文本。"""
        return "".join(self.text)

    @property
    def full_thinking(self) -> str:
        """拼接后的全部思维链。"""
        return "".join(self.thinking)


class ScriptExhausted(IndexError):
    """模型调用次数超过脚本长度：说明适配器发起了脚本之外的模型调用（如重复决策）。"""


class ModelScript(BaseModel):
    """按调用顺序排列的模型应答脚本。脚本本身不可变、不带游标：调用计数由持有假模型的 ProbeTarget 维护，
    restart() 不重置计数（重启后的会话继续消费后续应答）。"""

    model_config = {"extra": "forbid", "frozen": True}

    replies: list[ModelReply] = Field(default_factory=list)

    def reply_for(self, call_index: int) -> ModelReply:
        """返回第 call_index 次（从 0 起）模型调用的应答；超出脚本长度抛 ScriptExhausted。"""
        if 0 <= call_index < len(self.replies):
            return self.replies[call_index]
        raise ScriptExhausted(f"model call #{call_index} is beyond the script ({len(self.replies)} replies)")

    def total_usage(self, n_calls: int | None = None) -> ScriptedUsage:
        """前 n_calls 次调用（默认全部）的用量合计，供事件流探针核对 UsageEvent 总量。"""
        replies = self.replies if n_calls is None else self.replies[:n_calls]
        return ScriptedUsage(
            input_tokens=sum(r.usage.input_tokens for r in replies),
            output_tokens=sum(r.usage.output_tokens for r in replies),
            cache_read_tokens=sum(r.usage.cache_read_tokens for r in replies),
        )


class ScriptedModel:
    """按 ModelScript 应答的假模型状态：各适配器的探针模式共用（mock 脚本模式、litellm 假传输层、参考适配器等）。

    `call(messages)` 先把本次调用收到的规范化消息（深拷贝）记入 `calls`，再返回第 len(calls)-1 次应答；
    超出脚本时照样记录后抛 ScriptExhausted（多出来的调用本身就是"重复决策"的证据）。同一个 ScriptedModel
    在 restart() 前后共享，因此调用计数与 received() 记录跨重启连续。
    """

    def __init__(self, script: ModelScript) -> None:
        self.script = script
        self.calls: list[list[dict[str, Any]]] = []  # 每次调用收到的规范化消息，即 ProbeTarget.received()

    def call(self, messages: list[dict[str, Any]]) -> ModelReply:
        """记录一次模型调用并返回脚本应答；超出脚本抛 ScriptExhausted。"""
        self.calls.append(copy.deepcopy(messages))
        return self.script.reply_for(len(self.calls) - 1)

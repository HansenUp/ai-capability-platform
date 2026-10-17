"""Harness 适配器协议（Runtime Contract 六要素：session 会话、event stream 事件流、
tool injection 工具注入、interrupt 中断、hooks 钩子、checkpoint 检查点）。

Checkpoint（检查点/断点续跑）通过*重放 runtime 的事件日志*（replay the runtime event log）
经由 `resume` 实现；具体 harness 自身若原生支持 resume，只是一种性能优化，不是必需能力——
即便 harness 不支持原生续跑，runtime 也能靠重放统一事件流恢复上下文。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from .events import Event
from .tools import SkillDir, ToolSpec

# harness 的传输方式：进程内 / 标准输入输出子进程 / 本地代理 / 远程服务
TransportMode = Literal["in-process", "stdio", "local-proxy", "remote"]


class HookPoint(StrEnum):
    """平台统一执行 hook 的四个拦截点（V1.1 新增）。runner 与工具代理对所有适配器在这四个点执行 hook，
    结果以事件日志为准；适配器只需在 `HarnessCapabilities.internal_hook_points` 声明平台看不到、
    必须由它自己调用的点（如 stdio 帧内部、子代理）。"""

    PRE_MODEL = "pre_model"  # 调用模型前
    POST_MODEL = "post_model"  # 一次模型调用产出完毕（收到 UsageEvent）后
    PRE_TOOL = "pre_tool"  # 执行工具前（工具闸口之前）
    POST_TOOL = "post_tool"  # 工具执行后、结果回灌模型前


# 各拦截点 hook 返回值中允许覆盖 payload 的键白名单（V1.1 新增）。不在白名单内的键一律忽略并记日志；
# 返回 {"deny": reason} 表示阻断，任何点都可用。post_tool 的 data_class 只允许升级、不允许降级。
HOOK_OVERRIDABLE: dict[HookPoint, frozenset[str]] = {
    HookPoint.PRE_MODEL: frozenset(),
    HookPoint.POST_MODEL: frozenset({"thinking_summary"}),
    HookPoint.PRE_TOOL: frozenset({"input"}),
    HookPoint.POST_TOOL: frozenset({"output", "data_class"}),
}


class HarnessCapabilities(BaseModel):
    """描述一个 harness 适配器自身的能力边界，供 registry 与 runtime 做能力判断。"""

    model_config = {"extra": "forbid"}

    name: str  # harness 名称
    version: str  # harness 版本
    transport: TransportMode  # 传输方式
    models: list[str] = Field(
        default_factory=list
    )  # empty = model-agnostic (routed via Model Gateway)（为空表示模型无关，由 Model Gateway 路由）
    supports_vision: bool = False  # 是否支持图像输入
    supports_native_resume: bool = False  # 是否原生支持会话续跑（否则依赖事件日志重放）
    supports_subagents: bool = False  # 是否支持子代理
    tool_protocols: list[str] = Field(default_factory=lambda: ["mcp"])  # 支持的工具协议列表
    # 默认按 harness 持有循环处理（失败关闭）：未显式声明的适配器不被假定可由平台闸口兜底，
    # 因此会落到 L0，只允许只读工具。平台侧适配器必须显式声明 "platform"。
    loop_owner: Literal["platform", "harness"] = "harness"  # 循环归属：轮次循环由平台驱动还是由 harness 自己驱动
    # 执行前拦截强度：none 无拦截，advisory 仅建议（harness 可绕过），enforcing 表示平台的拒绝不可被绕过
    tool_interception: Literal["none", "advisory", "enforcing"] = "none"
    can_resume_from_events: bool = False  # 能否由平台事件日志重建会话继续执行
    # 平台看不到、须由适配器自己调用 hook 的拦截点（V1.1 新增，如 stdio 帧内部、子代理）。runner 传给 run_turn 的
    # HookSet 只包含这里声明的点，其余点由平台统一执行；为空表示全部由平台执行。
    internal_hook_points: list[HookPoint] = Field(default_factory=list)


class SessionSpec(BaseModel):
    """打开或恢复一个 harness 会话所需的完整规格。"""

    model_config = {"extra": "forbid"}

    run_id: str  # 所属 Run 标识
    agent_id: str  # Agent 标识
    agent_version: str  # Agent 版本
    model_alias: str  # pinned model alias resolved by the Model Gateway（由 Model Gateway 解析后固定的模型别名）
    system_prompt: str  # 系统提示词
    subject: str  # "human:<id>" | "agent:<id>" | "app:<id>" | "service:<id>"（发起主体身份）
    tenant_id: str = "default"  # 租户标识
    workspace_dir: str | None = None  # 本次 Run 的工作区目录（用于工具结果外置等）


class SessionHandle(BaseModel):
    """harness 返回的会话句柄，供后续 run_turn/interrupt 引用。"""

    model_config = {"extra": "forbid"}

    session_id: str  # runtime 侧使用的会话标识
    harness: str  # 所属 harness 名称
    native_ref: str | None = None  # harness 原生的会话引用（如原生 session id），可选


class TurnInput(BaseModel):
    """驱动一次模型轮次（turn）所需的输入。"""

    model_config = {"extra": "forbid"}

    messages: list[dict[str, Any]]  # 本轮模型可见的消息历史
    attachments: list[str] = Field(default_factory=list)  # 附件引用列表
    # 历史中工具结果的处理状态：call_id -> ok / error / denied / pending_approval（V1.1 新增，可选，默认空）。
    # 平台按事件日志 TOOL_RESULT 填写，供需要把错误 / 拒绝标记给模型的适配器使用（如 tool_result.is_error）；可忽略。
    tool_statuses: dict[str, Literal["ok", "error", "denied", "pending_approval"]] = Field(default_factory=dict)


# Hook signatures. Returning a dict may override the payload; returning {"deny": reason} blocks.
# （Hook 函数签名：返回 dict 可覆盖原 payload；返回 {"deny": reason} 表示拦截阻断。）
HookFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]


class HookSet(BaseModel):
    """Interception points injected by the runtime guard layer. The harness only calls them.
    （由 runtime 的 guard 层注入的拦截点集合；harness 只负责在对应时机调用它们，不关心其实现。）
    """

    model_config = {"arbitrary_types_allowed": True}

    pre_model: HookFn | None = None  # 调用模型前触发
    post_model: HookFn | None = None  # 一次模型调用产出完毕后触发（V1.1 新增；可覆盖键见 HOOK_OVERRIDABLE）
    pre_tool: HookFn | None = None  # 执行工具前触发（工具闸口顺序中的 hooks 环节）
    post_tool: HookFn | None = None  # 执行工具后触发
    on_subagent: HookFn | None = None  # 触发子代理时调用


@runtime_checkable
class HarnessAdapter(Protocol):
    """所有 harness 适配器（mock / claude-sdk / stdio 等）必须实现的统一协议。"""

    def capabilities(self) -> HarnessCapabilities:
        """返回该 harness 的能力描述。"""
        ...

    async def open_session(self, spec: SessionSpec) -> SessionHandle:
        """按规格开启一个新会话。"""
        ...

    def run_turn(
        self,
        session: SessionHandle,
        turn: TurnInput,
        tools: list[ToolSpec],
        skills: list[SkillDir],
        hooks: HookSet,
    ) -> AsyncIterator[Event]:
        """驱动一次模型轮次，以统一事件流的形式异步产出结果（文本/工具调用/用量/结束等）。"""
        ...

    async def interrupt(self, session: SessionHandle, run_id: str) -> None:
        """中断指定 run 正在进行的轮次。"""
        ...

    async def resume(self, spec: SessionSpec, events: list[Event]) -> SessionHandle:
        """Rebuild a session from the replayed unified event history (checkpoint via replay).
        （基于重放后的统一事件历史重建会话——这就是"通过重放实现 checkpoint"的具体入口。）
        """
        ...


def capability_tier(caps: HarnessCapabilities) -> Literal["L0", "L1", "L2"]:
    """按可治理性把 harness 能力归入三档，供平台决定允许它执行到什么程度的工具。

    - L0：平台无法在执行前强制拦截，只允许只读工具；
    - L1：平台的拒绝不可被绕过（tool_interception=enforcing），可以再允许低风险写；
    - L2：在 L1 之上还能由平台事件日志重建会话继续执行（can_resume_from_events），
      才允许高风险写与审批挂起——因为挂起后必须能从事件日志精确续跑，否则审批形同虚设。
    """
    # 平台持有循环（loop_owner="platform"）时，工具由平台的工具代理执行，闸口必然生效，
    # 恢复也由平台的事件日志重放完成。拦截强度与原生恢复能力此时由平台自身保证，与适配器
    # 自带的钩子无关，因此直接归入 L2。这一条对应 ADR-0008：管控点在工具代理，不在循环内部。
    if caps.loop_owner == "platform":
        return "L2"
    if caps.tool_interception == "enforcing" and caps.can_resume_from_events:
        return "L2"
    if caps.tool_interception == "enforcing":
        return "L1"
    return "L0"

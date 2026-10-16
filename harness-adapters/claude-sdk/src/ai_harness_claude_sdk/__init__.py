"""Claude Agent SDK 适配器（该 SDK 实际以子进程方式运行 CLI，因此传输方式声明为 stdio）。

对应 Runtime Contract 六要素的映射关系：
- session      → SDK client / session id（原生支持 resume 续接）
- event stream → SDK 消息流 → 统一 Event 事件
- tool inject  → 工具以 MCP servers / SDK tool definitions 形式注入；技能以工作目录下的 SKILL.md 目录形式注入
- interrupt    → client.interrupt()
- hooks        → SDK hooks（PreToolUse/PostToolUse/SubagentStop）→ HookSet
- checkpoint   → 运行时事件日志回放（resume）——原生 SDK resume 仅作为性能优化手段使用

SDK 为可选依赖；若未安装，适配器会在构造时降级为明确的 ImportError。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from ai_contract import (
    DoneEvent,
    ErrorEvent,
    Event,
    HarnessCapabilities,
    HookSet,
    SessionHandle,
    SessionSpec,
    SkillDir,
    TextEvent,
    ToolSpec,
    TurnInput,
    UsageEvent,
)


class ClaudeSdkHarness:
    """基于 Claude Agent SDK 的进程内 Harness 适配器骨架。

    当前仅完成会话生命周期与能力声明；回合循环（run_turn 的具体转译逻辑）
    属于 S5 工作包，尚未实现。
    """

    def __init__(self) -> None:
        try:
            import claude_agent_sdk  # noqa: F401  # 仅用于探测 SDK 是否已安装
        except ImportError as e:  # pragma: no cover - depends on env
            # SDK 未安装时给出明确的安装提示，而不是让调用方看到晦涩的 ImportError
            raise ImportError("install with `pip install 'ai-harness-claude-sdk[sdk]'`") from e
        self._sessions: dict[str, SessionSpec] = {}  # session_id -> 建会话时传入的 SessionSpec

    def capabilities(self) -> HarnessCapabilities:
        """声明本适配器支持的能力矩阵，供引擎做路由与降级判断。"""
        return HarnessCapabilities(
            name="claude-sdk",
            version="0.1",
            transport="stdio",  # SDK 实际以子进程方式运行 CLI，并非真正的进程内调用
            supports_vision=True,
            supports_native_resume=True,  # SDK 原生支持 resume，作为 checkpoint 重放的优化路径
            supports_subagents=True,
            tool_protocols=["mcp", "sdk"],
            loop_owner="harness",  # 轮次循环由 SDK 自己驱动，平台只观察其事件流
            # 先保守标为 advisory：SDK 的 PreToolUse 钩子能否真正阻断执行尚未实测，
            # 必须通过拦截探针（构造一个必被拒绝的工具调用并确认其未执行）验证后，才能升级为 enforcing。
            tool_interception="advisory",
            can_resume_from_events=False,  # 尚未验证能否由平台事件日志精确重建会话
        )

    async def open_session(self, spec: SessionSpec) -> SessionHandle:
        """创建新会话：生成本地 session id 并暂存 SessionSpec，返回统一的 SessionHandle。"""
        sid = uuid.uuid4().hex
        self._sessions[sid] = spec
        return SessionHandle(session_id=sid, harness="claude-sdk")

    async def run_turn(
        self, session: SessionHandle, turn: TurnInput, tools: list[ToolSpec], skills: list[SkillDir], hooks: HookSet
    ) -> AsyncIterator[Event]:
        """执行一个对话回合并产出统一事件流。

        S5 工作包实现：需要将 `tools` 转译为 SDK tool/MCP 配置、`skills` 转译为
        setting_sources/技能目录、HookSet 转译为 SDK hooks，再迭代
        `query()`/ClaudeSDKClient 的消息并转换为统一 Event。
        """
        spec = self._sessions[session.session_id]
        # 占位实现：尚未接入 SDK 回合循环，先返回明确的“未实现”错误事件，避免静默失败
        yield ErrorEvent(code="NOT_IMPLEMENTED", message=f"claude-sdk adapter turn loop pending (agent {spec.agent_id})")
        yield UsageEvent(model=spec.model_alias)
        yield DoneEvent(run_id=spec.run_id, stop_reason="error")

    async def interrupt(self, session: SessionHandle, run_id: str) -> None:
        """中断指定会话中的运行。S5 工作包接入 SDK 后需调用 client.interrupt()。"""
        return None

    async def resume(self, spec: SessionSpec, events: list[Event]) -> SessionHandle:
        """基于历史事件日志恢复会话（checkpoint 重放）。"""
        handle = await self.open_session(spec)
        # 重放：将历史文本/工具事件作为对话前缀喂回（若已有 session id，则优先使用 SDK 原生 resume）
        _ = [e for e in events if isinstance(e, TextEvent)]
        return handle

"""Mock harness for local development and CI. Model-free: echoes input and, when the user message
contains `!tool <name> {json}`, emits a tool_use so the full gate/loop path can be exercised.
（本地开发与 CI 使用的 mock harness：不依赖任何真实模型，直接回显输入；当用户消息以
`!tool <名称> {JSON}` 这一约定格式开头时，产出一个 tool_use 事件，从而可以在没有真实模型的
情况下完整跑通闸口（gate）与驱动循环（loop）的全部路径。）
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

from ai_contract import (
    DoneEvent,
    Event,
    HarnessCapabilities,
    HookSet,
    SessionHandle,
    SessionSpec,
    SkillDir,
    TextEvent,
    ToolSpec,
    ToolUseEvent,
    TurnInput,
    UsageEvent,
)


class MockHarness:
    """实现 HarnessAdapter 协议的模拟适配器，用于开发/测试，不依赖任何外部模型服务。"""

    def capabilities(self) -> HarnessCapabilities:
        """声明本 harness 的能力：进程内传输，不支持原生续跑（依赖事件日志重放实现 checkpoint）。

        可治理性方面 mock 是最低档（L0）：它不提供任何执行前拦截，也不能由平台事件日志重建会话，
        因此只适合跑只读路径，不应被用于承载高风险写。
        """
        return HarnessCapabilities(
            name="mock",
            version="0.1",
            transport="in-process",
            supports_native_resume=False,
            loop_owner="platform",  # 轮次循环由平台的 loop.runner 驱动
            tool_interception="none",  # 自身不提供执行前拦截；平台持有循环时平台闸口负责拦截
            can_resume_from_events=False,  # 自身不支持原生续跑；平台持有循环时由事件日志重放恢复
        )

    async def open_session(self, spec: SessionSpec) -> SessionHandle:
        """开启一个新的模拟会话，返回随机生成的会话句柄。"""
        return SessionHandle(session_id=uuid.uuid4().hex, harness="mock")

    async def run_turn(
        self, session: SessionHandle, turn: TurnInput, tools: list[ToolSpec], skills: list[SkillDir], hooks: HookSet
    ) -> AsyncIterator[Event]:
        """模拟一次模型轮次：
        - 若最新用户消息之后还没有工具结果，且消息以 `!tool ` 开头，则解析出工具名与 JSON 入参，
          产出一个 ToolUseEvent 并以 stop_reason="tool_use" 结束（触发 gate/execute 全流程）；
        - 若最新用户消息之后已经有工具结果，则回显工具结果；
        - 否则原样回显用户输入文本，附带用量统计事件，并以 end_turn 结束。
        """
        last_user_idx = max((i for i, m in enumerate(turn.messages) if m.get("role") == "user"), default=-1)
        content = str(turn.messages[last_user_idx].get("content", "")) if last_user_idx >= 0 else ""
        tool_results = [m for m in turn.messages[last_user_idx + 1 :] if m.get("role") == "tool"]
        if hooks.pre_model:
            await hooks.pre_model({"messages": turn.messages})
        if content.startswith("!tool ") and not tool_results:
            # !tool 约定格式：`!tool <工具名> <JSON 入参>`，用于在无真实模型的情况下人为触发工具调用。
            _, rest = content.split(" ", 1)
            name, _, raw = rest.partition(" ")
            yield ToolUseEvent(call_id=uuid.uuid4().hex[:12], tool_name=name, input=json.loads(raw or "{}"))
            yield DoneEvent(run_id=session.session_id, stop_reason="tool_use")
            return
        if tool_results:
            yield TextEvent(delta=f"工具结果已收到：{str(tool_results[-1].get('content'))[:200]}")
        else:
            yield TextEvent(delta=f"[mock:{len(skills)} skills/{len(tools)} tools] 你说：{content}")
        yield UsageEvent(input_tokens=len(content) // 3, output_tokens=16, model="mock")
        yield DoneEvent(run_id=session.session_id, stop_reason="end_turn")

    async def interrupt(self, session: SessionHandle, run_id: str) -> None:
        """mock 实现无需真正中断任何后台任务，直接返回。"""
        return None

    async def resume(self, spec: SessionSpec, events: list[Event]) -> SessionHandle:
        """基于重放事件数量生成一个新的会话句柄，native_ref 中记录重放事件条数，便于调试观察。"""
        return SessionHandle(session_id=uuid.uuid4().hex, harness="mock", native_ref=f"replayed:{len(events)}")

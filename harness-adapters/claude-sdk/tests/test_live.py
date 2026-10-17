"""claude-sdk 适配器的真实联调测试（默认跳过）：经 Claude Code CLI 调本机 LiteLLM 的 Anthropic 兼容端点。

运行：``RUNTIME_LIVE_LLM=1 .venv/bin/pytest harness-adapters/claude-sdk -q -k live -s``
（可用 CLAUDE_SDK_BASE_URL / CLAUDE_SDK_AUTH_TOKEN / CLAUDE_SDK_LIVE_MODEL 覆盖默认网关、凭据与模型别名。）
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("RUNTIME_LIVE_LLM"), reason="set RUNTIME_LIVE_LLM=1 to run live LLM tests")

pytest.importorskip("claude_agent_sdk")

# 以下导入必须在 importorskip 之后（SDK 未安装时整体跳过），因此豁免 E402。
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, Message, SystemMessage  # noqa: E402

from ai_contract import (  # noqa: E402
    DoneEvent,
    HookSet,
    SessionSpec,
    TextEvent,
    ThinkingEvent,
    ToolResultEvent,
    ToolSpec,
    ToolUseEvent,
    TurnInput,
    UsageEvent,
)
from ai_harness_claude_sdk import TOOL_PREFIX, ClaudeSdkHarness  # noqa: E402

MODEL = os.environ.get("CLAUDE_SDK_LIVE_MODEL", "deepseek-v4-pro-2026-08")
ECHO = ToolSpec(
    name="echo",
    description="原样返回 text 参数。",
    input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
)


class RecordingClient:
    """包一层真实 ClaudeSDKClient，记录 CLI 的 system/init 数据与 options，用于断言运行期隔离。"""

    def __init__(self, options: ClaudeAgentOptions) -> None:
        self.options = options
        self.inits: list[dict[str, Any]] = []
        self._c = ClaudeSDKClient(options=options)

    async def connect(self) -> None:
        await self._c.connect()

    async def query(self, prompt: str) -> None:
        await self._c.query(prompt)

    async def receive_response(self) -> AsyncIterator[Message]:
        async for m in self._c.receive_response():
            if isinstance(m, SystemMessage) and m.subtype == "init":
                self.inits.append(m.data)
            yield m

    async def interrupt(self) -> None:
        await self._c.interrupt()

    async def disconnect(self) -> None:
        await self._c.disconnect()

    def pid(self) -> int | None:
        """CLI 子进程 pid（SDK 内部属性，仅用于检查中断后子进程已回收）。"""
        process = getattr(getattr(self._c, "_transport", None), "_process", None)
        return getattr(process, "pid", None)


def recording_harness() -> tuple[ClaudeSdkHarness, list[RecordingClient]]:
    clients: list[RecordingClient] = []

    def factory(options: ClaudeAgentOptions) -> RecordingClient:
        clients.append(RecordingClient(options))
        return clients[-1]

    return ClaudeSdkHarness(client_factory=factory), clients


def assert_isolated(client: RecordingClient, expected_tools: set[str]) -> None:
    """CLI 实际报告的工具只能是平台桥接工具，MCP server 只能是 platform；会话结束后配置目录已删除。"""
    assert client.inits, "CLI did not emit system/init"
    for init in client.inits:
        tools = set(init.get("tools") or [])
        servers = {m.get("name") for m in init.get("mcp_servers") or []}
        print(f"[live] init tools={sorted(tools)} mcp_servers={sorted(servers)}")
        assert all(t.startswith(TOOL_PREFIX) for t in tools) and tools == expected_tools
        assert servers <= {"platform"}
    assert not Path(client.options.env["CLAUDE_CONFIG_DIR"]).exists()


def spec(tmp_path: Any, system_prompt: str) -> SessionSpec:
    return SessionSpec(
        run_id="run_live",
        agent_id="live",
        agent_version="1",
        model_alias=MODEL,
        system_prompt=system_prompt,
        subject="human:live",
        workspace_dir=str(tmp_path / "ws"),
    )


async def run(h: ClaudeSdkHarness, handle: Any, messages: list[dict[str, Any]], tools: list[ToolSpec]) -> list[Any]:
    started = time.monotonic()
    events = [ev async for ev in h.run_turn(handle, TurnInput(messages=messages), tools, [], HookSet())]
    text = "".join(e.delta for e in events if isinstance(e, TextEvent))
    thinking = sum(len(e.delta) for e in events if isinstance(e, ThinkingEvent))
    usage = next((e for e in events if isinstance(e, UsageEvent)), None)
    print(
        f"\n[live] {time.monotonic() - started:.1f}s kinds={[e.type.value for e in events]} text={text!r} "
        f"thinking_chars={thinking} usage={usage.model_dump() if usage else None}"
    )
    assert isinstance(events[-1], DoneEvent) and isinstance(events[-2], UsageEvent)
    return events


async def test_live_plain_text_turn(tmp_path: Any) -> None:
    h, clients = recording_harness()
    handle = await h.open_session(spec(tmp_path, "你是简洁的助手。"))
    events = await run(h, handle, [{"role": "user", "content": "回复一个字：好"}], [])
    assert any(isinstance(e, TextEvent) and e.delta for e in events)
    assert events[-1].stop_reason == "end_turn"
    usage = events[-2]
    assert usage.input_tokens > 0 and usage.output_tokens > 0
    assert_isolated(clients[0], set())


async def test_live_tool_roundtrip(tmp_path: Any) -> None:
    h, clients = recording_harness()
    system = "你必须先调用 echo 工具（参数 text 为用户给出的词），拿到工具结果后再用一句话告诉用户结果。不要跳过工具调用。"
    handle = await h.open_session(spec(tmp_path, system))
    history: list[dict[str, Any]] = [{"role": "user", "content": "请回显这个词：苹果"}]

    first = await run(h, handle, history, [ECHO])
    calls = [e for e in first if isinstance(e, ToolUseEvent)]
    assert len(calls) == 1 and first[-1].stop_reason == "tool_use"
    call = calls[0]
    assert call.tool_name == "echo"
    print(f"[live] tool_use call_id={call.call_id} input={call.input}")

    text = "".join(e.delta for e in first if isinstance(e, TextEvent))
    if text:
        history.append({"role": "assistant", "content": text})
    history.append({"role": "assistant", "tool_use": {"call_id": call.call_id, "name": call.tool_name, "input": call.input}})
    history.append({"role": "tool", "call_id": call.call_id, "content": f"echo: {call.input.get('text', '')}"})

    second = await run(h, handle, history, [ECHO])
    assert second[-1].stop_reason == "end_turn"
    assert any(isinstance(e, TextEvent) and e.delta for e in second)
    assert_isolated(clients[0], {TOOL_PREFIX + "echo"})


async def test_live_structured_resume_from_synthetic_transcript(tmp_path: Any) -> None:
    """S5 spike 的回归用例：审批续跑场景（历史以平台补完的 tool_result 结尾）。适配器把历史合成为 CLI 会话转录，
    真实 CLI 以 --resume + CLAUDE_CODE_RESUME_INTERRUPTED_TURN 自动续跑（与 --no-session-persistence 共存）：
    模型据结构化 tool_result 作答（复述 nonce），不重新调用工具。"""
    nonce = "NONCE-" + uuid.uuid4().hex[:8]
    h, clients = recording_harness()
    events = [
        ToolUseEvent(call_id="toolu_live1", tool_name="echo", input={"text": "x"}),
        ToolResultEvent(call_id="toolu_live1", output=nonce),
    ]
    handle = await h.resume(spec(tmp_path, "你是简洁的助手。"), events)
    assert handle.native_ref is not None and handle.native_ref.startswith("transcript:")
    history: list[dict[str, Any]] = [
        {"role": "user", "content": "调用 echo 工具（text=x），然后只回复工具返回的原文，不要任何其他内容。"},
        {"role": "assistant", "tool_use": {"call_id": "toolu_live1", "name": "echo", "input": {"text": "x"}}},
        {"role": "tool", "call_id": "toolu_live1", "content": nonce},
    ]
    out = await run(h, handle, history, [ECHO])
    text = "".join(e.delta for e in out if isinstance(e, TextEvent))
    assert nonce in text and out[-1].stop_reason == "end_turn"
    assert not any(isinstance(e, ToolUseEvent) for e in out)  # 不重新决策、不重复执行
    assert_isolated(clients[0], {TOOL_PREFIX + "echo"})
    await h.interrupt(handle, "run_live")


async def test_live_interrupt_while_awaiting_reclaims_cli_subprocess(tmp_path: Any) -> None:
    """等待平台结果时中断：CLI 子进程被回收、会话目录删除（S1 实现在此路径上会泄漏子进程）。"""
    h, clients = recording_harness()
    system = "你必须先调用 echo 工具（参数 text 为用户给出的词）。不要跳过工具调用。"
    handle = await h.open_session(spec(tmp_path, system))
    first = await run(h, handle, [{"role": "user", "content": "请回显这个词：苹果"}], [ECHO])
    assert first[-1].stop_reason == "tool_use"
    pid = clients[0].pid()
    assert pid is not None
    await h.interrupt(handle, "run_live")
    assert h._sessions == {}
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert_isolated(clients[0], {TOOL_PREFIX + "echo"})  # 含配置目录已删除

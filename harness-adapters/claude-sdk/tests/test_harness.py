"""claude-sdk 适配器的离线单元测试：用伪 SDK 客户端按脚本产出真实的 SDK 消息对象，不依赖网络与 CLI。

工具调用经由适配器真实注册的进程内 MCP server（SDK 的 SdkMcpBridge，与 CLI 走同一条 JSON-RPC 路径）发起，
因此覆盖了 schema 规范化、handler 桥接与结果内容转换的完整链路。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("claude_agent_sdk")

# 以下导入必须在 importorskip 之后（SDK 未安装时整体跳过），因此统一豁免 E402。
from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)
from claude_agent_sdk._internal.sdk_mcp_bridge import SdkMcpBridge  # noqa: E402

from ai_contract import (  # noqa: E402
    CardEvent,
    DoneEvent,
    ErrorEvent,
    HookSet,
    InputRequiredEvent,
    RiskLevel,
    SessionSpec,
    TextEvent,
    ThinkingEvent,
    ToolResultEvent,
    ToolSpec,
    ToolUseEvent,
    TurnInput,
    UsageEvent,
    capability_tier,
)
from ai_harness_claude_sdk import ClaudeSdkConfig, ClaudeSdkHarness, HarnessInterruptedError, build_prompt  # noqa: E402

MODEL = "deepseek-v4-pro-2026-08"
GW_TOKEN = "tok"  # noqa: S105 —— 测试用假凭据
ECHO = ToolSpec(name="echo", description="回显文本", input_schema={"type": "object", "properties": {"text": {"type": "string"}}})


class FakeClient:
    """按脚本产出 SDK 消息的伪 ClaudeSDKClient；call_tool 通过适配器注册的 MCP server 真实调用工具。"""

    def __init__(self, options: Any, script: Any) -> None:
        self.options = options
        self.script = script
        self.prompt: str | None = None
        self.connected = self.interrupted = self.disconnected = False
        self.tool_results: list[dict[str, Any]] = []
        self._bridge: SdkMcpBridge | None = None
        self._rpc_id = 0

    async def connect(self) -> None:
        self.connected = True

    async def query(self, prompt: str) -> None:
        self.prompt = prompt

    async def receive_response(self) -> Any:
        async for msg in self.script(self):
            yield msg

    async def interrupt(self) -> None:
        self.interrupted = True

    async def disconnect(self) -> None:
        self.disconnected = True
        if self._bridge is not None:
            await self._bridge.aclose()

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        assert self._bridge is not None
        self._rpc_id += 1
        return await self._bridge.handle({"jsonrpc": "2.0", "id": self._rpc_id, "method": method, "params": params or {}})

    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """模拟 CLI 调用 SDK MCP 工具（initialize → tools/call），返回 JSON-RPC result。"""
        if self._bridge is None:
            self._bridge = SdkMcpBridge("platform", self.options.mcp_servers["platform"]["instance"])
            init = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "fake", "version": "0"}}
            await self._rpc("initialize", init)
            await self._bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        resp = await self._rpc("tools/call", {"name": name, "arguments": args})
        assert resp is not None
        result: dict[str, Any] = resp["result"]
        self.tool_results.append(result)
        return result


def make_harness(script: Any, **cfg: Any) -> tuple[ClaudeSdkHarness, list[FakeClient]]:
    clients: list[FakeClient] = []

    def factory(options: Any) -> FakeClient:
        c = FakeClient(options, script)
        clients.append(c)
        return c

    timeout = cfg.pop("timeout", 5.0)
    config = ClaudeSdkConfig(base_url="http://gw.test", auth_token=GW_TOKEN, cli_path=None, max_turns=4, turn_timeout_s=timeout)
    return ClaudeSdkHarness(client_factory=factory, config=config), clients


def make_spec(tmp_path: Any, *, workspace: bool = True) -> SessionSpec:
    return SessionSpec(
        run_id="run_test",
        agent_id="a",
        agent_version="1",
        model_alias=MODEL,
        system_prompt="你是测试助手",
        subject="human:u1",
        workspace_dir=str(tmp_path / "ws") if workspace else None,
    )


def assistant(*blocks: Any, mid: str, inp: int = 0, out: int = 0) -> AssistantMessage:
    return AssistantMessage(content=list(blocks), model=MODEL, message_id=mid, usage={"input_tokens": inp, "output_tokens": out})


def result(
    inp: int,
    out: int,
    cost: float = 0.01,
    *,
    is_error: bool = False,
    text: str | None = None,
    subtype: str = "success",
    stop_reason: str | None = None,
) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        stop_reason=stop_reason,
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="sdk-s",
        total_cost_usd=cost,
        usage={"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": 0},
        result=text,
    )


async def collect(h: ClaudeSdkHarness, handle: Any, messages: list[dict[str, Any]], tools: list[ToolSpec] | None = None) -> list[Any]:
    """跑一个 turn 并做通用契约断言：不产出平台专属事件；以 Done 结束；Done 之前必有 UsageEvent（D5）。"""
    events = [ev async for ev in h.run_turn(handle, TurnInput(messages=messages), tools or [], [], HookSet())]
    assert not any(isinstance(e, ToolResultEvent | CardEvent | InputRequiredEvent) for e in events)
    assert isinstance(events[-1], DoneEvent)
    assert isinstance(events[-2], UsageEvent)
    return events


def exists(path: str | Path) -> bool:
    """同步的路径检查（测试里放在协程外，避免 ASYNC240）。"""
    return Path(path).exists()


def is_dir(path: str | Path) -> bool:
    return Path(path).is_dir()


def kinds(events: list[Any]) -> list[str]:
    return [e.type.value if not isinstance(e, DoneEvent) else f"done:{e.stop_reason}" for e in events]


async def test_capabilities_enforcing_but_not_replayable() -> None:
    h, _ = make_harness(None)
    caps = h.capabilities()
    assert (caps.loop_owner, caps.tool_interception, caps.can_resume_from_events) == ("harness", "enforcing", False)
    assert capability_tier(caps) == "L1"


async def test_text_turn_event_order(tmp_path: Any) -> None:
    """纯文本轮次：Thinking → Text → Usage → Done(end_turn)，用量与成本取自 ResultMessage。"""

    async def script(c: FakeClient) -> Any:
        yield assistant(ThinkingBlock(thinking="想一想", signature="sig"), TextBlock(text="好"), mid="m1", inp=10, out=3)
        yield result(10, 3, cost=0.02)

    h, clients = make_harness(script)
    handle = await h.open_session(make_spec(tmp_path))
    events = await collect(h, handle, [{"role": "user", "content": "回复一个字：好"}])
    assert kinds(events) == ["thinking", "text", "usage", "done:end_turn"]
    assert events[0].delta == "想一想" and events[1].delta == "好"
    usage = events[2]
    assert (usage.input_tokens, usage.output_tokens, usage.cost, usage.model) == (10, 3, 0.02, MODEL)
    c = clients[0]
    assert c.prompt == "回复一个字：好" and c.connected and c.disconnected
    assert (tmp_path / "ws").is_dir()  # 工作区不存在时自动创建
    assert c.options.system_prompt == "你是测试助手" and c.options.model == MODEL


async def test_stream_deltas_are_not_duplicated_by_final_message(tmp_path: Any) -> None:
    """开启 include_partial_messages 时：增量逐条产出，随后到达的完整 AssistantMessage 不再重复产出。"""

    def se(event: dict[str, Any]) -> StreamEvent:
        return StreamEvent(uuid="u", session_id="s", event=event)

    async def script(c: FakeClient) -> Any:
        yield se({"type": "message_start", "message": {"id": "m1"}})
        yield se({"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "嗯"}})
        yield se({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "你"}})
        yield se({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "好"}})
        yield assistant(ThinkingBlock(thinking="嗯", signature="s"), TextBlock(text="你好"), mid="m1", inp=5, out=2)
        yield result(5, 2)

    h, _ = make_harness(script)
    handle = await h.open_session(make_spec(tmp_path))
    events = await collect(h, handle, [{"role": "user", "content": "hi"}])
    assert kinds(events) == ["thinking", "text", "text", "usage", "done:end_turn"]
    assert "".join(e.delta for e in events if isinstance(e, TextEvent)) == "你好"


async def test_streamed_tool_turn_usage_includes_message_delta(tmp_path: Any) -> None:
    """流式模式下 AssistantMessage 的用量来自 message_start（output=0）；message_delta 先于工具调用到达时，
    中途上报应合并其输出量，且结束时不重复计。"""

    async def script(c: FakeClient) -> Any:
        yield StreamEvent(uuid="u", session_id="s", event={"type": "message_start", "message": {"id": "m1", "usage": {"input_tokens": 9}}})
        yield StreamEvent(uuid="u", session_id="s", event={"type": "message_delta", "usage": {"output_tokens": 7}})
        yield assistant(ToolUseBlock(id="toolu_1", name="mcp__platform__echo", input={"text": "x"}), mid="m1", inp=9, out=0)
        await c.call_tool("echo", {"text": "x"})
        yield result(9, 7)

    h, _ = make_harness(script)
    handle = await h.open_session(make_spec(tmp_path))
    first = await collect(h, handle, [{"role": "user", "content": "go"}], [ECHO])
    assert (first[-2].input_tokens, first[-2].output_tokens) == (9, 7)
    second = await collect(h, handle, [{"role": "user", "content": "go"}, {"role": "tool", "call_id": "toolu_1", "content": "x"}], [ECHO])
    assert (second[-2].input_tokens, second[-2].output_tokens) == (0, 0)  # 总量已在中途上报，不重复计


async def test_tool_bridge_roundtrip(tmp_path: Any) -> None:
    """工具桥接：handler 挂起 → 第一轮 ToolUse + Usage + Done(tool_use) → 第二轮带回结果 → Future 解析 → 最终 Usage + Done。"""

    async def script(c: FakeClient) -> Any:
        block = ToolUseBlock(id="toolu_1", name="mcp__platform__echo", input={"text": "hi"})
        yield assistant(TextBlock(text="我来调用 echo"), block, mid="m1", inp=20, out=8)
        res = await c.call_tool("echo", {"text": "hi"})
        yield assistant(TextBlock(text="结果：" + res["content"][0]["text"]), mid="m2", inp=30, out=4)
        yield result(50, 12, cost=0.05)

    h, clients = make_harness(script)
    handle = await h.open_session(make_spec(tmp_path))
    history: list[dict[str, Any]] = [{"role": "user", "content": "请回显 hi"}]

    first = await collect(h, handle, history, [ECHO])
    assert kinds(first) == ["text", "tool_use", "usage", "done:tool_use"]
    call = first[1]
    assert (call.call_id, call.tool_name, call.input) == ("toolu_1", "echo", {"text": "hi"})  # call_id 沿用 SDK 的 tool_use id
    assert (first[2].input_tokens, first[2].output_tokens, first[2].cost) == (20, 8, 0.0)
    await asyncio.sleep(0.05)
    assert clients[0].tool_results == []  # 平台给结果之前，SDK 侧的工具调用一直挂起

    history += [
        {"role": "assistant", "content": "我来调用 echo"},
        {"role": "assistant", "tool_use": {"call_id": "toolu_1", "name": "echo", "input": {"text": "hi"}}},
        {"role": "tool", "call_id": "toolu_1", "content": {"echo": "hi"}},
    ]
    second = await collect(h, handle, history, [ECHO])
    assert kinds(second) == ["text", "usage", "done:end_turn"]
    assert second[0].delta == '结果：{"echo": "hi"}'  # 非字符串结果按 JSON 文本交给 SDK
    assert (second[1].input_tokens, second[1].output_tokens, second[1].cost) == (30, 4, 0.05)  # 总量减去中途已上报
    assert clients[0].tool_results == [{"content": [{"type": "text", "text": '{"echo": "hi"}'}], "isError": False}]
    assert h._sessions == {}  # 查询结束即释放会话


async def test_interception_probe_platform_denial_reaches_sdk_without_execution(tmp_path: Any) -> None:
    """拦截探针（tool_interception=enforcing 的证据）：

    1) SDK 被隔离到只能调用平台桥接工具：无内置工具、无本机设置/插件/外部 MCP、未放行即拒绝；
    2) 高风险写工具被调用时，harness 不执行任何逻辑，只把调用交给平台并挂起；
    3) 平台判定 denied 后，SDK 拿到的工具结果就是平台的拒绝内容原文，而不是任何执行产物。
    """
    wipe = ToolSpec(name="wipe_database", description="清空数据库", risk_level=RiskLevel.WRITE_HIGH)
    denial = "denied: tool 'wipe_database' blocked by redline policy"

    async def script(c: FakeClient) -> Any:
        yield assistant(ToolUseBlock(id="toolu_w", name="mcp__platform__wipe_database", input={}), mid="m1", inp=1, out=1)
        res = await c.call_tool("wipe_database", {})
        yield assistant(TextBlock(text="收到：" + res["content"][0]["text"]), mid="m2")
        yield result(2, 2)

    h, clients = make_harness(script)
    handle = await h.open_session(make_spec(tmp_path))
    history: list[dict[str, Any]] = [{"role": "user", "content": "把库清掉"}]
    first = await collect(h, handle, history, [wipe])
    assert kinds(first) == ["tool_use", "usage", "done:tool_use"]

    opts = clients[0].options
    assert opts.tools == []  # 内置工具全部禁用
    assert opts.allowed_tools == ["mcp__platform__wipe_database"]
    assert set(opts.mcp_servers) == {"platform"} and opts.strict_mcp_config
    assert opts.setting_sources == [] and opts.permission_mode == "dontAsk"
    assert opts.env["ANTHROPIC_API_KEY"] == "" and opts.env["ANTHROPIC_BASE_URL"] == "http://gw.test"

    await asyncio.sleep(0.05)
    assert clients[0].tool_results == []  # 平台未裁决前没有任何结果产生，更没有执行

    history += [
        {"role": "assistant", "tool_use": {"call_id": "toolu_w", "name": "wipe_database", "input": {}}},
        {"role": "tool", "call_id": "toolu_w", "content": denial},
    ]
    second = await collect(h, handle, history, [wipe])
    assert kinds(second) == ["text", "usage", "done:end_turn"]
    assert clients[0].tool_results == [{"content": [{"type": "text", "text": denial}], "isError": False}]
    assert second[0].delta == "收到：" + denial


async def test_missing_tool_result_ends_turn_with_error(tmp_path: Any) -> None:
    async def script(c: FakeClient) -> Any:
        yield assistant(ToolUseBlock(id="toolu_1", name="mcp__platform__echo", input={"text": "x"}), mid="m1")
        await c.call_tool("echo", {"text": "x"})
        yield result(1, 1)

    h, clients = make_harness(script)
    handle = await h.open_session(make_spec(tmp_path))
    await collect(h, handle, [{"role": "user", "content": "go"}], [ECHO])
    events = await collect(h, handle, [{"role": "user", "content": "go"}], [ECHO])  # 没带回 tool 结果
    assert kinds(events) == ["error", "usage", "done:error"]
    assert "toolu_1" in events[0].message
    assert clients[0].disconnected and h._sessions == {}


async def test_interrupt_ends_pending_future(tmp_path: Any) -> None:
    async def script(c: FakeClient) -> Any:
        yield assistant(ToolUseBlock(id="toolu_1", name="mcp__platform__echo", input={"text": "x"}), mid="m1")
        await c.call_tool("echo", {"text": "x"})
        yield result(1, 1)

    h, clients = make_harness(script)
    handle = await h.open_session(make_spec(tmp_path))
    first = await collect(h, handle, [{"role": "user", "content": "go"}], [ECHO])
    assert kinds(first)[-1] == "done:tool_use"
    session = h._sessions[handle.session_id]
    futures = list(session.pending.values())
    task = session.task
    assert len(futures) == 1 and not futures[0].done()

    await h.interrupt(handle, "run_test")
    assert futures[0].done() and isinstance(futures[0].exception(), HarnessInterruptedError)
    assert task is not None and task.done()
    assert clients[0].interrupted and clients[0].disconnected
    assert h._sessions == {}
    after = await collect(h, handle, [{"role": "user", "content": "go"}], [ECHO])  # 已释放的会话给出合规的错误轮次
    assert kinds(after) == ["error", "usage", "done:error"]


async def test_sdk_exception_maps_to_harness_error(tmp_path: Any) -> None:
    async def script(c: FakeClient) -> Any:
        raise RuntimeError("CLI exited with code 1")
        yield  # pragma: no cover

    h, _ = make_harness(script)
    handle = await h.open_session(make_spec(tmp_path))
    events = await collect(h, handle, [{"role": "user", "content": "hi"}])
    assert kinds(events) == ["error", "usage", "done:error"]
    assert isinstance(events[0], ErrorEvent) and events[0].code == "HARNESS_ERROR" and "CLI exited" in events[0].message


async def test_result_error_maps_to_harness_error(tmp_path: Any) -> None:
    async def script(c: FakeClient) -> Any:
        yield result(3, 0, is_error=True, text="API Error: 401 invalid key")

    h, _ = make_harness(script)
    handle = await h.open_session(make_spec(tmp_path))
    events = await collect(h, handle, [{"role": "user", "content": "hi"}])
    assert kinds(events) == ["error", "usage", "done:error"]
    assert "401" in events[0].message and events[1].input_tokens == 3


async def test_turn_timeout_yields_error_turn(tmp_path: Any) -> None:
    async def script(c: FakeClient) -> Any:
        await asyncio.sleep(10)
        yield result(0, 0)  # pragma: no cover

    h, clients = make_harness(script, timeout=0.1)
    handle = await h.open_session(make_spec(tmp_path))
    events = await collect(h, handle, [{"role": "user", "content": "hi"}])
    assert kinds(events) == ["error", "usage", "done:error"]
    assert clients[0].disconnected


async def test_fallback_call_id_when_tool_block_not_seen(tmp_path: Any) -> None:
    """异常路径：handler 等不到对应 ToolUseBlock 时自生成 call_id，流程照常。"""

    async def script(c: FakeClient) -> Any:
        res = await c.call_tool("echo", {"text": "x"})
        yield assistant(TextBlock(text=res["content"][0]["text"]), mid="m1")
        yield result(1, 1)

    h, _ = make_harness(script)
    h.block_wait_s = 0.05
    handle = await h.open_session(make_spec(tmp_path))
    first = await collect(h, handle, [{"role": "user", "content": "go"}], [ECHO])
    call = first[0]
    assert isinstance(call, ToolUseEvent) and call.call_id.startswith("call_")
    msgs = [{"role": "user", "content": "go"}, {"role": "tool", "call_id": call.call_id, "content": "ok"}]
    second = await collect(h, handle, msgs, [ECHO])
    assert kinds(second) == ["text", "usage", "done:end_turn"] and second[0].delta == "ok"


async def test_resume_renders_history_into_prompt(tmp_path: Any) -> None:
    async def script(c: FakeClient) -> Any:
        yield assistant(TextBlock(text="继续"), mid="m1")
        yield result(1, 1)

    h, clients = make_harness(script)
    handle = await h.resume(make_spec(tmp_path), [])
    assert handle.native_ref == "replayed:0"
    msgs = [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
        {"role": "user", "content": "第二问"},
        {"role": "assistant", "tool_use": {"call_id": "c1", "name": "echo", "input": {"text": "a"}}},
        {"role": "tool", "call_id": "c1", "content": "a"},
    ]
    await collect(h, handle, msgs, [ECHO])
    prompt = clients[0].prompt or ""
    assert prompt.startswith("[对话历史]\nuser: 第一问\nassistant: 第一答")
    assert "[当前用户消息]\n第二问" in prompt
    assert "[本轮已发生的工具调用与结果]" in prompt and "tool[c1]: a" in prompt


def test_build_prompt_single_message_is_verbatim() -> None:
    assert build_prompt([{"role": "user", "content": "你好"}]) == "你好"


def test_config_from_env_defaults_and_overrides() -> None:
    d = ClaudeSdkConfig.from_env({})
    expected = ("http://localhost:4000", "sk-dev-master", None, 16, 300.0)
    assert (d.base_url, d.auth_token, d.cli_path, d.max_turns, d.turn_timeout_s) == expected
    g = ClaudeSdkConfig.from_env({"MODEL_GATEWAY_URL": "http://litellm:4000/v1", "MODEL_GATEWAY_API_KEY": "k1"})
    assert (g.base_url, g.auth_token) == ("http://litellm:4000", "k1")
    o = ClaudeSdkConfig.from_env(
        {
            "CLAUDE_SDK_BASE_URL": "http://x:1/",
            "MODEL_GATEWAY_URL": "http://ignored",
            "CLAUDE_SDK_AUTH_TOKEN": "k2",
            "CLAUDE_SDK_CLI_PATH": "/bin/claude",
            "CLAUDE_SDK_MAX_TURNS": "3",
            "CLAUDE_SDK_TURN_TIMEOUT_S": "9.5",
        }
    )
    assert (o.base_url, o.auth_token, o.cli_path, o.max_turns, o.turn_timeout_s) == ("http://x:1", "k2", "/bin/claude", 3, 9.5)


async def test_thinking_event_default_visibility_is_internal(tmp_path: Any) -> None:
    async def script(c: FakeClient) -> Any:
        yield assistant(ThinkingBlock(thinking="t", signature="s"), mid="m1")
        yield result(1, 1)

    h, _ = make_harness(script)
    handle = await h.open_session(make_spec(tmp_path))
    events = await collect(h, handle, [{"role": "user", "content": "hi"}])
    assert isinstance(events[0], ThinkingEvent) and events[0].visibility == "internal"


# ---- 生命周期：生成器被提前关闭 ----


async def test_aclose_before_done_tears_down_session(tmp_path: Any) -> None:
    """消费到 ToolUse 后直接 aclose 生成器（未取 Usage/Done）：会话被拆除、泵结束、client 断开；
    随后 runner 的 interrupt 幂等无异常。"""

    async def script(c: FakeClient) -> Any:
        yield assistant(ToolUseBlock(id="toolu_1", name="mcp__platform__echo", input={"text": "x"}), mid="m1")
        await c.call_tool("echo", {"text": "x"})
        yield result(1, 1)  # pragma: no cover

    h, clients = make_harness(script)
    handle = await h.open_session(make_spec(tmp_path))
    session = h._sessions[handle.session_id]
    gen: Any = h.run_turn(handle, TurnInput(messages=[{"role": "user", "content": "go"}]), [ECHO], [], HookSet())
    async for ev in gen:
        if isinstance(ev, ToolUseEvent):
            break
    futures = list(session.pending.values())
    await gen.aclose()

    assert session.task is not None and session.task.done()
    assert handle.session_id not in h._sessions
    assert clients[0].disconnected
    assert futures and all(f.done() for f in futures)
    assert not exists(session.config_dir)
    await h.interrupt(handle, "run_test")  # 已拆除：幂等
    await h.interrupt(handle, "run_test")


async def test_interrupt_unknown_or_finished_session_is_noop(tmp_path: Any) -> None:
    async def script(c: FakeClient) -> Any:
        yield result(1, 1)

    h, _ = make_harness(script)
    handle = await h.open_session(make_spec(tmp_path))
    await collect(h, handle, [{"role": "user", "content": "hi"}])
    await h.interrupt(handle, "run_test")  # 查询已正常结束
    await h.interrupt(handle.model_copy(update={"session_id": "nope"}), "run_test")  # 从未存在
    assert h._sessions == {}


# ---- 运行期隔离断言 ----


def init_message(tools: list[str], servers: list[str]) -> SystemMessage:
    data = {
        "type": "system",
        "subtype": "init",
        "tools": tools,
        "mcp_servers": [{"name": n, "status": "connected"} for n in servers],
    }
    return SystemMessage(subtype="init", data=data)


@pytest.mark.parametrize(
    ("tools", "servers", "needle"),
    [
        (["mcp__platform__echo", "Bash"], ["platform"], "Bash"),
        (["mcp__platform__echo", "mcp__github__create_issue"], ["platform", "github"], "github"),
        ([], ["github"], "github"),
    ],
)
async def test_init_isolation_violation_fails_closed(tmp_path: Any, tools: list[str], servers: list[str], needle: str) -> None:
    """init 报告了平台之外的工具或 MCP server：失败关闭，不再转发后续任何模型输出。"""

    async def script(c: FakeClient) -> Any:
        yield init_message(tools, servers)
        yield assistant(TextBlock(text="不应出现"), mid="m1")  # pragma: no cover
        yield result(1, 1)  # pragma: no cover

    h, clients = make_harness(script)
    handle = await h.open_session(make_spec(tmp_path))
    events = await collect(h, handle, [{"role": "user", "content": "hi"}], [ECHO])
    assert kinds(events) == ["error", "usage", "done:error"]
    assert events[0].code == "HARNESS_ISOLATION_VIOLATION" and needle in events[0].message
    assert clients[0].disconnected and h._sessions == {}


async def test_init_with_only_platform_tools_passes(tmp_path: Any) -> None:
    async def script(c: FakeClient) -> Any:
        yield init_message(["mcp__platform__echo"], ["platform"])
        yield assistant(TextBlock(text="好"), mid="m1")
        yield result(1, 1)

    h, _ = make_harness(script)
    handle = await h.open_session(make_spec(tmp_path))
    events = await collect(h, handle, [{"role": "user", "content": "hi"}], [ECHO])
    assert kinds(events) == ["text", "usage", "done:end_turn"]


# ---- 会话目录与转录落盘 ----


async def test_config_dir_is_per_session_temp_and_removed(tmp_path: Any) -> None:
    """CLAUDE_CONFIG_DIR 是会话独立的临时目录（不在工作区内），会话结束后删除；调用方给定的工作区保留。"""

    async def script(c: FakeClient) -> Any:
        assert is_dir(c.options.env["CLAUDE_CONFIG_DIR"])  # 查询期间存在
        yield result(1, 1)

    h, clients = make_harness(script)
    handle = await h.open_session(make_spec(tmp_path))
    await collect(h, handle, [{"role": "user", "content": "hi"}])
    opts = clients[0].options
    config_dir = Path(opts.env["CLAUDE_CONFIG_DIR"])
    assert config_dir.name.startswith("claude-sdk-home-")
    assert tmp_path / "ws" not in config_dir.parents
    assert not exists(config_dir)
    assert (tmp_path / "ws").is_dir()  # 用户给定的 workspace_dir 不删
    # CLI 转录与输入历史不写盘
    assert opts.extra_args == {"no-session-persistence": None}
    assert opts.env["CLAUDE_CODE_SKIP_PROMPT_HISTORY"] == "1"


async def test_self_created_cwd_removed_on_end_and_on_interrupt(tmp_path: Any) -> None:
    async def script(c: FakeClient) -> Any:
        yield result(1, 1)

    h, clients = make_harness(script)
    handle = await h.open_session(make_spec(tmp_path, workspace=False))
    await collect(h, handle, [{"role": "user", "content": "hi"}])
    assert not exists(clients[0].options.cwd)

    # 从未启动查询的会话，由 interrupt 负责删除
    idle = await h.open_session(make_spec(tmp_path, workspace=False))
    s = h._sessions[idle.session_id]
    assert is_dir(s.cwd) and is_dir(s.config_dir)
    await h.interrupt(idle, "run_test")
    assert not exists(s.cwd) and not exists(s.config_dir)


# ---- 子进程环境最小化 ----

SENSITIVE = [
    "DATABASE_URL",
    "RUNTIME_TEST_DATABASE_URL",
    "MODEL_GATEWAY_API_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_OTEL_AUTH",
    "GATEWAY_CLIENT_SECRET",
    "LITELLM_MASTER_KEY",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "COHERE_API_KEY",
    "AWS_SECRET_ACCESS_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "AZURE_OPENAI_API_KEY",
]


async def test_env_scrubs_sensitive_variables(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    for i, k in enumerate(SENSITIVE):
        monkeypatch.setenv(k, f"secret-{i}")

    async def script(c: FakeClient) -> Any:
        yield result(1, 1)

    h, clients = make_harness(script)
    handle = await h.open_session(make_spec(tmp_path))
    await collect(h, handle, [{"role": "user", "content": "hi"}])
    env = clients[0].options.env
    assert {k: env.get(k) for k in SENSITIVE} == dict.fromkeys(SENSITIVE, "")
    assert not any(v.startswith("secret-") for v in env.values())
    assert env["ANTHROPIC_AUTH_TOKEN"] == GW_TOKEN  # 网关凭据只以这一处显式传入
    assert os.environ["DATABASE_URL"] == "secret-0"  # 不修改本进程环境


# ---- 停止原因与 pre_model ----


async def test_result_max_tokens_maps_to_done_max_tokens(tmp_path: Any) -> None:
    async def script(c: FakeClient) -> Any:
        yield assistant(TextBlock(text="截断的"), mid="m1")
        yield result(1, 8, stop_reason="max_tokens")

    h, _ = make_harness(script)
    handle = await h.open_session(make_spec(tmp_path))
    events = await collect(h, handle, [{"role": "user", "content": "hi"}])
    assert kinds(events) == ["text", "usage", "done:max_tokens"]


async def test_result_error_max_turns_maps_to_harness_max_turns(tmp_path: Any) -> None:
    async def script(c: FakeClient) -> Any:
        yield result(4, 4, is_error=True, subtype="error_max_turns")

    h, _ = make_harness(script)
    handle = await h.open_session(make_spec(tmp_path))
    events = await collect(h, handle, [{"role": "user", "content": "hi"}])
    assert kinds(events) == ["error", "usage", "done:error"]
    assert events[0].code == "HARNESS_MAX_TURNS" and events[1].input_tokens == 4


async def test_pre_model_deny_does_not_start_query(tmp_path: Any) -> None:
    seen: list[dict[str, Any]] = []

    async def deny(payload: dict[str, Any]) -> dict[str, Any] | None:
        seen.append(payload)
        return {"deny": "budget exceeded"}

    async def allow(payload: dict[str, Any]) -> dict[str, Any] | None:
        return None

    async def script(c: FakeClient) -> Any:
        yield assistant(TextBlock(text="好"), mid="m1")
        yield result(1, 1)

    h, clients = make_harness(script)
    handle = await h.open_session(make_spec(tmp_path))
    msgs = [{"role": "user", "content": "hi"}]
    events = [ev async for ev in h.run_turn(handle, TurnInput(messages=msgs), [], [], HookSet(pre_model=deny))]
    assert kinds(events) == ["error", "usage", "done:error"]
    assert events[0].code == "PRE_MODEL_DENIED" and events[0].message == "budget exceeded"
    assert clients == []  # 查询从未启动
    assert seen == [{"messages": msgs, "model_alias": MODEL}]

    # 会话保留：放行后同一会话可正常开启查询
    events = [ev async for ev in h.run_turn(handle, TurnInput(messages=msgs), [], [], HookSet(pre_model=allow))]
    assert kinds(events) == ["text", "usage", "done:end_turn"]


# ---- fallback call_id 之后迟到的块 ----


async def test_late_block_after_fallback_call_id_is_dropped(tmp_path: Any) -> None:
    """handler 等块超时自生成 call_id 后，该调用的块迟到：应被丢弃，不能被下一次同名同参调用认领。"""

    async def script(c: FakeClient) -> Any:
        res = await c.call_tool("echo", {"text": "x"})  # 块尚未出现 → 自生成 call_id
        yield assistant(ToolUseBlock(id="toolu_late", name="mcp__platform__echo", input={"text": "x"}), mid="m1")
        yield assistant(ToolUseBlock(id="toolu_2", name="mcp__platform__echo", input={"text": "x"}), mid="m2")
        res2 = await c.call_tool("echo", {"text": "x"})
        yield assistant(TextBlock(text=res["content"][0]["text"] + "|" + res2["content"][0]["text"]), mid="m3")
        yield result(1, 1)

    h, _ = make_harness(script)
    h.block_wait_s = 0.05
    handle = await h.open_session(make_spec(tmp_path))
    msgs: list[dict[str, Any]] = [{"role": "user", "content": "go"}]
    first = await collect(h, handle, msgs, [ECHO])
    fallback = first[0]
    assert isinstance(fallback, ToolUseEvent) and fallback.call_id.startswith("call_")

    msgs.append({"role": "tool", "call_id": fallback.call_id, "content": "r1"})
    second = await collect(h, handle, msgs, [ECHO])
    assert kinds(second) == ["tool_use", "usage", "done:tool_use"]
    assert second[0].call_id == "toolu_2"  # 不是迟到的 toolu_late

    msgs.append({"role": "tool", "call_id": "toolu_2", "content": "r2"})
    third = await collect(h, handle, msgs, [ECHO])
    assert kinds(third) == ["text", "usage", "done:end_turn"] and third[0].delta == "r1|r2"

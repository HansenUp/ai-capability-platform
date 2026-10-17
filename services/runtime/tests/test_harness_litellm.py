"""LiteLLM harness 测试：以 httpx.MockTransport 模拟 LiteLLM 网关，验证统一事件顺序
（Thinking → Text → ToolUse → Usage → Done）、每轮必有 UsageEvent、end_turn / max_tokens / 错误 / 拒绝 / 中断路径、
非预期异常收口（HARNESS_ERROR）、截断的工具调用不执行（TOOL_CALL_TRUNCATED）、`/model/info` 负缓存、
interrupt 后会话回收、推理文本经历史 tool_use 消息的 reasoning 字段回传，以及经 Messages API 的端到端对话。

另含默认跳过的真实联调测试：设置 RUNTIME_LIVE_LLM=1 时对本机网关（http://localhost:4000）跑真实模型。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from ai_contract import HookSet, SessionSpec, ToolSpec, TurnInput, capability_tier
from runtime.assemble.assembler import LOCAL_TOOLS
from runtime.harness import registry
from runtime.harness.litellm import LiteLLMHarness
from runtime.llm import LLMClient, set_transport_for_tests

ALIAS = "deepseek-v4-pro-2026-08"
MODEL_INFO = {"data": [{"model_name": ALIAS, "litellm_params": {"model": "deepseek/deepseek-v4-pro"}, "model_info": {}}]}


def sse(*payloads: dict[str, Any]) -> bytes:
    """把若干 chunk 负载编码为以 [DONE] 结尾的 SSE 响应体。"""
    return ("".join(f"data: {json.dumps(p, ensure_ascii=False)}\n\n" for p in payloads) + "data: [DONE]\n\n").encode()


def delta(finish: str | None = None, **d: Any) -> dict[str, Any]:
    """构造一个 chat.completion.chunk 负载（model 字段回报别名，与 LiteLLM 行为一致）。"""
    choice: dict[str, Any] = {"index": 0, "delta": d}
    if finish:
        choice["finish_reason"] = finish
    return {"object": "chat.completion.chunk", "model": ALIAS, "choices": [choice]}


def usage(prompt: int = 50, completion: int = 30) -> dict[str, Any]:
    """构造携带 usage 的最后一个 chunk。"""
    return {
        "object": "chat.completion.chunk",
        "model": ALIAS,
        "choices": [],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }


def tool_call_turn(name: str, args: str, call_id: str = "call_1", reasoning: str = "需要调用工具") -> bytes:
    """一轮"推理 → 文本 → 工具调用"的模型响应。"""
    return sse(
        delta(reasoning_content=reasoning),
        delta(content="我来处理。"),
        delta(tool_calls=[{"index": 0, "id": call_id, "type": "function", "function": {"name": name, "arguments": args}}]),
        delta(finish="tool_calls"),
        usage(),
    )


def text_turn(text: str, finish: str = "stop") -> bytes:
    """一轮纯文本的模型响应。"""
    return sse(delta(content=text), delta(finish=finish), usage(80, 12))


class Gateway:
    """模拟 LiteLLM 网关：/model/info 返回固定清单（可替换为指定响应并计数），/v1/chat/completions 依次返回
    预置响应并记录请求体。"""

    def __init__(self, *responses: httpx.Response | bytes, model_info: httpx.Response | None = None) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.model_info = model_info
        self.model_info_calls = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/model/info":
            self.model_info_calls += 1
            return self.model_info or httpx.Response(200, json=MODEL_INFO)
        self.requests.append(json.loads(req.content))
        nxt = self.responses.pop(0)
        return nxt if isinstance(nxt, httpx.Response) else httpx.Response(200, headers={"content-type": "text/event-stream"}, content=nxt)


def make_harness(handler: Callable[[httpx.Request], httpx.Response]) -> LiteLLMHarness:
    """构造注入了模拟网关的 harness（不重试、无退避）。"""
    return LiteLLMHarness(
        client=LLMClient(base_url="http://gw.test", api_key="k", max_retries=0, backoff_s=0, transport=httpx.MockTransport(handler))
    )


def spec(system_prompt: str = "你是工单助手。") -> SessionSpec:
    """构造一个最小的会话规格。"""
    return SessionSpec(run_id="run_t", agent_id="a", agent_version="1", model_alias=ALIAS, system_prompt=system_prompt, subject="human:t")


async def run_turn(
    h: LiteLLMHarness, messages: list[dict[str, Any]], tools: list[ToolSpec] | None = None, hooks: HookSet | None = None
) -> list[Any]:
    """开会话并跑一轮，返回事件列表。"""
    session = await h.open_session(spec())
    return [ev async for ev in h.run_turn(session, TurnInput(messages=messages), tools or [], [], hooks or HookSet())]


async def test_colliding_tool_names_are_rejected_before_calling_model():
    """ticket.search 与 ticket__search 编码成同一个 OpenAI 函数名：模型的调用无法还原，本轮以 HARNESS_ERROR 结束、不调用模型。"""
    gw = Gateway(text_turn("不应出现"))
    dup = LOCAL_TOOLS["ticket.search"].model_copy(update={"name": "ticket__search"})
    events = await run_turn(make_harness(gw), [{"role": "user", "content": "查"}], [LOCAL_TOOLS["ticket.search"], dup])
    assert [e.type.value for e in events] == ["error", "usage", "done"] and events[-1].stop_reason == "error"
    assert events[0].code == "HARNESS_ERROR" and "collide" in events[0].message
    assert gw.requests == []


def test_registry_loads_litellm_harness_as_platform_loop():
    """registry 可按名称加载 litellm harness；平台持有循环，能力等级为 L2，模型无关。"""
    h = registry.get("litellm")
    caps = h.capabilities()
    assert isinstance(h, LiteLLMHarness) and caps.name == "litellm" and caps.loop_owner == "platform"
    assert caps.models == [] and capability_tier(caps) == "L2"


async def test_event_order_with_tool_use():
    """推理 → 文本 → 工具调用 → 用量 → Done(tool_use)；工具名映射回平台名，UsageEvent.model 为解析后的真实模型。"""
    gw = Gateway(tool_call_turn("ticket__search", '{"q": "打印机"}'))
    events = await run_turn(make_harness(gw), [{"role": "user", "content": "查工单"}], [LOCAL_TOOLS["ticket.search"]])

    assert [e.type for e in events] == ["thinking", "text", "tool_use", "usage", "done"]
    tool_use, usage_ev, done = events[2], events[3], events[4]
    assert (tool_use.call_id, tool_use.tool_name, tool_use.input) == ("call_1", "ticket.search", {"q": "打印机"})
    assert (usage_ev.input_tokens, usage_ev.output_tokens, usage_ev.model) == (50, 30, "deepseek/deepseek-v4-pro")
    assert done.stop_reason == "tool_use" and done.run_id == "run_t"

    body = gw.requests[0]
    assert body["model"] == ALIAS and body["parallel_tool_calls"] is False
    assert body["messages"][0] == {"role": "system", "content": "你是工单助手。"}
    assert body["tools"][0]["function"]["name"] == "ticket__search"


async def test_end_turn_and_max_tokens_paths_always_emit_usage():
    """纯文本：Done(end_turn)；finish_reason=length：Done(max_tokens)；两者都带 UsageEvent。无工具时不带 parallel_tool_calls。"""
    gw = Gateway(text_turn("你好"), text_turn("被截", finish="length"))
    h = make_harness(gw)
    first = await run_turn(h, [{"role": "user", "content": "hi"}])
    assert [e.type for e in first] == ["text", "usage", "done"] and first[-1].stop_reason == "end_turn"
    assert "tools" not in gw.requests[0] and "parallel_tool_calls" not in gw.requests[0]
    second = await run_turn(h, [{"role": "user", "content": "hi"}])
    assert [e.type for e in second] == ["text", "usage", "done"] and second[-1].stop_reason == "max_tokens"


async def test_llm_error_maps_to_error_usage_done():
    """网关 4xx：ErrorEvent(LLM_ERROR, 不可重试) → UsageEvent → Done(error)。"""
    gw = Gateway(httpx.Response(400, text="bad request"))
    events = await run_turn(make_harness(gw), [{"role": "user", "content": "hi"}])
    assert [e.type for e in events] == ["error", "usage", "done"]
    assert events[0].code == "LLM_ERROR" and events[0].retryable is False and "HTTP 400" in events[0].message
    assert events[-1].stop_reason == "error"


async def test_pre_model_hook_deny_blocks_model_call():
    """pre_model 钩子返回 {"deny": reason}：不调用模型，产出 ErrorEvent + UsageEvent + Done(error)。"""
    gw = Gateway()

    async def deny(payload: dict[str, Any]) -> dict[str, Any] | None:
        return {"deny": "预算耗尽"}

    events = await run_turn(make_harness(gw), [{"role": "user", "content": "hi"}], hooks=HookSet(pre_model=deny))
    assert [e.type for e in events] == ["error", "usage", "done"] and events[0].message == "预算耗尽"
    assert gw.requests == []


async def test_interrupt_cancels_running_turn_and_releases_session():
    """一轮进行中 interrupt：流在下一个增量处停止，只再产出 UsageEvent 与 Done(cancelled)；会话随即被回收，
    此后该会话的 run_turn 返回 SESSION_NOT_FOUND，不残留任何中断标记。"""
    gw = Gateway(sse(delta(content="一"), delta(content="二"), delta(finish="stop"), usage()))
    h = make_harness(gw)
    session = await h.open_session(spec())
    events: list[Any] = []
    async for ev in h.run_turn(session, TurnInput(messages=[{"role": "user", "content": "hi"}]), [], [], HookSet()):
        events.append(ev)
        if ev.type == "text":
            await h.interrupt(session, "run_t")
    assert [e.type for e in events] == ["text", "usage", "done"] and events[-1].stop_reason == "cancelled"
    assert h._specs == {} and h._active == set() and h._cancelled == set()

    again = [ev async for ev in h.run_turn(session, TurnInput(messages=[{"role": "user", "content": "hi"}]), [], [], HookSet())]
    assert [e.type for e in again] == ["error", "usage", "done"] and again[0].code == "SESSION_NOT_FOUND"


async def test_interrupt_right_after_stream_opened_does_not_hang(monkeypatch: pytest.MonkeyPatch):
    """开流后立即 interrupt：读取上游的任务在首次调度前就被取消（协程体与其 finally 都不执行），
    消费端也不会永久挂起——以 Usage + Done(cancelled) 结束，且不留下任何会话状态。"""
    h = make_harness(Gateway(text_turn("你好")))
    session = await h.open_session(spec())
    assert h._client is not None
    original = h._client.stream_chat

    def interrupt_now() -> None:
        # 在读取任务首次调度之前同步执行一次真正的 interrupt（它没有 await 点）
        coro = h.interrupt(session, "run_t")
        with contextlib.suppress(StopIteration):
            coro.send(None)

    def stream_chat(*args: Any, **kwargs: Any) -> Any:
        asyncio.get_running_loop().call_soon(interrupt_now)  # 排在随后 create_task 的首次调度之前
        return original(*args, **kwargs)

    monkeypatch.setattr(h._client, "stream_chat", stream_chat)

    async def consume() -> list[Any]:
        return [ev async for ev in h.run_turn(session, TurnInput(messages=[{"role": "user", "content": "hi"}]), [], [], HookSet())]

    events = await asyncio.wait_for(consume(), timeout=5)
    assert [e.type for e in events] == ["usage", "done"] and events[-1].stop_reason == "cancelled"
    assert h._specs == {} and h._active == set() and h._cancelled == set() and h._pumps == {}


async def test_interrupt_idle_session_leaves_no_state():
    """drive 结束时对空闲会话 interrupt（runner 的常规回收路径）：规格被删除，且不留下会误伤后续的中断标记。"""
    h = make_harness(Gateway(text_turn("你好")))
    idle = await h.open_session(spec())
    await h.interrupt(idle, "run_t")
    assert h._specs == {} and h._cancelled == set()
    events = await run_turn(h, [{"role": "user", "content": "hi"}])  # 新会话不受影响
    assert [e.type for e in events] == ["text", "usage", "done"] and events[-1].stop_reason == "end_turn"


async def test_reasoning_comes_from_tool_use_message_in_history():
    """推理文本只来自历史：tool_use 消息带 reasoning → 请求中对应 assistant 消息带 reasoning_content；
    不带则无该字段（即便同一 harness 之前跑过产生推理的轮次，也不会串用进程内状态）。"""
    gw = Gateway(tool_call_turn("echo", '{"x": 1}', call_id="c42", reasoning="先回显"), text_turn("完成"), text_turn("完成"))
    h = make_harness(gw)
    session = await h.open_session(spec())
    history: list[dict[str, Any]] = [{"role": "user", "content": "回显"}]
    tools = [LOCAL_TOOLS["echo"]]
    _ = [ev async for ev in h.run_turn(session, TurnInput(messages=history), tools, [], HookSet())]
    tail = [{"role": "tool", "call_id": "c42", "content": {"x": 1}}]
    tool_use = {"call_id": "c42", "name": "echo", "input": {"x": 1}}
    with_reasoning = [
        *history,
        {"role": "assistant", "content": "我来处理。"},
        {"role": "assistant", "tool_use": tool_use, "reasoning": "先回显"},
    ]
    second = [ev async for ev in h.run_turn(session, TurnInput(messages=with_reasoning + tail), tools, [], HookSet())]
    assert [e.type for e in second] == ["text", "usage", "done"]
    msgs = gw.requests[1]["messages"]
    assistant = next(m for m in msgs if m.get("tool_calls"))
    assert assistant["content"] == "我来处理。" and assistant["reasoning_content"] == "先回显"
    assert assistant["tool_calls"][0]["id"] == "c42"
    assert msgs[-1] == {"role": "tool", "tool_call_id": "c42", "content": '{"x": 1}'}

    without = [*history, {"role": "assistant", "tool_use": tool_use}]
    _ = [ev async for ev in h.run_turn(session, TurnInput(messages=without + tail), tools, [], HookSet())]
    assistant = next(m for m in gw.requests[2]["messages"] if m.get("tool_calls"))
    assert "reasoning_content" not in assistant


async def test_unexpected_errors_still_end_with_usage_and_done():
    """D5：传输层抛 httpx.DecodingError → ErrorEvent(LLM_ERROR, 不可重试)；handler 内抛 ValueError（非预期异常）
    → ErrorEvent(HARNESS_ERROR)；两者都以 UsageEvent + Done(error) 收尾。"""

    def decoding(req: httpx.Request) -> httpx.Response:
        raise httpx.DecodingError("bad gzip", request=req)

    def broken(req: httpx.Request) -> httpx.Response:
        raise ValueError("boom")

    first = await run_turn(make_harness(decoding), [{"role": "user", "content": "hi"}])
    assert [e.type for e in first] == ["error", "usage", "done"] and first[-1].stop_reason == "error"
    assert first[0].code == "LLM_ERROR" and first[0].retryable is False

    second = await run_turn(make_harness(broken), [{"role": "user", "content": "hi"}])
    assert [e.type for e in second] == ["error", "usage", "done"] and second[-1].stop_reason == "error"
    assert second[0].code == "HARNESS_ERROR" and second[0].retryable is False and "ValueError: boom" in second[0].message


async def test_truncated_tool_call_is_not_executed():
    """finish_reason=length 且工具参数因截断无法解析：不产出 ToolUse，改为 Error(TOOL_CALL_TRUNCATED) → Usage → Done(max_tokens)。"""
    truncated = sse(
        delta(content="我来查。"),
        delta(tool_calls=[{"index": 0, "id": "c1", "type": "function", "function": {"name": "ticket__search", "arguments": '{"q": "打'}}]),
        delta(finish="length"),
        usage(),
    )
    events = await run_turn(make_harness(Gateway(truncated)), [{"role": "user", "content": "查"}], [LOCAL_TOOLS["ticket.search"]])
    assert [e.type for e in events] == ["text", "error", "usage", "done"]
    assert events[1].code == "TOOL_CALL_TRUNCATED" and events[1].retryable is False and "ticket.search" in events[1].message
    assert events[2].input_tokens == 50 and events[-1].stop_reason == "max_tokens"


async def test_model_info_failure_is_requested_once_across_turns():
    """`/model/info` 返回 403：连续两轮对话只请求一次（负缓存、不重试），UsageEvent.model 退回为别名。"""
    gw = Gateway(text_turn("一"), text_turn("二"), model_info=httpx.Response(403, text="forbidden"))
    h = LiteLLMHarness(
        client=LLMClient(base_url="http://gw.test", api_key="k", max_retries=3, backoff_s=0, transport=httpx.MockTransport(gw))
    )
    for _ in range(2):
        events = await run_turn(h, [{"role": "user", "content": "hi"}])
        assert [e.type for e in events] == ["text", "usage", "done"] and events[1].model == ALIAS
    assert gw.model_info_calls == 1


def _events(resp: Any) -> list[dict[str, Any]]:
    """把 SSE 响应体解析为事件字典列表。"""
    return [json.loads(line[5:].strip()) for line in resp.iter_lines() if line.startswith("data:")]


def test_end_to_end_messages_api_with_litellm_harness(client):
    """端到端：harness="litellm" 的 Agent 经 /v1/messages 完成"工具调用 → 工具结果 → 文本回答"两轮，
    SSE 含 thinking（full 可见）/ tool_use / tool_result / text / usage，Run 终态 completed。"""
    gw = Gateway(tool_call_turn("echo", '{"msg": "hi"}', call_id="call_e2e"), text_turn("回显完成"))
    set_transport_for_tests(httpx.MockTransport(gw), max_retries=0, backoff_s=0)
    try:
        r = client.post(
            "/admin/agents",
            json={
                "agent_id": "llm-demo",
                "version": "1",
                "harness": "litellm",
                "model_alias": ALIAS,
                "system_prompt": "你是测试助手。",
                "config": {"tools": ["echo"], "thinking_visibility": "full"},
            },
        )
        assert r.status_code == 201, r.text
        assert client.post("/admin/agents/llm-demo/versions/1/publish", json={"eval_passed": True}).status_code == 200

        body = {"agent_id": "llm-demo", "messages": [{"role": "user", "content": "回显 hi"}]}
        with client.stream("POST", "/v1/messages", json=body) as resp:
            assert resp.status_code == 200
            run_id = resp.headers["x-run-id"]
            evs = _events(resp)
    finally:
        set_transport_for_tests(None)

    types = [e["type"] for e in evs]
    for t in ("thinking", "text", "tool_use", "tool_result", "usage"):
        assert t in types, types
    assert types.count("usage") == 2 and evs[-1]["type"] == "done" and evs[-1]["stop_reason"] == "end_turn"
    assert "".join(e["delta"] for e in evs if e["type"] == "text") == "我来处理。回显完成"
    assert next(e for e in evs if e["type"] == "usage")["model"] == "deepseek/deepseek-v4-pro"
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "completed"

    # 第二轮请求带上了平台历史：合并后的 assistant(文本 + tool_calls) 与对应的 tool 结果
    second = gw.requests[1]["messages"]
    assert second[0]["role"] == "system" and "你是测试助手。" in second[0]["content"]
    assistant = next(m for m in second if m.get("tool_calls"))
    assert assistant["tool_calls"][0]["function"]["name"] == "echo" and assistant["content"] == "我来处理。"
    assert second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "call_e2e"


# ---------------- 真实联调（默认跳过） ----------------

LIVE = pytest.mark.skipif(not os.environ.get("RUNTIME_LIVE_LLM"), reason="设置 RUNTIME_LIVE_LLM=1 才对本机 LiteLLM 网关做真实联调")


def _live_harness() -> LiteLLMHarness:
    """连接本机网关的 harness（地址可经 RUNTIME_LIVE_GATEWAY 覆盖）。"""
    return LiteLLMHarness(client=LLMClient(base_url=os.environ.get("RUNTIME_LIVE_GATEWAY", "http://localhost:4000")))


@LIVE
async def test_live_simple_chat():
    """真实模型（DeepSeek v4 pro，推理模型）跑一轮简单对话：有正文、UsageEvent.input_tokens > 0。"""
    events = await run_turn(_live_harness(), [{"role": "user", "content": "用一句话介绍你自己。"}])
    text = "".join(e.delta for e in events if e.type == "text")
    usage_ev = next(e for e in events if e.type == "usage")
    assert text.strip(), events
    assert usage_ev.input_tokens > 0 and usage_ev.output_tokens > 0 and usage_ev.model
    assert events[-1].type == "done" and events[-1].stop_reason == "end_turn"


@LIVE
async def test_live_tool_call_round_trip():
    """真实模型工具调用往返：第一轮请求 ticket.search，回填工具结果后第二轮给出文本。模拟 runner 行为：
    把第一轮 ThinkingEvent 拼接后放进 tool_use 消息的 reasoning 字段（DeepSeek 思考模式缺它会 400）。"""
    h = _live_harness()
    session = await h.open_session(spec())
    tools = [
        LOCAL_TOOLS["ticket.search"].model_copy(
            update={"input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}}
        )
    ]
    history: list[dict[str, Any]] = [{"role": "user", "content": "帮我查关键词为“打印机”的工单，必须调用工具。"}]
    first = [ev async for ev in h.run_turn(session, TurnInput(messages=history), tools, [], HookSet())]
    tool_use = next(e for e in first if e.type == "tool_use")
    assert tool_use.tool_name == "ticket.search" and first[-1].stop_reason == "tool_use"
    text = "".join(e.delta for e in first if e.type == "text")
    if text:
        history.append({"role": "assistant", "content": text})
    reasoning = "".join(e.delta for e in first if e.type == "thinking")
    call = {"role": "assistant", "tool_use": {"call_id": tool_use.call_id, "name": tool_use.tool_name, "input": tool_use.input}}
    if reasoning:
        call["reasoning"] = reasoning
    history += [
        call,
        {"role": "tool", "call_id": tool_use.call_id, "content": [{"id": "T-1", "title": "打印机卡纸"}]},
    ]
    second = [ev async for ev in h.run_turn(session, TurnInput(messages=history), tools, [], HookSet())]
    assert "".join(e.delta for e in second if e.type == "text").strip(), second
    assert second[-1].stop_reason == "end_turn"

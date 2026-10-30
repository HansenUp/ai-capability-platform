"""runtime.llm.client 单元测试（不依赖网络，经 httpx.MockTransport 模拟 LiteLLM 网关）：
覆盖 SSE 解析（推理 / 文本 / 分片工具调用 / usage / 兜底 call_id 唯一）、重试策略（429 重试、流中途失败不重试、
4xx 不重试、5xx 重试耗尽、网络错误与超时、非网络层 httpx 异常收口为不可重试）、`/model/info` 别名解析与
正 / 负缓存（不重试）、非流式调用，以及历史 / 工具的 OpenAI 翻译（含 tool_use 消息自带推理文本的回传）。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from ai_contract import ToolSpec
from runtime.llm import (
    LLMChunk,
    LLMClient,
    LLMError,
    UnknownModelAlias,
    to_openai_messages,
    to_openai_tool_name,
    to_openai_tools,
)


def sse(*payloads: dict[str, Any], done: bool = True) -> bytes:
    """把若干 chunk 负载编码为 SSE 响应体（可选追加 [DONE]）。"""
    body = "".join(f"data: {json.dumps(p, ensure_ascii=False)}\n\n" for p in payloads)
    return (body + ("data: [DONE]\n\n" if done else "")).encode()


def delta(model: str = "deepseek-v4-pro-2026-08", finish: str | None = None, **d: Any) -> dict[str, Any]:
    """构造一个 chat.completion.chunk 负载。"""
    choice: dict[str, Any] = {"index": 0, "delta": d}
    if finish:
        choice["finish_reason"] = finish
    return {"id": "x", "object": "chat.completion.chunk", "model": model, "choices": [choice]}


def usage_chunk(**usage: Any) -> dict[str, Any]:
    """构造携带 usage 的最后一个 chunk。"""
    return {"id": "x", "object": "chat.completion.chunk", "model": "deepseek-v4-pro-2026-08", "choices": [], "usage": usage}


def make_client(handler: Callable[[httpx.Request], httpx.Response], **kw: Any) -> LLMClient:
    """构造一个注入 MockTransport、无退避等待的客户端。"""
    kw.setdefault("max_retries", 2)
    return LLMClient(base_url="http://gw.test", api_key="sk-test", backoff_s=0, transport=httpx.MockTransport(handler), **kw)


async def collect(agen: AsyncIterator[LLMChunk]) -> list[LLMChunk]:
    """收集异步迭代器的全部 chunk。"""
    return [c async for c in agen]


class _BrokenStream(httpx.AsyncByteStream):
    """先吐出一段合法 SSE，再抛出网络读错误，用于模拟"流中途断开"。"""

    def __init__(self, first: bytes) -> None:
        self.first = first

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.first
        raise httpx.ReadError("connection reset mid-stream")


# ---------------- 流式解析 ----------------


async def test_stream_parses_reasoning_text_tool_calls_and_usage():
    """推理增量 → thinking，正文 → text，分片工具调用按 index 拼接，最后 usage 与 done；请求带鉴权与 include_usage。"""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse(
                delta(role="assistant", reasoning_content=""),
                delta(reasoning_content="先查工单"),
                delta(content="好的，"),
                delta(
                    tool_calls=[{"index": 0, "id": "call_a", "type": "function", "function": {"name": "ticket__search", "arguments": ""}}]
                ),
                delta(tool_calls=[{"index": 0, "function": {"arguments": '{"q": '}}]),
                delta(tool_calls=[{"index": 1, "id": "call_b", "function": {"name": "echo", "arguments": "{}"}}]),
                delta(tool_calls=[{"index": 0, "function": {"arguments": '"打印机"}'}}]),
                delta(finish="tool_calls"),
                usage_chunk(
                    prompt_tokens=87,
                    completion_tokens=85,
                    completion_tokens_details={"reasoning_tokens": 78},
                    prompt_tokens_details={"cached_tokens": 64},
                    cost=0.0002,
                ),
            ),
        )

    client = make_client(handler)
    chunks = await collect(client.stream_chat("deepseek-v4-pro-2026-08", [{"role": "user", "content": "hi"}], max_tokens=2048))

    assert [c.kind for c in chunks] == ["thinking", "text", "tool_call", "tool_call", "usage", "done"]
    assert chunks[0].delta == "先查工单" and chunks[1].delta == "好的，"
    first, second = chunks[2].tool_call, chunks[3].tool_call
    assert first is not None and (first.id, first.name, first.arguments) == ("call_a", "ticket__search", {"q": "打印机"})
    assert second is not None and (second.id, second.name, second.arguments) == ("call_b", "echo", {})
    usage = chunks[4].usage
    assert usage is not None
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.reasoning_tokens) == (87, 85, 64, 78)
    assert usage.cost == pytest.approx(0.0002)
    assert chunks[5].finish_reason == "tool_calls" and chunks[5].model == "deepseek-v4-pro-2026-08"

    req = seen[0]
    assert req.url.path == "/v1/chat/completions" and req.headers["authorization"] == "Bearer sk-test"
    body = json.loads(req.content)
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True} and body["max_tokens"] == 2048


async def test_stream_estimates_reasoning_tokens_and_keeps_bad_arguments_raw():
    """兼容 delta.reasoning；无 reasoning_tokens 时按推理文本长度 //3 估算；非法 JSON 参数给 {"_raw": ...} 不抛。"""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse(
                delta(reasoning="想" * 30),
                delta(tool_calls=[{"index": 0, "id": "c1", "function": {"name": "echo", "arguments": "not json"}}]),
                delta(finish="tool_calls"),
                usage_chunk(prompt_tokens=10, completion_tokens=20),
            ),
        )

    chunks = await collect(make_client(handler).stream_chat("m", []))
    tool = next(c.tool_call for c in chunks if c.kind == "tool_call")
    assert tool is not None and tool.arguments == {"_raw": "not json"}
    usage = next(c.usage for c in chunks if c.kind == "usage")
    assert usage is not None and usage.reasoning_tokens == 10 and usage.output_tokens == 20


async def test_stream_without_usage_still_yields_usage_and_done():
    """网关未下发 usage（且没有 [DONE]）时，仍以全零 usage 与 done 收尾。"""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse(delta(content="hi"), delta(finish="stop"), done=False))

    chunks = await collect(make_client(handler).stream_chat("m", []))
    assert [c.kind for c in chunks] == ["text", "usage", "done"]
    assert chunks[1].usage is not None and chunks[1].usage.input_tokens == 0 and chunks[2].finish_reason == "stop"


async def test_missing_tool_call_ids_get_unique_fallbacks():
    """上游不给工具调用 id 时，流式与非流式的兜底 id 都全局唯一（两次调用不会都得到 call_0）。"""

    def stream_handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse(delta(tool_calls=[{"index": 0, "function": {"name": "echo", "arguments": "{}"}}]), delta(finish="tool_calls")),
        )

    def chat_handler(req: httpx.Request) -> httpx.Response:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"type": "function", "function": {"name": "echo", "arguments": "{}"}}],
        }
        return httpx.Response(200, json={"model": "m", "choices": [{"finish_reason": "tool_calls", "message": message}]})

    stream_client, chat_client = make_client(stream_handler), make_client(chat_handler)
    ids: list[str] = []
    for _ in range(2):
        call = next(c.tool_call for c in await collect(stream_client.stream_chat("m", [])) if c.kind == "tool_call")
        assert call is not None
        ids.append(call.id)
        ids.append((await chat_client.chat("m", [])).tool_calls[0].id)
    assert len(set(ids)) == 4 and all(i.startswith("call_") and len(i) > len("call_0") for i in ids)


# ---------------- 重试策略 ----------------


async def test_stream_retries_after_429_then_succeeds():
    """429 属于可重试错误：尚未产出增量时重试，第二次成功。"""
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, content=sse(delta(content="ok"), delta(finish="stop")))

    chunks = await collect(make_client(handler).stream_chat("m", []))
    assert len(calls) == 2 and [c.delta for c in chunks if c.kind == "text"] == ["ok"]


async def test_stream_mid_stream_failure_is_not_retried():
    """已产出增量后网络断开：不重试（避免内容重复），抛出 retryable=True 的 LLMError。"""
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, stream=_BrokenStream(sse(delta(content="部分"), done=False)))

    got: list[LLMChunk] = []
    with pytest.raises(LLMError) as ei:
        async for c in make_client(handler).stream_chat("m", []):
            got.append(c)
    assert len(calls) == 1 and [c.delta for c in got] == ["部分"]
    assert ei.value.retryable is True and ei.value.code == "LLM_ERROR"


async def test_stream_error_frame_raises_non_retryable():
    """网关在流中下发 error 帧：抛出不可重试的 LLMError，且不重试。"""
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, content=sse({"error": {"message": "upstream exploded", "code": "500"}}))

    with pytest.raises(LLMError) as ei:
        await collect(make_client(handler).stream_chat("m", []))
    assert len(calls) == 1 and ei.value.retryable is False and "upstream exploded" in str(ei.value)


async def test_4xx_is_not_retried_and_body_truncated():
    """400 等非 429 的 4xx 直接失败：不重试，错误包含状态码与响应体前 500 字。"""
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, text="E" * 2000)

    with pytest.raises(LLMError) as ei:
        await collect(make_client(handler).stream_chat("m", []))
    err = ei.value
    assert len(calls) == 1 and err.retryable is False and err.status_code == 400
    assert "HTTP 400" in str(err) and str(err).count("E") <= 500 + 2  # "HTTP 400: " 之外至多 500 个 E


async def test_5xx_retries_until_exhausted():
    """5xx 可重试：重试 max_retries 次后仍失败，抛出 retryable=True 的 LLMError。"""
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, text="unavailable")

    with pytest.raises(LLMError) as ei:
        await collect(make_client(handler, max_retries=3).stream_chat("m", []))
    assert len(calls) == 4 and ei.value.retryable is True and ei.value.status_code == 503


async def test_connect_error_retried_and_timeout_mapped():
    """连接错误重试后成功；持续超时最终抛出 code=LLM_TIMEOUT。"""
    calls: list[int] = []

    def flaky(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("refused", request=req)
        return httpx.Response(200, content=sse(delta(content="ok")))

    assert [c.kind for c in await collect(make_client(flaky).stream_chat("m", []))] == ["text", "usage", "done"]
    assert len(calls) == 2

    def slow(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(LLMError) as ei:
        await collect(make_client(slow, max_retries=1).stream_chat("m", []))
    assert ei.value.code == "LLM_TIMEOUT" and ei.value.retryable is True


async def test_non_transport_httpx_error_becomes_non_retryable_llm_error():
    """非网络层的 httpx 异常（如响应解码失败）同样收口为 LLMError：不可重试、不重试；流式与非流式一致。"""
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.DecodingError("bad gzip", request=req)

    client = make_client(handler)
    with pytest.raises(LLMError) as ei:
        await collect(client.stream_chat("m", []))
    assert len(calls) == 1 and ei.value.retryable is False and ei.value.code == "LLM_ERROR"
    assert isinstance(ei.value.__cause__, httpx.DecodingError)
    with pytest.raises(LLMError) as ei2:
        await client.chat("m", [])
    assert len(calls) == 2 and ei2.value.retryable is False


# ---------------- 别名解析 / 非流式 ----------------

MODEL_INFO = {
    "data": [
        {
            "model_name": "deepseek-v4-pro-2026-08",
            "litellm_params": {"model": "deepseek/deepseek-v4-pro"},
            "model_info": {"litellm_provider": "deepseek", "max_input_tokens": 1000000, "max_output_tokens": 393216},
        },
        {"model_name": "default-chat", "litellm_params": {"model": "anthropic/claude-sonnet-5"}, "model_info": {}},
    ]
}


async def test_resolve_alias_uses_model_info_and_caches():
    """别名解析调用 GET /model/info（Bearer），结果缓存；未知别名抛 UnknownModelAlias。"""
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, json=MODEL_INFO)

    client = make_client(handler)
    info = await client.resolve_alias("deepseek-v4-pro-2026-08")
    assert (info.model, info.provider, info.max_output_tokens) == ("deepseek/deepseek-v4-pro", "deepseek", 393216)
    assert (await client.resolve_alias("default-chat")).model == "anthropic/claude-sonnet-5"
    assert len(calls) == 1 and calls[0].method == "GET" and calls[0].url.path == "/model/info"
    assert calls[0].headers["authorization"] == "Bearer sk-test"
    with pytest.raises(UnknownModelAlias):
        await client.resolve_alias("no-such-alias")

    expired = make_client(handler, model_info_ttl_s=0)
    await expired.resolve_alias("default-chat")
    await expired.resolve_alias("default-chat")
    assert len(calls) == 3  # TTL 为 0 时每次都重新拉取


async def test_resolve_alias_failure_is_negatively_cached_without_retry():
    """`/model/info` 失败（403 / 503）：不重试；失败结果在负缓存期内直接复用、不再请求；负缓存过期后重新请求。"""
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(403, text="forbidden")

    client = make_client(handler, max_retries=3)
    for _ in range(3):
        with pytest.raises(LLMError) as ei:
            await client.resolve_alias("deepseek-v4-pro-2026-08")
        assert ei.value.status_code == 403
    assert len(calls) == 1

    def unavailable(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, text="unavailable")

    no_cache = make_client(unavailable, max_retries=3, model_info_negative_ttl_s=0)
    for _ in range(2):
        with pytest.raises(LLMError):
            await no_cache.resolve_alias("default-chat")
    assert len(calls) == 3  # 可重试的 503 也不重试；负缓存为 0 时每次各请求一次


async def test_chat_non_stream_parses_message():
    """非流式调用：不带 stream 字段，解析正文、推理、工具调用与用量。"""
    bodies: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return httpx.Response(
            200,
            json={
                "model": "deepseek-v4-pro-2026-08",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "查一下",
                            "reasoning_content": "需要调用工具",
                            "tool_calls": [{"id": "c9", "type": "function", "function": {"name": "echo", "arguments": '{"a": 1}'}}],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 7, "completion_tokens_details": {"reasoning_tokens": 3}},
            },
        )

    resp = await make_client(handler).chat("deepseek-v4-pro-2026-08", [{"role": "user", "content": "hi"}], temperature=0.1)
    assert "stream" not in bodies[0] and bodies[0]["temperature"] == 0.1
    assert (resp.text, resp.thinking, resp.finish_reason) == ("查一下", "需要调用工具", "tool_calls")
    assert resp.tool_calls[0].id == "c9" and resp.tool_calls[0].arguments == {"a": 1}
    assert (resp.usage.input_tokens, resp.usage.output_tokens, resp.usage.reasoning_tokens) == (5, 7, 3)


# ---------------- 历史 / 工具翻译 ----------------


def test_to_openai_messages_merges_text_and_tool_use():
    """assistant 文本 + 紧随的 tool_use 合并为一条（content=文本 + tool_calls）；tool 结果转字符串；
    工具结果之后的 tool_use 单独成条（content=None）；工具名做 OpenAI 兼容转换；tool_use 消息自带的 reasoning
    附为 reasoning_content，不带则不输出该字段。"""
    history = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "查工单"},
        {"role": "assistant", "content": "我来查"},
        {"role": "assistant", "tool_use": {"call_id": "c1", "name": "ticket.search", "input": {"q": "打印机"}}},
        {"role": "tool", "call_id": "c1", "content": [{"id": "T-1"}]},
        {"role": "assistant", "tool_use": {"call_id": "c2", "name": "echo", "input": {}}, "reasoning": "再回显一次"},
        {"role": "tool", "call_id": "c2", "content": "artifact://x"},
        {"role": "assistant", "content": "完成"},
    ]
    out = to_openai_messages(history)
    assert out[0] == {"role": "system", "content": "S"} and out[1] == {"role": "user", "content": "查工单"}
    assert out[2] == {
        "role": "assistant",
        "content": "我来查",
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "ticket__search", "arguments": '{"q": "打印机"}'}}],
    }
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": '[{"id": "T-1"}]'}
    assert out[4] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "echo", "arguments": "{}"}}],
        "reasoning_content": "再回显一次",
    }
    assert out[5] == {"role": "tool", "tool_call_id": "c2", "content": "artifact://x"}
    assert out[6] == {"role": "assistant", "content": "完成"}
    assert len(out) == 7
    assert history[2] == {"role": "assistant", "content": "我来查"}  # 入参未被修改


def test_to_openai_tools_and_tool_name():
    """ToolSpec → OpenAI function；空 input_schema 给空对象 schema；非法字符的工具名被转换。"""
    tools = [
        ToolSpec(name="ticket.search", description="查询工单"),
        ToolSpec(name="echo", description="回显", input_schema={"type": "object", "properties": {"x": {"type": "string"}}}),
    ]
    out = to_openai_tools(tools)
    assert out[0] == {
        "type": "function",
        "function": {"name": "ticket__search", "description": "查询工单", "parameters": {"type": "object", "properties": {}}},
    }
    assert out[1]["function"]["parameters"]["properties"] == {"x": {"type": "string"}}
    assert to_openai_tool_name("a b/c.d") == "a_b_c__d" and len(to_openai_tool_name("x" * 100)) == 64


def test_usage_diagnostic_provenance_preserves_legacy_values():
    from runtime.llm.client import _usage_from

    missing = _usage_from({}, "")
    zero = _usage_from({"prompt_tokens": 0, "completion_tokens": 0, "cost": 0}, "")
    assert missing.input_tokens == zero.input_tokens == 0
    assert not missing.input_reported and not missing.output_reported and not missing.cost_reported
    assert zero.input_reported and zero.output_reported and zero.cost_reported
    invalid = _usage_from({"prompt_tokens": True, "completion_tokens": -1, "cost": float("inf")}, "")
    assert not invalid.input_reported and not invalid.output_reported and not invalid.cost_reported

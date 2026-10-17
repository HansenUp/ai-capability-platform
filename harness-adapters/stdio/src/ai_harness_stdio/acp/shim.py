"""llm_shim：ACP agent 唯一的"模型"（OpenAI Chat Completions 兼容端点，D93 / D95 / D96）。

agent 的 OpenAI provider 指向本端点（127.0.0.1 临时端口，每会话随机 bearer；agent 拿不到平台模型网关的密钥）。
每次模型请求，shim 都**丢弃 agent 组装的 system 与 messages**，改用：

- system：平台的 ``SessionSpec.system_prompt``（D95）；
- messages：平台在最近一个 turn 帧里给出的历史（与 ``runtime.run.invariant.rebuild_messages`` 同格式），翻译成
  OpenAI 消息（结构化续跑 D96：新会话、续跑、重放都是同一机制，模型看到的上下文完全由平台事件日志决定）；
- tools：agent 请求里的工具必须全部是本会话注入的平台工具（经 profile 的模型侧前缀还原），出现任何其他工具即
  ``ISOLATION_VIOLATION``（失败关闭）；上游请求的工具声明按平台 ToolSpec 重建；
- model 固定为 ``SessionSpec.model_alias``；``parallel_tool_calls=false``、``stream_options.include_usage``；
  注入 run_id 等元数据（``metadata`` 与 ``x-ai-run-id``，有 ``TRACEPARENT`` 环境变量时透传 ``traceparent``）。

流式应答转给 agent，同时把思维 / 文本增量作为统一事件交给宿主；每次调用结束产出一条 UsageEvent。思维 / 文本
增量即时转发；tool_call 增量与 finish chunk（及其后的全部 chunk）先缓冲，流结束后按**本 turn** 的平台工具集合校验
模型请求的工具，通过才登记调用并转发给 agent——越界的工具调用 agent 根本看不到（失败关闭）。

两种后端：``UpstreamBackend``（经平台模型网关 / LiteLLM，持密钥）与 ``ScriptedBackend``（探针脚本模式：按
ModelScript 应答，并把每次调用模型"实际看到"的消息——由发往上游的 OpenAI 消息反向规范化——追加到 calls 文件）。
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from ai_contract import SessionSpec, TextEvent, ThinkingEvent, ToolSpec, UsageEvent

from .toolproxy import ToolNames, tool_result_text

PLACEHOLDER_MODEL = "platform-model"  # agent 配置里的模型名（shim 一律改写为平台别名）
_FORWARDED_PARAMS = ("max_tokens", "max_completion_tokens", "temperature", "top_p", "tool_choice")  # 允许透传的请求参数


class ShimError(Exception):
    """一次模型调用失败（上游错误、脚本耗尽等）。"""

    def __init__(self, code: str, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


# ---------------- 历史 ⇄ OpenAI 消息 ----------------


def to_openai_messages(history: list[dict[str, Any]], model_tool_name: Callable[[str], str]) -> list[dict[str, Any]]:
    """平台历史 → OpenAI 消息（语义同 runtime.llm.client.to_openai_messages；工具名经 model_tool_name 映射）。

    tool_use 与紧邻的前一条 assistant 文本 / tool_calls 合并为同一条 assistant 消息；``reasoning`` 回传为
    ``reasoning_content``；tool 结果内容统一转成文本。
    """
    out: list[dict[str, Any]] = []
    for m in history:
        role = m.get("role")
        if role == "assistant" and isinstance(m.get("tool_use"), dict):
            tu = m["tool_use"]
            call = {
                "id": str(tu.get("call_id")),
                "type": "function",
                "function": {
                    "name": model_tool_name(str(tu.get("name"))),
                    "arguments": json.dumps(tu.get("input") or {}, ensure_ascii=False),
                },
            }
            prev = out[-1] if out else None
            if prev is not None and prev.get("role") == "assistant" and (isinstance(prev.get("content"), str) or "tool_calls" in prev):
                prev.setdefault("tool_calls", []).append(call)
            else:
                prev = {"role": "assistant", "content": None, "tool_calls": [call]}
                out.append(prev)
            thought = m.get("reasoning")
            if isinstance(thought, str) and thought and "reasoning_content" not in prev:
                prev["reasoning_content"] = thought
        elif role == "tool":
            out.append({"role": "tool", "tool_call_id": str(m.get("call_id")), "content": tool_result_text(m.get("content"))})
        else:
            out.append(dict(m))
    return out


def from_openai_messages(messages: list[dict[str, Any]], platform_tool_name: Callable[[str], str]) -> list[dict[str, Any]]:
    """OpenAI 消息（不含 system 提示词）→ 平台历史格式，供探针核对"模型实际看到的消息"。"""
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            if isinstance(m.get("content"), str) and m["content"]:
                out.append({"role": "assistant", "content": m["content"]})
            for i, call in enumerate(m["tool_calls"]):
                fn = call.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {"_raw": fn.get("arguments")}
                item: dict[str, Any] = {
                    "role": "assistant",
                    "tool_use": {"call_id": call.get("id"), "name": platform_tool_name(str(fn.get("name"))), "input": args},
                }
                if i == 0 and isinstance(m.get("reasoning_content"), str) and m["reasoning_content"]:
                    item["reasoning"] = m["reasoning_content"]
                out.append(item)
        elif role == "tool":
            out.append({"role": "tool", "call_id": m.get("tool_call_id"), "content": m.get("content")})
        else:
            out.append(dict(m))
    return out


def openai_tools(specs: list[ToolSpec], model_tool_name: Callable[[str], str]) -> list[dict[str, Any]]:
    """平台 ToolSpec → OpenAI function 声明（名字为 agent 给模型看的名字）。"""
    return [
        {
            "type": "function",
            "function": {
                "name": model_tool_name(t.name),
                "description": t.description,
                "parameters": {"type": "object", "properties": {}, **(t.input_schema or {})},
            },
        }
        for t in specs
    ]


# ---------------- 流式解析 ----------------


@dataclass
class ModelToolCall:
    """模型在一次调用里请求的工具（平台名、模型侧 id、入参）。"""

    call_id: str
    name: str  # 平台工具名
    input: dict[str, Any]


@dataclass
class _ToolBuf:
    id: str = ""
    name: str = ""
    args: list[str] = field(default_factory=list)


class StreamState:
    """一次流式应答的解析状态。"""

    def __init__(self) -> None:
        self.model: str | None = None
        self.finish_reason: str | None = None
        self.usage: dict[str, Any] | None = None
        self.tools: dict[int, _ToolBuf] = {}

    def feed(self, chunk: dict[str, Any]) -> list[tuple[str, str]]:
        """解析一个 chunk，返回其中的 (kind, delta)：kind 为 thinking / text。"""
        if "error" in chunk:
            raise ShimError("UPSTREAM_ERROR", f"model stream error: {json.dumps(chunk['error'], ensure_ascii=False)[:500]}")
        if isinstance(chunk.get("model"), str):
            self.model = chunk["model"]
        if isinstance(chunk.get("usage"), dict):
            self.usage = chunk["usage"]
        out: list[tuple[str, str]] = []
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                out.append(("thinking", reasoning))
            content = delta.get("content")
            if isinstance(content, str) and content:
                out.append(("text", content))
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index")
                buf = self.tools.setdefault(idx if isinstance(idx, int) else 0, _ToolBuf())
                fn = tc.get("function") or {}
                if tc.get("id") and not buf.id:
                    buf.id = str(tc["id"])
                if fn.get("name") and not buf.name:
                    buf.name = str(fn["name"])
                if isinstance(fn.get("arguments"), str):
                    buf.args.append(fn["arguments"])
            if choice.get("finish_reason"):
                self.finish_reason = str(choice["finish_reason"])
        return out

    def tool_calls(self) -> list[tuple[str, str, dict[str, Any]]]:
        """聚合后的工具调用：(模型侧 id, 模型侧名字, 入参)。参数无法解析时入参为 {"_raw": 原文}。"""
        out = []
        for _, buf in sorted(self.tools.items()):
            raw = "".join(buf.args)
            try:
                args = json.loads(raw) if raw.strip() else {}
            except ValueError:
                args = {"_raw": raw}
            out.append((buf.id or f"call_{uuid.uuid4().hex[:16]}", buf.name, args if isinstance(args, dict) else {"_raw": raw}))
        return out

    def usage_event(self, fallback_model: str) -> UsageEvent:
        u = self.usage or {}
        details = u.get("prompt_tokens_details") or {}

        def num(v: Any) -> int:
            return int(v) if isinstance(v, int | float) else 0

        cost = u.get("cost")
        return UsageEvent(
            input_tokens=num(u.get("prompt_tokens")),
            output_tokens=num(u.get("completion_tokens")),
            cache_read_tokens=num(details.get("cached_tokens") if isinstance(details, dict) else 0),
            cost=float(cost) if isinstance(cost, int | float) else 0.0,
            model=self.model or fallback_model,
        )


# ---------------- 后端 ----------------


@dataclass
class ModelRequest:
    """发往模型的一次请求（已由 shim 重建）。"""

    model: str
    messages: list[dict[str, Any]]  # 含 system
    tools: list[dict[str, Any]]
    params: dict[str, Any]
    spec: SessionSpec
    platform_tool_name: Callable[[str], str]  # 模型侧名字 → 平台名（用于规范化记录）


class ModelBackend(Protocol):
    async def open(self, req: ModelRequest, cancelled: asyncio.Event) -> AsyncIterator[dict[str, Any]]:
        """建立一次模型调用，返回 chunk 迭代器；建立失败抛 ShimError。"""
        ...

    async def aclose(self) -> None: ...


class UpstreamBackend:
    """经平台模型网关（LiteLLM，OpenAI 兼容）的真实模型。密钥只在桥进程内。"""

    def __init__(self, base_url: str, api_key: str, *, timeout_s: float = 120.0) -> None:
        base = base_url.rstrip("/")
        self.url = base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
        self.api_key = api_key
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=10.0), trust_env=False)

    async def open(self, req: ModelRequest, cancelled: asyncio.Event) -> AsyncIterator[dict[str, Any]]:
        body: dict[str, Any] = {
            "model": req.model,
            "messages": req.messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "metadata": {
                "run_id": req.spec.run_id,
                "agent_id": req.spec.agent_id,
                "agent_version": req.spec.agent_version,
                "tenant_id": req.spec.tenant_id,
                "trace_user_id": req.spec.subject,
            },
            **req.params,
        }
        if req.tools:
            body["tools"] = req.tools
            body["parallel_tool_calls"] = False
        else:
            body.pop("tool_choice", None)
        headers = {"Authorization": f"Bearer {self.api_key}", "x-ai-run-id": req.spec.run_id}
        if os.environ.get("TRACEPARENT"):
            headers["traceparent"] = os.environ["TRACEPARENT"]
        try:
            resp = await self._client.send(self._client.build_request("POST", self.url, json=body, headers=headers), stream=True)
        except httpx.HTTPError as exc:
            raise ShimError("UPSTREAM_UNREACHABLE", f"model gateway unreachable: {type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 400:
            detail = (await resp.aread()).decode("utf-8", "replace")[:500]
            await resp.aclose()
            raise ShimError("UPSTREAM_ERROR", f"model gateway returned {resp.status_code}: {detail}", status=resp.status_code)
        return self._iter(resp)

    @staticmethod
    async def _iter(resp: httpx.Response) -> AsyncIterator[dict[str, Any]]:
        try:
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                with contextlib.suppress(ValueError):
                    chunk = json.loads(data)
                    if isinstance(chunk, dict):
                        yield chunk
        finally:
            await resp.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()


class ScriptedBackend:
    """探针脚本模式的假模型：第 n 次调用（n = calls 文件已有行数，跨进程重启连续）按 script.json 的第 n 个应答流式产出。"""

    def __init__(self, script: Path, calls: Path) -> None:
        self.replies: list[dict[str, Any]] = json.loads(script.read_text(encoding="utf-8")).get("replies", [])
        self.calls = calls
        calls.touch()

    async def open(self, req: ModelRequest, cancelled: asyncio.Event) -> AsyncIterator[dict[str, Any]]:
        seen = from_openai_messages(req.messages[1:], req.platform_tool_name)  # 去掉首条平台 system 提示词
        with self.calls.open("r+", encoding="utf-8") as fh:
            n = sum(1 for _ in fh)
            fh.write(json.dumps(seen, ensure_ascii=False) + "\n")
        if n >= len(self.replies):
            raise ShimError("SCRIPT_EXHAUSTED", f"model call #{n} is beyond the script ({len(self.replies)} replies)", status=500)
        return self._stream(self.replies[n], req, cancelled)

    @staticmethod
    async def _stream(reply: dict[str, Any], req: ModelRequest, cancelled: asyncio.Event) -> AsyncIterator[dict[str, Any]]:
        base = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": req.model,
        }

        def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
            return {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        yield chunk({"role": "assistant", "content": ""})
        for d in reply.get("thinking", []):
            yield chunk({"reasoning_content": d})
        for d in reply.get("text", []):
            yield chunk({"content": d})
        if reply.get("hang_after_text"):
            await cancelled.wait()  # 流式输出中挂起，直到会话被中断
            return
        calls = reply.get("tool_calls") or []
        offered = [str(t["function"]["name"]) for t in req.tools]
        for i, c in enumerate(calls):
            name = next((m for m in offered if req.platform_tool_name(m) == c["name"]), c["name"])
            yield chunk(
                {
                    "tool_calls": [
                        {
                            "index": i,
                            "id": c["call_id"],
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(c.get("input", {}), ensure_ascii=False)},
                        }
                    ]
                }
            )
        yield chunk({}, "tool_calls" if calls else "stop")
        usage = reply.get("usage") or {}
        yield {
            **base,
            "choices": [],
            "usage": {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                "prompt_tokens_details": {"cached_tokens": usage.get("cache_read_tokens", 0)},
            },
        }

    async def aclose(self) -> None:
        return None


# ---------------- 端点 ----------------


class ShimHost(Protocol):
    """shim 的宿主（桥里的会话状态）。"""

    token: str
    cancelled: asyncio.Event

    @property
    def spec(self) -> SessionSpec | None: ...

    def begin_model_call(self) -> str | None:
        """一次模型请求到达：返回 None 表示允许；返回原因表示越界（宿主已按隔离违规处理）。"""
        ...

    def platform_history(self) -> list[dict[str, Any]]: ...

    def turn_tools(self) -> list[ToolSpec]: ...

    def tool_names(self) -> ToolNames:
        """本会话见过的全部平台工具（agent 可能缓存了早先 turn 的工具列表）。"""
        ...

    def model_tool_name(self, wire: str) -> str:
        """MCP 工具名 → agent 给模型看的名字（profile 前缀）。"""
        ...

    def violation(self, message: str) -> None: ...

    def emit_delta(self, event: TextEvent | ThinkingEvent) -> None: ...

    def model_call_done(self, usage: UsageEvent, calls: list[ModelToolCall], finish_reason: str | None) -> None: ...

    def model_call_failed(self, code: str, message: str) -> None: ...


def _bearer_ok(request: Request, token: str) -> bool:
    return hmac.compare_digest(request.headers.get("authorization", "").encode(), f"Bearer {token}".encode())


class LlmShim:
    """Starlette 端点：/v1/chat/completions 与 /v1/models。"""

    def __init__(self, host: ShimHost, backend: ModelBackend) -> None:
        self.host = host
        self.backend = backend

    async def models(self, request: Request) -> Response:
        if not _bearer_ok(request, self.host.token):
            return JSONResponse({"error": {"message": "unauthorized"}}, status_code=401)
        return JSONResponse({"object": "list", "data": [{"id": PLACEHOLDER_MODEL, "object": "model", "owned_by": "platform"}]})

    async def chat(self, request: Request) -> Response:
        host = self.host
        if not _bearer_ok(request, host.token):
            return JSONResponse({"error": {"message": "unauthorized"}}, status_code=401)
        if host.cancelled.is_set():
            return JSONResponse({"error": {"message": "session interrupted"}}, status_code=503)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": {"message": "invalid json"}}, status_code=400)
        spec = host.spec
        if spec is None or not isinstance(body, dict):
            host.violation("model request before the platform opened a session")
            return _forbidden("no platform session")
        reason = host.begin_model_call()
        if reason is not None:
            return _forbidden(reason)
        names = host.tool_names()
        model_to_platform = {host.model_tool_name(w): p for w, p in names.to_platform.items()}
        requested = [str((t.get("function") or {}).get("name", "")) for t in body.get("tools") or [] if isinstance(t, dict)]
        foreign = sorted(n for n in requested if n not in model_to_platform)
        if foreign:
            msg = f"agent offered non-platform tools to the model: {foreign}"
            host.violation(msg)
            return _forbidden(msg)
        offered = {model_to_platform[n] for n in requested}
        specs = [t for t in host.turn_tools() if t.name in offered]

        def model_name(platform: str) -> str:
            return host.model_tool_name(names.wire(platform))

        def platform_name(model: str) -> str:
            return model_to_platform.get(model, model)

        messages = [{"role": "system", "content": spec.system_prompt}, *to_openai_messages(host.platform_history(), model_name)]
        params = {k: body[k] for k in _FORWARDED_PARAMS if k in body}
        req = ModelRequest(spec.model_alias, messages, openai_tools(specs, model_name), params, spec, platform_name)
        turn_tools = {model_name(t.name): t.name for t in host.turn_tools()}  # 本 turn 的平台工具（模型侧名 → 平台名）
        try:
            chunks = await self.backend.open(req, host.cancelled)
        except ShimError as exc:
            host.model_call_failed(exc.code, str(exc))
            return JSONResponse({"error": {"message": str(exc), "code": exc.code}}, status_code=exc.status)
        return StreamingResponse(self._relay(chunks, req, turn_tools), media_type="text/event-stream")

    async def _relay(self, chunks: AsyncIterator[dict[str, Any]], req: ModelRequest, turn_tools: dict[str, str]) -> AsyncIterator[str]:
        """转发上游流：思维 / 文本增量即时转发；自第一个带 tool_call 增量或 finish_reason 的 chunk 起全部缓冲，流结束后
        按本 turn 的平台工具校验通过，先登记本次调用（model_call_done）再转发缓冲的 chunk 与 [DONE]。"""
        host = self.host
        state = StreamState()
        held: list[dict[str, Any]] = []
        completed = False
        try:
            async for chunk in _until(chunks, host.cancelled):
                for kind, delta in state.feed(chunk):
                    host.emit_delta(ThinkingEvent(delta=delta) if kind == "thinking" else TextEvent(delta=delta))
                if held or _holds_back(chunk):
                    held.append(chunk)
                else:
                    yield _sse(chunk)
            if host.cancelled.is_set():
                return
            calls = []
            for cid, name, args in state.tool_calls():
                if name not in turn_tools:
                    completed = True  # 已按隔离违规收尾，不再记一次调用失败；缓冲的工具调用不转发给 agent
                    host.violation(f"model called a tool that is not a platform tool of this turn: {name!r}")
                    return
                calls.append(ModelToolCall(cid, turn_tools[name], args))
            completed = True
            host.model_call_done(state.usage_event(req.model), calls, state.finish_reason)
            for chunk in held:
                yield _sse(chunk)
            yield "data: [DONE]\n\n"
        except ShimError as exc:
            completed = True  # 只记一次失败（finally 不再重复记）
            host.model_call_failed(exc.code, str(exc))
        finally:
            if not completed and not host.cancelled.is_set():
                host.model_call_failed("MODEL_STREAM_INCOMPLETE", "model stream ended before completion")
            with contextlib.suppress(Exception):
                await chunks.aclose()  # type: ignore[attr-defined]


async def _until(it: AsyncIterator[dict[str, Any]], cancelled: asyncio.Event) -> AsyncIterator[dict[str, Any]]:
    """逐个取 chunk，cancelled 置位时立即停止（不等上游下一个增量）。"""
    waiter = asyncio.ensure_future(cancelled.wait())
    try:
        while not cancelled.is_set():
            nxt = asyncio.ensure_future(anext(it))
            await asyncio.wait({nxt, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if not nxt.done():
                nxt.cancel()
                await asyncio.gather(nxt, return_exceptions=True)
                return
            try:
                yield nxt.result()
            except StopAsyncIteration:
                return
    finally:
        waiter.cancel()


def _holds_back(chunk: dict[str, Any]) -> bool:
    """带 tool_call 增量或 finish_reason 的 chunk：须等整次应答校验通过后才转发给 agent。"""
    for choice in chunk.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        if choice.get("finish_reason") or (choice.get("delta") or {}).get("tool_calls"):
            return True
    return False


def _sse(chunk: dict[str, Any]) -> str:
    return "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"


def _forbidden(message: str) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "code": "ISOLATION_VIOLATION"}}, status_code=403)

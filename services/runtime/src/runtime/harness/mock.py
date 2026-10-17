"""Mock harness for local development and CI. Model-free: echoes input and, when the user message
contains `!tool <name> {json}`, emits a tool_use so the full gate/loop path can be exercised.
（本地开发与 CI 使用的 mock harness：不依赖任何真实模型，直接回显输入；当用户消息以
`!tool <名称> {JSON}` 这一约定格式开头时，产出一个 tool_use 事件，从而可以在没有真实模型的
情况下完整跑通闸口（gate）与驱动循环（loop）的全部路径。）
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast

import httpx

from ai_contract import (
    DoneEvent,
    ErrorEvent,
    Event,
    HarnessAdapter,
    HarnessCapabilities,
    HookSet,
    SessionHandle,
    SessionSpec,
    SkillDir,
    TextEvent,
    ThinkingEvent,
    ToolSpec,
    ToolUseEvent,
    TurnInput,
    UsageEvent,
)

if TYPE_CHECKING:  # 探针包只在脚本模式 / 探针工厂中用到（可选依赖 ai-runtime[probe]），运行期按需导入
    from ai_harness_probe import ModelReply, ModelScript, ProbeTarget, ScriptedModel


def turn_delay_s() -> float:
    """MOCK_TURN_DELAY_S（故障注入，S7 §6）：回显模式每轮先等待这么多秒（可被 interrupt 打断），默认 0 不等待。
    每轮读取环境变量，测试可随时调整。"""
    try:
        return max(0.0, float(os.environ.get("MOCK_TURN_DELAY_S", "0") or 0))
    except ValueError:
        return 0.0


class MockHarness:
    """实现 HarnessAdapter 协议的模拟适配器，用于开发/测试，不依赖任何外部模型服务。

    脚本模式（S5 探针）：构造时传入 `model`（ai_harness_probe.ScriptedModel）后，每次 run_turn 即一次模型调用，
    按 ModelScript 应答（思维 → 文本 → 至多一个工具调用 → 用量 → Done），并把收到的历史记入 model.calls；
    `hang_after_text` 的应答在文本之后挂起，直到 interrupt 才以 Done(cancelled) 收尾。
    """

    def __init__(self, model: ScriptedModel | None = None) -> None:
        self._model = model  # 脚本模式的假模型；None 为默认的回显模式
        self._active: dict[str, asyncio.Event] = {}  # 脚本模式下进行中的轮次：session_id → 中断信号

    def active_turns(self) -> list[str]:
        """脚本模式下仍在进行中的轮次（session_id），探针据此判断中断后是否残留。"""
        return sorted(self._active)

    def capabilities(self) -> HarnessCapabilities:
        """声明本 harness 的能力：进程内传输，不支持原生续跑（依赖事件日志重放实现 checkpoint）。

        可治理性方面 mock 判定为 L2：轮次循环由平台的 loop.runner 驱动（loop_owner="platform"），
        工具一律经平台工具代理执行、闸口必然生效，恢复由平台事件日志重放完成；mock 自身虽不提供
        执行前拦截也不支持原生续跑，但这两项此时由平台保证，因此可以承载高风险写的审批挂起与续跑。
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
        设置了 MOCK_TURN_DELAY_S 时每轮先等待该秒数（故障注入），期间被 interrupt 即以 Done(cancelled) 收尾。
        脚本模式见类说明。
        """
        if self._model is not None:
            async for ev in self._scripted_turn(session, turn):
                yield ev
            return
        delay = turn_delay_s()
        if delay > 0 and await self._delay_interrupted(session.session_id, delay):
            yield DoneEvent(run_id=session.session_id, stop_reason="cancelled")
            return
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

    async def _delay_interrupted(self, sid: str, delay: float) -> bool:
        """回显模式的注入延迟：等待 delay 秒，期间被 interrupt 则返回 True（本轮以 Done(cancelled) 收尾）。"""
        cancel = self._active[sid] = asyncio.Event()
        try:
            await asyncio.wait_for(cancel.wait(), delay)
            return True
        except TimeoutError:
            return False
        finally:
            if self._active.get(sid) is cancel:
                del self._active[sid]

    async def _scripted_turn(self, session: SessionHandle, turn: TurnInput) -> AsyncIterator[Event]:
        """脚本模式的一轮：一次假模型调用。超出脚本（适配器之外的多余调用）以 Error → Usage → Done(error) 收尾。"""
        assert self._model is not None
        sid = session.session_id
        cancel = self._active[sid] = asyncio.Event()
        try:
            try:
                reply: ModelReply = self._model.call(turn.messages)
            except IndexError as exc:  # ai_harness_probe.ScriptExhausted
                yield ErrorEvent(code="SCRIPT_EXHAUSTED", message=str(exc))
                yield UsageEvent(model="mock")
                yield DoneEvent(run_id=sid, stop_reason="error")
                return
            for delta in reply.thinking:
                yield ThinkingEvent(delta=delta)
            for delta in reply.text:
                yield TextEvent(delta=delta)
            if reply.hang_after_text:
                await cancel.wait()
                yield UsageEvent(model="mock")
                yield DoneEvent(run_id=sid, stop_reason="cancelled")
                return
            call = reply.tool_calls[0] if reply.tool_calls else None  # 每轮至多一个工具调用（与 runner 一致）
            if call is not None:
                yield ToolUseEvent(call_id=call.call_id, tool_name=call.name, input=dict(call.input))
            u = reply.usage
            yield UsageEvent(
                input_tokens=u.input_tokens, output_tokens=u.output_tokens, cache_read_tokens=u.cache_read_tokens, model="mock"
            )
            yield DoneEvent(run_id=sid, stop_reason="tool_use" if call is not None else "end_turn")
        finally:
            if self._active.get(sid) is cancel:
                del self._active[sid]

    async def interrupt(self, session: SessionHandle, run_id: str) -> None:
        """若该会话有进行中的轮次（脚本模式，或回显模式的注入延迟），令其以 Done(cancelled) 收尾。对未知会话无副作用。"""
        cancel = self._active.get(session.session_id)
        if cancel is not None:
            cancel.set()

    async def resume(self, spec: SessionSpec, events: list[Event]) -> SessionHandle:
        """基于重放事件数量生成一个新的会话句柄，native_ref 中记录重放事件条数，便于调试观察。"""
        return SessionHandle(session_id=uuid.uuid4().hex, harness="mock", native_ref=f"replayed:{len(events)}")


# ---------------- 契约探针工厂（S5 §1.3；门禁经 runtime.harness.registry.KNOWN 延迟导入） ----------------


def _mock_leaks(adapter: HarnessAdapter) -> list[str]:
    return [f"mock turn still active (session {sid})" for sid in cast(MockHarness, adapter).active_turns()]


async def _mock_discard(adapter: HarnessAdapter) -> None:
    for cancel in list(cast(MockHarness, adapter)._active.values()):
        cancel.set()


@asynccontextmanager
async def probe_factory(script: ModelScript) -> AsyncIterator[ProbeTarget]:
    """mock 的探针工厂：MockHarness 脚本模式 + 共享的 ScriptedModel。"""
    from ai_harness_probe import BasicTarget, ScriptedModel

    model = ScriptedModel(script)
    target = BasicTarget(model, lambda: MockHarness(model=model), leak_check=_mock_leaks, discard=_mock_discard)
    try:
        yield target
    finally:
        await target.aclose()


def openai_to_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """runtime.llm.client.to_openai_messages 的逆变换：把发往网关的 OpenAI 消息还原为 runner 历史格式
    （即 invariant.rebuild_messages 的结构，供探针比较"模型实际收到的历史"）。首条 system 消息是系统提示词，不属于历史；
    工具名按 to_openai_tool_name 的约定把 "__" 还原为 "."。"""
    out: list[dict[str, Any]] = []
    for i, m in enumerate(messages):
        role = m.get("role")
        if i == 0 and role == "system":
            continue
        if role == "assistant":
            if isinstance(m.get("content"), str) and m["content"]:
                out.append({"role": "assistant", "content": m["content"]})
            for k, tc in enumerate(m.get("tool_calls") or []):
                fn = tc.get("function") or {}
                msg: dict[str, Any] = {
                    "role": "assistant",
                    "tool_use": {
                        "call_id": tc.get("id"),
                        "name": str(fn.get("name") or "").replace("__", "."),
                        "input": json.loads(fn.get("arguments") or "{}"),
                    },
                }
                if k == 0 and m.get("reasoning_content"):
                    msg["reasoning"] = m["reasoning_content"]
                out.append(msg)
        elif role == "tool":
            out.append({"role": "tool", "call_id": m.get("tool_call_id"), "content": m.get("content")})
        else:
            out.append(dict(m))
    return out


_OPENAI_TOOL_NAME = re.compile(r"[a-zA-Z0-9_-]{1,64}")


class _ScriptedSSE(httpx.AsyncByteStream):
    """一次脚本应答的 SSE 流；hang 时在文本之后挂起，直到客户端关闭响应。"""

    def __init__(self, frames: list[dict[str, Any]], tail: list[dict[str, Any]], hang: bool, owner: ScriptedGatewayTransport) -> None:
        self._frames, self._tail, self._hang, self._owner = frames, tail, hang, owner
        self._closed = asyncio.Event()
        owner.open_streams += 1

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for frame in self._frames:
            yield f"data: {json.dumps(frame, ensure_ascii=False)}\n\n".encode()
        if self._hang:
            await self._closed.wait()
            return
        for frame in self._tail:
            yield f"data: {json.dumps(frame, ensure_ascii=False)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    async def aclose(self) -> None:
        if not self._closed.is_set():
            self._closed.set()
            self._owner.open_streams -= 1


class ScriptedGatewayTransport(httpx.AsyncBaseTransport):
    """假的 LiteLLM 网关传输层（注入 LLMClient(transport=...)，不改 LiteLLMHarness）：`/v1/chat/completions` 按
    ModelScript 以 OpenAI SSE 流应答并把请求消息还原为历史格式记入 ScriptedModel；`/model/info` 返回空清单。"""

    MODEL = "probe/scripted"

    def __init__(self, model: ScriptedModel) -> None:
        self.model = model
        self.open_streams = 0  # 尚未被客户端关闭的 SSE 流

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        from runtime.llm.client import to_openai_tool_name

        if request.url.path.endswith("/model/info"):
            return httpx.Response(200, json={"data": []})
        body = json.loads(await request.aread())
        # 与 OpenAI 兼容端点一样校验函数名（只允许 [a-zA-Z0-9_-]、最长 64），探针因此覆盖带点的平台工具名
        bad = [f["function"]["name"] for f in body.get("tools") or [] if not _OPENAI_TOOL_NAME.fullmatch(f["function"]["name"])]
        if bad:
            return httpx.Response(400, json={"error": {"message": f"Invalid 'tools[].function.name': {bad}"}})
        try:
            reply = self.model.call(openai_to_history(body["messages"]))
        except IndexError as exc:  # ai_harness_probe.ScriptExhausted：脚本之外的调用
            return httpx.Response(400, json={"error": {"message": str(exc)}})

        def chunk(delta: dict[str, Any], **extra: Any) -> dict[str, Any]:
            return {"model": self.MODEL, "choices": [{"index": 0, "delta": delta, **extra}]}

        frames = [chunk({"reasoning_content": d}) for d in reply.thinking] + [chunk({"content": d}) for d in reply.text]
        tail = [
            chunk(
                {
                    "tool_calls": [
                        {
                            "index": i,
                            "id": c.call_id,
                            "type": "function",
                            "function": {"name": to_openai_tool_name(c.name), "arguments": json.dumps(c.input, ensure_ascii=False)},
                        }
                    ]
                }
            )
            for i, c in enumerate(reply.tool_calls)
        ]
        final = chunk({}, finish_reason="tool_calls" if reply.tool_calls else "stop")
        final["usage"] = {
            "prompt_tokens": reply.usage.input_tokens,
            "completion_tokens": reply.usage.output_tokens,
            "prompt_tokens_details": {"cached_tokens": reply.usage.cache_read_tokens},
        }
        tail.append(final)
        stream = _ScriptedSSE(frames, tail, reply.hang_after_text, self)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)


@asynccontextmanager
async def litellm_probe_factory(script: ModelScript) -> AsyncIterator[ProbeTarget]:
    """litellm 的探针工厂：真实 LiteLLMHarness + LLMClient，只把 httpx 传输层换成 ScriptedGatewayTransport。"""
    from ai_harness_probe import BasicTarget, ScriptedModel
    from runtime.harness.litellm import LiteLLMHarness
    from runtime.llm.client import LLMClient

    model = ScriptedModel(script)
    transports: dict[int, ScriptedGatewayTransport] = {}

    def make() -> HarnessAdapter:
        transport = ScriptedGatewayTransport(model)
        client = LLMClient(base_url="http://probe-gateway", api_key="probe", timeout_s=30, max_retries=0, transport=transport)
        adapter = LiteLLMHarness(client=client)
        transports[id(adapter)] = transport
        return adapter

    def leaks(adapter: HarnessAdapter) -> list[str]:
        out = [f"litellm turn still active (session {sid})" for sid in sorted(getattr(adapter, "_active", set()))]
        open_streams = transports[id(adapter)].open_streams
        if open_streams:
            out.append(f"{open_streams} gateway stream(s) still open")
        return out

    target = BasicTarget(model, make, leak_check=leaks)
    try:
        yield target
    finally:
        await target.aclose()

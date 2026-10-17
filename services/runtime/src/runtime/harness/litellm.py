"""LiteLLM harness：平台持有循环（loop_owner="platform"）的模型适配器。

本 harness 只负责"一轮模型调用"：把 runner 提供的历史翻译为 OpenAI 格式，经 LiteLLM 网关流式调用
模型别名，把推理 / 文本 / 工具调用映射为统一事件。轮次循环、工具执行与闸口判定全部由平台的
loop.runner 与工具代理完成，因此本 harness 不执行任何工具，capability_tier 为 L2（ADR-0008）。

每轮事件顺序固定为：Thinking*/Text* → ToolUse?（至多一个）→ Usage（每轮必有，D5 决策）→ Done。

约束：每轮只支持一个工具调用——请求里带 `parallel_tool_calls: False`，即便模型仍返回多个也只取第一个
（runner 每轮也只处理一个工具调用）；多工具并行留待 S5/S7。

推理模型回传：DeepSeek 等推理模型在思考模式下要求回传发起工具调用那一轮的 reasoning_content。
本 harness 不在进程内缓存推理文本：runner 依事件日志把该轮 ThinkingEvent 拼接后放进 tool_use 历史消息的
`reasoning` 字段，翻译历史时据此输出 reasoning_content。这样实际发给模型的内容完全由历史决定（受可重建性
不变量覆盖），跨进程续跑也不会因缺推理文本而 400，不同 Run 之间也不会串用。

异常收口（D5）：模型调用失败（LLMError）与任何非预期异常（HARNESS_ERROR）都产出 Error → Usage → Done(error)；
finish_reason=length 且工具调用参数因截断无法解析时，不产出 ToolUse，改为 Error(TOOL_CALL_TRUNCATED) → Usage →
Done(max_tokens)，避免执行一个参数残缺的工具调用。

图片输入（S6，D102）：历史里的图片只是 ``image_ref`` 引用（Contract V1.2）；发送前才从本 Run 工作区读取、校验 sha256，
转成 OpenAI 兼容的 ``image_url`` data URL 放进请求体，不进入任何事件。读取失败（工件缺失、指纹不符）产出
Error(ATTACHMENT_UNAVAILABLE) → Usage → Done(error)。

会话回收：会话规格按 session_id 缓存；interrupt 即会话结束（runner 在每次 drive 结束时调用），立即删除规格，
若该会话正有一轮在进行则同时令其在下一个增量处停止。未被 interrupt 的会话由有界缓存按插入顺序淘汰。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal

from ai_contract import (
    DoneEvent,
    ErrorEvent,
    Event,
    HarnessCapabilities,
    HookSet,
    ImageRefError,
    SessionHandle,
    SessionSpec,
    SkillDir,
    TextEvent,
    ThinkingEvent,
    ToolSpec,
    ToolUseEvent,
    TurnInput,
    UsageEvent,
    is_image_ref,
    load_image_ref,
)
from runtime.llm import client as llm
from runtime.llm.client import LLMChunk, LLMClient, LLMError, LLMUsage, ToolCall

MAX_CACHED_SESSIONS = 4096  # 进程内缓存的会话规格上限（超出时淘汰最早的）
ERROR_MESSAGE_LIMIT = 500  # HARNESS_ERROR 消息中保留的异常描述字符数


def _put_bounded(cache: dict[str, Any], key: str, value: Any, limit: int) -> None:
    """写入一个按插入顺序淘汰的有界缓存（dict 保持插入顺序，超限时弹出最早的键）。"""
    cache.pop(key, None)
    cache[key] = value
    while len(cache) > limit:
        cache.pop(next(iter(cache)))


def _is_truncated(call: ToolCall) -> bool:
    """工具调用参数是否解析失败（客户端把非法 JSON 保留为 {"_raw": 原始字符串}）。"""
    return set(call.arguments) == {"_raw"}


def _with_images(messages: list[dict[str, Any]], workspace_dir: str | None) -> list[dict[str, Any]]:
    """把 OpenAI 消息里的 image_ref 块换成 image_url data URL（同步读文件，在线程中执行；不修改入参）。"""
    out: list[dict[str, Any]] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list) and any(is_image_ref(p) for p in content):
            parts: list[Any] = []
            for p in content:
                if is_image_ref(p):
                    media_type, data = load_image_ref(workspace_dir, p)
                    parts.append({"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}})
                else:
                    parts.append(p)
            m = {**m, "content": parts}
        out.append(m)
    return out


class LiteLLMHarness:
    """经 LiteLLM 网关调用模型的 HarnessAdapter 实现，模型无关（具体模型由 SessionSpec.model_alias 经网关路由）。"""

    def __init__(self, client: LLMClient | None = None) -> None:
        self._client = client  # 显式注入的客户端；为 None 时每轮取进程级默认客户端（便于测试替换传输层）
        self._specs: dict[str, SessionSpec] = {}  # session_id → 会话规格（interrupt 时删除）
        self._active: set[str] = set()  # 正在跑一轮的 session_id
        self._cancelled: set[str] = set()  # 进行中的一轮已被请求中断的 session_id（只记录 _active 中的会话）
        self._pumps: dict[str, asyncio.Task[None]] = {}  # session_id → 读取上游流的任务（interrupt 时取消）

    def _llm(self) -> LLMClient:
        """返回本轮使用的 LLM 客户端。"""
        return self._client or llm.get_client()

    def capabilities(self) -> HarnessCapabilities:
        """平台持有循环：工具经平台工具代理执行、恢复靠平台事件日志重放，因此自身无需拦截与原生续跑。"""
        return HarnessCapabilities(
            name="litellm",
            version="0.1",
            transport="in-process",
            models=[],  # 模型无关，经 Model Gateway 路由
            supports_vision=True,  # 图片以 image_url data URL 发送；别名是否支持由 Runtime 按 VISION_MODEL_ALIASES 把关
            supports_native_resume=False,
            loop_owner="platform",
            tool_interception="none",
            can_resume_from_events=False,
        )

    async def open_session(self, spec: SessionSpec) -> SessionHandle:
        """开启会话：生成 session_id 并缓存会话规格（模型别名、系统提示词）。"""
        session_id = uuid.uuid4().hex
        _put_bounded(self._specs, session_id, spec, MAX_CACHED_SESSIONS)
        return SessionHandle(session_id=session_id, harness="litellm")

    async def resume(self, spec: SessionSpec, events: list[Event]) -> SessionHandle:
        """续跑与新开会话相同：历史由 runner 依事件日志重建后逐轮传入，本 harness 无需原生状态。"""
        handle = await self.open_session(spec)
        return handle.model_copy(update={"native_ref": f"replayed:{len(events)}"})

    async def interrupt(self, session: SessionHandle, run_id: str) -> None:
        """结束会话：删除会话规格（之后该会话的 run_turn 返回 SESSION_NOT_FOUND）；若正有一轮在进行，
        令其在收到下一个增量时停止，并以 stop_reason="cancelled" 结束。空闲会话不留下任何中断标记。"""
        self._specs.pop(session.session_id, None)
        if session.session_id in self._active:
            self._cancelled.add(session.session_id)
            pump = self._pumps.get(session.session_id)
            if pump is not None:
                pump.cancel()  # 上游可能长时间不出增量：直接停止等待，而不是等下一个增量

    async def _model_name(self, client: LLMClient, alias: str, reported: str | None) -> str:
        """UsageEvent.model 的取值：网关回报的模型与别名不同（如发生回退）时以回报为准，
        否则用别名解析出的底层模型；解析失败（任何原因，失败结果由客户端短期负缓存）时退回回报值或别名本身。"""
        if reported and reported != alias:
            return reported
        try:
            return (await client.resolve_alias(alias)).model
        except Exception:  # 仅用于标注用量，尽力而为，绝不影响本轮结果
            return reported or alias

    @staticmethod
    def _usage_event(usage: LLMUsage | None, model: str | None) -> UsageEvent:
        """把 LLMUsage 映射为统一 UsageEvent（output_tokens 已含推理 token）。"""
        u = usage or LLMUsage()
        return UsageEvent(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=u.cache_read_tokens,
            cost=u.cost,
            model=model,
        )

    async def run_turn(
        self, session: SessionHandle, turn: TurnInput, tools: list[ToolSpec], skills: list[SkillDir], hooks: HookSet
    ) -> AsyncIterator[Event]:
        """驱动一轮模型调用并产出统一事件流（顺序见模块说明）。失败时产出 ErrorEvent + UsageEvent + Done(error)。"""
        sid = session.session_id
        spec = self._specs.get(sid)
        if spec is None:
            yield ErrorEvent(code="SESSION_NOT_FOUND", message=f"litellm session '{sid}' not found")
            yield UsageEvent()
            yield DoneEvent(run_id=sid, stop_reason="error")
            return
        run_id = spec.run_id
        usage: LLMUsage | None = None
        reported_model: str | None = None
        self._active.add(sid)
        try:
            if hooks.pre_model:
                verdict = await hooks.pre_model({"messages": turn.messages, "model_alias": spec.model_alias})
                if verdict and "deny" in verdict:
                    yield ErrorEvent(code="PRE_MODEL_DENIED", message=str(verdict["deny"]))
                    yield UsageEvent()
                    yield DoneEvent(run_id=run_id, stop_reason="error")
                    return

            # 系统提示词为空时不发送空 system 消息（部分厂商拒绝空内容块）。
            system = [{"role": "system", "content": spec.system_prompt}] if spec.system_prompt else []
            messages = system + llm.to_openai_messages(turn.messages)
            if any(isinstance(m.get("content"), list) for m in messages):
                messages = await asyncio.to_thread(_with_images, messages, spec.workspace_dir)
            openai_tools = llm.to_openai_tools(tools)
            name_map = {llm.to_openai_tool_name(t.name): t.name for t in tools}  # OpenAI 函数名 → 平台工具名
            if len(name_map) < len({t.name for t in tools}):
                # 两个平台工具名编码成同一个 OpenAI 函数名：模型的调用无法还原，拒绝本轮（收口为 HARNESS_ERROR）
                raise ValueError(f"platform tool names collide after OpenAI name encoding: {sorted(t.name for t in tools)}")
            extra = {"parallel_tool_calls": False} if openai_tools else None

            client = self._llm()
            tool_calls: list[ToolCall] = []
            finish_reason: str | None = None
            cancelled = False
            stream = client.stream_chat(spec.model_alias, messages, tools=openai_tools or None, extra=extra)
            chunks: asyncio.Queue[LLMChunk | Exception | None] = asyncio.Queue()

            async def pump() -> None:
                # 整条流在同一个任务里打开、读取、关闭；interrupt 取消该任务即可立即停止等待上游
                try:
                    async for c in stream:
                        chunks.put_nowait(c)
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    chunks.put_nowait(exc)
                finally:
                    await stream.aclose()  # 提前结束（中断）时关闭底层 HTTP 流
                    chunks.put_nowait(None)

            task = asyncio.create_task(pump())
            # 任务在首次调度前就被 interrupt 取消时协程体不会执行，finally 里的结束标记也就不会入队：由完成回调
            # 再补一个，保证下面的 chunks.get() 不会永久挂起（正常结束时多出的一个 None 在循环退出后无人读取，无害）。
            task.add_done_callback(lambda _: chunks.put_nowait(None))
            self._pumps[sid] = task
            try:
                while (chunk := await chunks.get()) is not None:
                    if isinstance(chunk, Exception):
                        raise chunk
                    if sid in self._cancelled:
                        break
                    if chunk.kind == "thinking":
                        yield ThinkingEvent(delta=chunk.delta)  # visibility 由 runner 按 Agent 配置覆写
                    elif chunk.kind == "text":
                        yield TextEvent(delta=chunk.delta)
                    elif chunk.kind == "tool_call" and chunk.tool_call is not None:
                        tool_calls.append(chunk.tool_call)
                    elif chunk.kind == "usage":
                        usage, reported_model = chunk.usage, chunk.model
                    elif chunk.kind == "done":
                        finish_reason, reported_model = chunk.finish_reason, chunk.model or reported_model
                cancelled = sid in self._cancelled
            finally:
                task = self._pumps.pop(sid)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

            if cancelled:
                yield self._usage_event(usage, reported_model)
                yield DoneEvent(run_id=run_id, stop_reason="cancelled")
                return

            tc = tool_calls[0] if tool_calls else None  # 每轮只支持一个工具调用（见模块说明）
            usage_ev = self._usage_event(usage, await self._model_name(client, spec.model_alias, reported_model))
            if tc is not None and finish_reason == "length" and _is_truncated(tc):
                # 输出被 max_tokens 截断导致参数残缺：不能把它当作工具调用交给 runner 执行。
                yield ErrorEvent(
                    code="TOOL_CALL_TRUNCATED",
                    message=f"tool call '{name_map.get(tc.name, tc.name)}' arguments truncated by max_tokens",
                    retryable=False,
                )
                yield usage_ev
                yield DoneEvent(run_id=run_id, stop_reason="max_tokens")
                return
            if tc is not None:
                yield ToolUseEvent(call_id=tc.id, tool_name=name_map.get(tc.name, tc.name), input=tc.arguments)
            yield usage_ev
            stop: Literal["tool_use", "max_tokens", "end_turn"]
            if tc is not None:
                stop = "tool_use"
            elif finish_reason == "length":
                stop = "max_tokens"
            else:
                stop = "end_turn"
            yield DoneEvent(run_id=run_id, stop_reason=stop)
        except LLMError as e:
            yield ErrorEvent(code=e.code, message=str(e), retryable=e.retryable)
            yield self._usage_event(usage, reported_model)
            yield DoneEvent(run_id=run_id, stop_reason="error")
        except ImageRefError as e:  # 图片附件读不到或与记录的指纹不符：不发请求（消息里只有引用，不含图片内容）
            yield ErrorEvent(code="ATTACHMENT_UNAVAILABLE", message=str(e), retryable=False)
            yield self._usage_event(usage, reported_model)
            yield DoneEvent(run_id=run_id, stop_reason="error")
        except Exception as e:  # 非预期异常同样收口为错误事件，保证每轮必有 Usage + Done（D5）
            yield ErrorEvent(code="HARNESS_ERROR", message=f"{type(e).__name__}: {e}"[:ERROR_MESSAGE_LIMIT], retryable=False)
            yield self._usage_event(usage, reported_model)
            yield DoneEvent(run_id=run_id, stop_reason="error")
        finally:
            self._active.discard(sid)
            self._cancelled.discard(sid)

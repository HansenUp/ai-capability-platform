"""PlatformSim：在探针里扮演 runtime 的 runner + 工具代理，不依赖 runtime 包。

与 runner 保持一致的语义（探针以此为"平台侧期望"）：
- 每轮以当前历史调用一次 `run_turn`，在 DoneEvent 处停止消费；
- 收到 UsageEvent 视为本次模型输出结束，非空文本以 `{"role": "assistant", "content": ...}` 追加进历史；
- 一轮内只处理最后一个 ToolUseEvent（与 runner 相同；适配器应把多个工具调用串行成多轮），由平台执行工具后
  把 tool_use 消息与 tool 结果消息追加进历史，再进入下一轮；本轮没有工具调用即结束；
- 适配器产出的平台专属事件（ToolResultEvent / CardEvent / InputRequiredEvent）不进历史，只记入 `forged`；
- 历史格式与 `runtime.run.invariant.rebuild_messages` 相同（见 ai_harness_probe.target）。

Hooks：平台在 pre_model / post_model / pre_tool / post_tool 四个点自行执行 `hooks` 中的函数（返回值按
`HOOK_OVERRIDABLE` 过滤，`{"deny": reason}` 阻断，hook 抛异常按失败关闭处理）；传给 run_turn 的 HookSet 只含
适配器在 `internal_hook_points` 声明的点。所有 hook 调用记入 `hook_calls`（source 区分平台侧 / 适配器侧）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from ai_contract import (
    HOOK_OVERRIDABLE,
    CardEvent,
    DoneEvent,
    Event,
    HarnessAdapter,
    HookPoint,
    HookSet,
    InputRequiredEvent,
    SessionHandle,
    SessionSpec,
    TextEvent,
    ThinkingEvent,
    ToolResultEvent,
    ToolSpec,
    ToolUseEvent,
    TurnInput,
    UsageEvent,
)

# 平台侧工具执行函数：返回工具输出；抛异常表示执行失败（结果记为 status=error，输出为异常信息）。
ToolHandler = Callable[[ToolUseEvent], Awaitable[Any]]

# PlatformSim.run 的停止原因：前五个来自适配器的 DoneEvent；awaiting 表示调用了 suspend_tools 中的工具而挂起；
# hook_denied 表示 pre_model / post_model 拒绝；max_rounds 表示超过轮数上限。
SimStop = Literal["end_turn", "tool_use", "max_tokens", "cancelled", "error", "awaiting", "hook_denied", "max_rounds"]

PLATFORM_ONLY_EVENTS = (ToolResultEvent, CardEvent, InputRequiredEvent)

DEFAULT_SPEC = SessionSpec(
    run_id="probe-run",
    agent_id="probe-agent",
    agent_version="1",
    model_alias="probe-model",
    system_prompt="You are a contract probe.",
    subject="human:probe",
)


async def _echo_tool(call: ToolUseEvent) -> Any:
    """默认工具实现：返回可逐字节核对的确定性结果。"""
    return f"result:{call.call_id}"


def messages_hash(messages: list[dict[str, Any]]) -> str:
    """与 runtime.run.invariant.message_hash 相同的规范化哈希（sort_keys、紧凑分隔符、UTF-8 原文）。"""
    canon = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


@dataclass
class ToolExecution:
    """平台侧的一次工具处理记录（含被 hook 拒绝、未真正执行的调用）。"""

    call_id: str
    tool_name: str
    input: dict[str, Any]  # 实际用于执行的入参（pre_tool 改写后的 effective input）
    status: Literal["ok", "error", "denied"]
    output: Any  # 回灌给模型的结果（post_tool 处理后）
    executed: bool  # 是否真的调用了 tool_handler（被 pre_tool 拒绝时为 False）


@dataclass
class HookCall:
    """一次 hook 调用记录。"""

    point: HookPoint
    source: Literal["platform", "adapter"]  # platform：PlatformSim 自己执行；adapter：适配器经 run_turn 的 HookSet 调用
    payload: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None = None  # hook 抛出的异常（按失败关闭处理）


@dataclass
class _ModelOutput:
    text: list[str] = field(default_factory=list)
    thinking: list[str] = field(default_factory=list)
    tool_call: ToolUseEvent | None = None
    denied: bool = False


class PlatformSim:
    """扮演 runner 驱动一个适配器：开会话 / 重放恢复、逐轮 run_turn、执行平台侧工具并回灌、执行四点 hooks、记录一切。

    记录（只增不改，供探针判定）：
    - `events`：适配器产出的全部事件（含被丢弃的平台专属事件），按到达顺序；`turns`：按 run_turn 分组的同一批事件；
    - `history`：当前模型可见历史（rebuild_messages 格式）；`executions`：平台侧工具处理记录；
    - `hook_calls`：hook 调用记录；`forged`：适配器伪造的平台专属事件；`ignored_overrides`：被白名单过滤掉的覆盖键；
    - `stop_reason`：最近一次 run() 的停止原因。
    """

    def __init__(
        self,
        adapter: HarnessAdapter,
        *,
        spec: SessionSpec = DEFAULT_SPEC,
        tools: Sequence[ToolSpec] = (),
        tool_handler: ToolHandler = _echo_tool,
        hooks: HookSet | None = None,
        suspend_tools: Sequence[str] = (),
        max_rounds: int = 8,
    ) -> None:
        self.adapter = adapter
        self.spec = spec
        self.tools = list(tools)
        self.tool_handler = tool_handler
        self.hooks = hooks or HookSet()
        self.suspend_tools = set(suspend_tools)  # 调用这些工具时像等待审批一样挂起：不执行、不回灌，run() 以 awaiting 返回
        self.max_rounds = max_rounds
        self.session: SessionHandle | None = None
        self.pending: ToolUseEvent | None = None  # 挂起中的工具调用（已进历史、尚无结果）
        self.stop_reason: SimStop | None = None
        self.events: list[Event] = []
        self.turns: list[list[Event]] = []
        self.history: list[dict[str, Any]] = []
        self.executions: list[ToolExecution] = []
        self.hook_calls: list[HookCall] = []
        self.forged: list[Event] = []
        self.ignored_overrides: list[tuple[HookPoint, str]] = []
        self._log: list[Event] = []  # 事件日志投影（等价于 EventLog.project_unified_events 的输出），供 resume 使用
        self._round = 0

    # ---------------- 会话 ----------------

    async def open(self) -> SessionHandle:
        """以 spec 开新会话。"""
        self.session = await self.adapter.open_session(self.spec)
        return self.session

    async def resume(self, adapter: HarnessAdapter | None = None) -> SessionHandle:
        """用（可能是 restart() 后的新）适配器从事件日志投影重建会话，与 runner 续跑一致；之后调用 run() 继续。"""
        if adapter is not None:
            self.adapter = adapter
        self.session = await self.adapter.resume(self.spec, self.unified_events())
        return self.session

    async def interrupt(self) -> None:
        """中断当前会话（与取消 API / runner 收尾时的 harness.interrupt 相同）。"""
        if self.session is not None:
            await self.adapter.interrupt(self.session, self.spec.run_id)

    def unified_events(self) -> list[Event]:
        """当前事件日志的统一事件投影：助手文本 → TextEvent，工具请求 → ToolUseEvent，工具结果 → ToolResultEvent。"""
        return list(self._log)

    # ---------------- 驱动 ----------------

    async def run(self, messages: Sequence[dict[str, Any]] | str = ()) -> SimStop:
        """追加 messages（字符串视为一条用户消息）后驱动到结束、挂起或出错，返回并记录停止原因。
        未开会话时先 open()；存在挂起调用时先补完它（执行工具并回灌），与 runner 续跑相同。"""
        if self.session is None:
            await self.open()
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        self.history.extend(dict(m) for m in messages)
        if self.pending is not None:
            call, self.pending = self.pending, None
            await self._execute(call)
        self.stop_reason = await self._loop()
        return self.stop_reason

    async def _loop(self) -> SimStop:
        for _ in range(self.max_rounds):
            self._round += 1
            pre = {
                "round": self._round,
                "model_alias": self.spec.model_alias,
                "n_messages": len(self.history),
                "messages_hash": messages_hash(self.history),
            }
            denied, _ = await self._hook(HookPoint.PRE_MODEL, pre)
            if denied is not None:
                return "hook_denied"
            out, stop = await self._turn()
            if stop in ("cancelled", "error"):
                return cast(SimStop, stop)
            if out.denied:
                return "hook_denied"
            if out.tool_call is None:
                return cast(SimStop, stop)
            call = out.tool_call
            tool_use: dict[str, Any] = {
                "role": "assistant",
                "tool_use": {"call_id": call.call_id, "name": call.tool_name, "input": call.input},
            }
            if out.thinking:
                tool_use["reasoning"] = "".join(out.thinking)
            self.history.append(tool_use)
            self._log.append(call)
            if call.tool_name in self.suspend_tools:
                self.pending = call
                return "awaiting"
            await self._execute(call)
        return "max_rounds"

    async def _turn(self) -> tuple[_ModelOutput, str]:
        """驱动一次 run_turn，返回本轮模型输出与适配器的停止原因（没有 DoneEvent 时按 error）。"""
        assert self.session is not None
        out = _ModelOutput()
        stop = "error"
        batch: list[Event] = []
        self.turns.append(batch)
        gen = cast(
            AsyncGenerator[Event, None],
            self.adapter.run_turn(
                self.session,
                # 与 runner 相同：按工具结果记录填写 tool_statuses（Contract V1.1）
                TurnInput(messages=list(self.history), tool_statuses={e.call_id: e.status for e in self.executions}),
                self.tools,
                [],
                self._adapter_hooks(),
            ),
        )
        async with aclosing(gen) as it:
            async for ev in it:
                self.events.append(ev)
                batch.append(ev)
                if isinstance(ev, TextEvent):
                    out.text.append(ev.delta)
                elif isinstance(ev, ThinkingEvent):
                    out.thinking.append(ev.delta)
                elif isinstance(ev, ToolUseEvent):
                    out.tool_call = ev
                elif isinstance(ev, PLATFORM_ONLY_EVENTS):
                    self.forged.append(ev)
                elif isinstance(ev, UsageEvent):
                    await self._model_done(out, ev)
                elif isinstance(ev, DoneEvent):
                    stop = ev.stop_reason
                    break
        if out.denied:
            out.tool_call = None
        return out, stop

    async def _model_done(self, out: _ModelOutput, usage: UsageEvent) -> None:
        """一次模型输出结束：执行 post_model；未被拒绝时把文本写进历史与事件日志投影。"""
        payload = {
            "text": "".join(out.text),
            "thinking": "".join(out.thinking),
            "tool_call": out.tool_call.model_dump(mode="json") if out.tool_call else None,
            "usage": usage.model_dump(mode="json"),
        }
        denied, _ = await self._hook(HookPoint.POST_MODEL, payload)
        if denied is not None:
            out.denied = True
            return
        if out.text:
            text = "".join(out.text)
            self.history.append({"role": "assistant", "content": text})
            self._log.append(TextEvent(delta=text))
        out.text.clear()  # 同一轮再次出现 Usage 时只记新增文本

    async def _execute(self, call: ToolUseEvent) -> None:
        """平台侧处理一次工具调用：pre_tool → 执行 → post_tool → 回灌历史。历史中的 tool_use 保持模型原始入参。"""
        status: Literal["ok", "error", "denied"] = "ok"
        effective = dict(call.input)
        executed = False
        denied, over = await self._hook(HookPoint.PRE_TOOL, {"call_id": call.call_id, "tool": call.tool_name, "input": dict(call.input)})
        if denied is not None:
            status, output = "denied", denied
        else:
            if isinstance(over.get("input"), dict):
                effective = over["input"]
            executed = True
            try:
                output = await self.tool_handler(call.model_copy(update={"input": effective}))
            except Exception as exc:  # 工具失败：结果记为 error，与 toolbroker 一致不中断 Run
                status, output = "error", f"{type(exc).__name__}: {exc}"
            post = {"call_id": call.call_id, "tool": call.tool_name, "status": status, "output": output}
            denied, over = await self._hook(HookPoint.POST_TOOL, post)
            if denied is not None:
                status, output = "error", f"output withheld by hook: {denied}"
            elif "output" in over:
                output = over["output"]
        self.executions.append(ToolExecution(call.call_id, call.tool_name, effective, status, output, executed))
        self.history.append({"role": "tool", "call_id": call.call_id, "content": output})
        self._log.append(ToolResultEvent(call_id=call.call_id, status=status, output=output))

    # ---------------- hooks ----------------

    async def _hook(self, point: HookPoint, payload: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
        """平台侧执行一个点的 hook。返回 (拒绝原因或 None, 白名单内的覆盖键)。hook 抛异常：按拒绝处理（失败关闭）。"""
        fn = getattr(self.hooks, point.value)
        if fn is None:
            return None, {}
        try:
            result = await fn(dict(payload))
        except Exception as exc:
            self.hook_calls.append(HookCall(point, "platform", payload, None, f"{type(exc).__name__}: {exc}"))
            return f"hook error: {type(exc).__name__}", {}
        self.hook_calls.append(HookCall(point, "platform", payload, result))
        if not result:
            return None, {}
        if "deny" in result:
            return str(result["deny"]), {}
        allowed = HOOK_OVERRIDABLE[point]
        self.ignored_overrides.extend((point, k) for k in result if k not in allowed)
        return None, {k: v for k, v in result.items() if k in allowed}

    def _adapter_hooks(self) -> HookSet:
        """传给 run_turn 的 HookSet：只含适配器声明的 internal_hook_points，调用经包装记入 hook_calls。"""
        declared = self.adapter.capabilities().internal_hook_points
        fns: dict[str, Any] = {}
        for point in declared:
            fn = getattr(self.hooks, point.value)
            if fn is not None:
                fns[point.value] = self._recording(point, fn)
        return HookSet(**fns)

    def _recording(self, point: HookPoint, fn: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]) -> Any:
        async def wrapped(payload: dict[str, Any]) -> dict[str, Any] | None:
            result = await fn(payload)
            self.hook_calls.append(HookCall(point, "adapter", dict(payload), result))
            return result

        return wrapped

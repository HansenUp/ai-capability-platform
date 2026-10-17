"""探针自检：一个正确的参考适配器五项全过；七种故意做坏的适配器各被对应探针判不通过；门禁 CLI 的汇总与退出码。

参考适配器声明 loop_owner=harness + enforcing + can_resume_from_events，使能力门禁（capability_violations）真正生效。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest

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
    ToolResultEvent,
    ToolSpec,
    ToolUseEvent,
    TurnInput,
    UsageEvent,
)
from ai_harness_probe import (
    ALL,
    ENFORCING_HOOK_CHECKS,
    EVENT_STREAM,
    HOOK_DENY_REACHES_MODEL,
    HOOK_NO_MODEL_CALL_AFTER_DENY,
    HOOKS,
    INTERRUPT,
    REPLAY_RESUME,
    TOOL_ROUNDTRIP,
    BasicTarget,
    ModelReply,
    ModelScript,
    ProbeTarget,
    ScriptedModel,
    ScriptExhausted,
    probes,
    run_probes,
)
from ai_harness_probe.gate import main as gate_main
from ai_harness_probe.gate import run_gate


class RefAdapter:
    """正确的参考适配器：每次 run_turn 即一次脚本模型调用；interrupt 结束会话并令挂起的轮次以 Done(cancelled) 收尾。"""

    def __init__(self, model: ScriptedModel) -> None:
        self.model = model
        self.sessions: set[str] = set()
        self.active: dict[str, asyncio.Event] = {}

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            name=type(self).__name__,
            version="0",
            transport="in-process",
            loop_owner="harness",
            tool_interception="enforcing",
            can_resume_from_events=True,
        )

    async def open_session(self, spec: SessionSpec) -> SessionHandle:
        sid = uuid.uuid4().hex
        self.sessions.add(sid)
        return SessionHandle(session_id=sid, harness="ref")

    async def resume(self, spec: SessionSpec, events: list[Event]) -> SessionHandle:
        return await self.open_session(spec)

    async def interrupt(self, session: SessionHandle, run_id: str) -> None:
        self.sessions.discard(session.session_id)
        cancel = self.active.get(session.session_id)
        if cancel is not None:
            cancel.set()

    def history_for_model(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return messages

    async def hang(self, cancel: asyncio.Event) -> None:
        await cancel.wait()

    def usage(self, reply: ModelReply) -> list[Event]:
        u = reply.usage
        return [UsageEvent(input_tokens=u.input_tokens, output_tokens=u.output_tokens, cache_read_tokens=u.cache_read_tokens)]

    async def after_tool_use(self, call: ToolUseEvent, messages: list[dict[str, Any]]) -> AsyncIterator[Event]:
        yield call

    async def run_turn(
        self, session: SessionHandle, turn: TurnInput, tools: list[ToolSpec], skills: list[SkillDir], hooks: HookSet
    ) -> AsyncIterator[Event]:
        sid = session.session_id
        if sid not in self.sessions:
            yield ErrorEvent(code="SESSION_NOT_FOUND", message=sid)
            yield UsageEvent()
            yield DoneEvent(run_id=sid, stop_reason="error")
            return
        cancel = self.active[sid] = asyncio.Event()
        try:
            try:
                reply = self.model.call(self.history_for_model(turn.messages))
            except ScriptExhausted as exc:
                yield ErrorEvent(code="SCRIPT_EXHAUSTED", message=str(exc))
                yield UsageEvent()
                yield DoneEvent(run_id=sid, stop_reason="error")
                return
            for d in reply.thinking:
                yield ThinkingEvent(delta=d)
            for d in reply.text:
                yield TextEvent(delta=d)
            if reply.hang_after_text:
                await self.hang(cancel)
                yield UsageEvent()
                yield DoneEvent(run_id=sid, stop_reason="cancelled")
                return
            call = reply.tool_calls[0] if reply.tool_calls else None
            if call is not None:
                async for ev in self.after_tool_use(
                    ToolUseEvent(call_id=call.call_id, tool_name=call.name, input=call.input), turn.messages
                ):
                    yield ev
            for ev in self.usage(reply):
                yield ev
            yield DoneEvent(run_id=sid, stop_reason="tool_use" if call is not None else "end_turn")
        finally:
            self.active.pop(sid, None)


class ForgedToolResult(RefAdapter):
    """坏：上报工具调用后自己伪造一条 tool_result 事件。"""

    async def after_tool_use(self, call: ToolUseEvent, messages: list[dict[str, Any]]) -> AsyncIterator[Event]:
        yield call
        yield ToolResultEvent(call_id=call.call_id, output="fabricated")


class MissingUsage(RefAdapter):
    """坏：从不上报 UsageEvent。"""

    def usage(self, reply: ModelReply) -> list[Event]:
        return []


class SelfExecutesTools(RefAdapter):
    """坏：不把工具调用交给平台，自己"执行"后把结果喂回模型、在同一轮里继续。"""

    async def after_tool_use(self, call: ToolUseEvent, messages: list[dict[str, Any]]) -> AsyncIterator[Event]:
        history = [
            *messages,
            {"role": "assistant", "tool_use": {"call_id": call.call_id, "name": call.tool_name, "input": call.input}},
            {"role": "tool", "call_id": call.call_id, "content": "self-executed"},
        ]
        for d in self.model.call(history).text:
            yield TextEvent(delta=d)


class IgnoresInterrupt(RefAdapter):
    """坏：interrupt 什么也不做，挂起的轮次永不结束。"""

    async def interrupt(self, session: SessionHandle, run_id: str) -> None:
        return None

    async def hang(self, cancel: asyncio.Event) -> None:
        await asyncio.Event().wait()


class TreatsDeniedAsSuccess(RefAdapter):
    """坏（Hooks）：忽略平台对工具调用的拒绝，把被拒工具当作执行成功——以自己的"成功"结果替换拒绝原因喂给模型。
    未被拒绝的结果原样转交，因此工具回灌探针仍然通过，只有 Hooks 的适配器侧判据能判出。"""

    def history_for_model(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            dict(m, content="ok: executed by adapter") if m.get("role") == "tool" and "deny" in str(m.get("content")) else m
            for m in messages
        ]


class CallsModelDuringTeardown(RefAdapter):
    """坏（Hooks）：会话被 interrupt 拆除时，像后台 CLI 拿到失败的工具结果那样又自行请求一次模型。"""

    async def interrupt(self, session: SessionHandle, run_id: str) -> None:
        if session.session_id in self.sessions:
            with contextlib.suppress(ScriptExhausted):
                self.model.call([{"role": "user", "content": "continue after teardown"}])
        await super().interrupt(session, run_id)


class RedecidesOnResume(RefAdapter):
    """坏：resume 后丢掉挂起的工具调用与结果，让模型重新决策。"""

    resumed = False

    async def resume(self, spec: SessionSpec, events: list[Event]) -> SessionHandle:
        self.resumed = True
        return await super().resume(spec, events)

    def history_for_model(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not self.resumed:
            return messages
        self.resumed = False
        out = list(messages)
        while out and (out[-1].get("role") == "tool" or "tool_use" in out[-1]):
            out.pop()
        return out


def factory_for(cls: type[RefAdapter]) -> Callable[[ModelScript], Any]:
    def leaks(a: HarnessAdapter) -> list[str]:
        ref = a if isinstance(a, RefAdapter) else None
        assert ref is not None
        return [f"session {s} open" for s in ref.sessions] + [f"turn {s} active" for s in ref.active]

    async def discard(a: HarnessAdapter) -> None:
        assert isinstance(a, RefAdapter)
        a.sessions.clear()
        for e in a.active.values():
            e.set()

    @asynccontextmanager
    async def factory(script: ModelScript) -> AsyncIterator[ProbeTarget]:
        model = ScriptedModel(script)
        target = BasicTarget(model, lambda: cls(model), leak_check=leaks, discard=discard)
        try:
            yield target
        finally:
            await target.aclose()

    return factory


@pytest.fixture(autouse=True)
def fast_deadlines(monkeypatch: pytest.MonkeyPatch) -> None:
    """缩短中断/泄漏等待，让"忽略中断"的坏适配器不至于拖慢测试。"""
    monkeypatch.setattr(probes, "INTERRUPT_DEADLINE_S", 0.5)
    monkeypatch.setattr(probes, "LEAK_GRACE_S", 0.2)


async def test_reference_adapter_passes_all_five():
    report = await run_probes(factory_for(RefAdapter))
    assert [r.name for r in report.results] == list(ALL)
    assert report.passed, [(r.name, r.failures) for r in report.results]
    assert report.capability_violations() == []


@pytest.mark.parametrize(
    ("cls", "probe", "needle"),
    [
        (ForgedToolResult, EVENT_STREAM, "platform-only events: ToolResultEvent"),
        (MissingUsage, EVENT_STREAM, "no UsageEvent before DoneEvent"),
        (SelfExecutesTools, TOOL_ROUNDTRIP, "platform tool handler ran 0 times"),
        (IgnoresInterrupt, INTERRUPT, "no DoneEvent within"),
        (RedecidesOnResume, REPLAY_RESUME, "model did not receive tool_use"),
        (TreatsDeniedAsSuccess, HOOKS, "pre_tool deny → model call #1: tool_result content"),
        (CallsModelDuringTeardown, HOOKS, "pre_model deny (round 2): model was called 2 times"),
    ],
)
async def test_broken_adapter_fails_its_probe(cls: type[RefAdapter], probe: str, needle: str):
    report = await run_probes(factory_for(cls), probes=[probe])
    result = report.result(probe)
    assert result is not None and not result.passed
    assert any(needle in f for f in result.failures), result.failures
    assert not report.passed


async def test_broken_adapters_other_details():
    """补充：伪造结果也被工具回灌判出；忽略中断会留下残留；自行执行工具同时破坏 Hooks；声明 enforcing 却未通过即能力违规。"""
    rt = (await run_probes(factory_for(ForgedToolResult), probes=[TOOL_ROUNDTRIP])).result(TOOL_ROUNDTRIP)
    assert rt is not None and any("fabricated" in f for f in rt.failures)
    it = (await run_probes(factory_for(IgnoresInterrupt), probes=[INTERRUPT])).result(INTERRUPT)
    assert it is not None and any("leaked" in f for f in it.failures)
    report = await run_probes(factory_for(SelfExecutesTools), probes=[TOOL_ROUNDTRIP, HOOKS])
    hk = report.result(HOOKS)
    assert hk is not None and not hk.passed
    assert "tool_interception=enforcing requires probes tool_roundtrip and hooks to pass" in report.capability_violations()


async def test_hooks_adapter_side_evidence():
    """参考适配器的 Hooks 结果带全部适配器侧证据，平台侧子项在 notes 里单独标注；忽略拒绝的坏适配器工具回灌照样通过，
    但缺少 deny_reaches_model 证据，声明 enforcing 即能力违规；拆除时调用模型的坏适配器缺少 no_model_call 证据。"""
    ref = (await run_probes(factory_for(RefAdapter), probes=[HOOKS])).result(HOOKS)
    assert ref is not None and ref.passed and set(ENFORCING_HOOK_CHECKS) <= set(ref.adapter_checks)
    assert any(n.startswith(probes.PLATFORM_SIDE) for n in ref.notes)

    report = await run_probes(factory_for(TreatsDeniedAsSuccess), probes=[TOOL_ROUNDTRIP, HOOKS])
    rt, hk = report.result(TOOL_ROUNDTRIP), report.result(HOOKS)
    assert rt is not None and rt.passed
    assert hk is not None and not hk.passed and HOOK_DENY_REACHES_MODEL not in hk.adapter_checks
    assert "tool_interception=enforcing requires probes tool_roundtrip and hooks to pass" in report.capability_violations()

    hk = (await run_probes(factory_for(CallsModelDuringTeardown), probes=[HOOKS])).result(HOOKS)
    assert hk is not None and not hk.passed and HOOK_NO_MODEL_CALL_AFTER_DENY not in hk.adapter_checks
    assert HOOK_DENY_REACHES_MODEL in hk.adapter_checks


async def test_gate_reports_and_fails_closed(tmp_path):
    known = {
        "ref": f"{__name__}:REF_FACTORY",
        "redecide": f"{__name__}:REDECIDE_FACTORY",
        "missing": "no_such_module_for_probe_gate:probe_factory",
    }
    result = await run_gate(known)
    assert not result["passed"]
    h = result["harnesses"]
    assert h["ref"]["passed"] and h["ref"]["capability_violations"] == []
    assert not h["redecide"]["passed"] and h["redecide"]["capability_violations"] == [
        "can_resume_from_events=True requires probe replay_resume to pass"
    ]
    assert not h["missing"]["passed"] and "cannot load probe factory" in h["missing"]["error"]
    unknown = await run_gate(known, harnesses=["nope"])
    assert not unknown["passed"] and "not in the gate scope" in unknown["harnesses"]["nope"]["error"]
    json.dumps(result)  # 报告可直接序列化


def test_gate_cli_exit_codes(tmp_path, monkeypatch: pytest.MonkeyPatch):
    report = tmp_path / "probe-report.json"
    assert gate_main(["--known", f"{__name__}:KNOWN_OK", "--report", str(report)]) == 0
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["passed"] and set(data["harnesses"]) == {"ref"}
    assert gate_main(["--known", f"{__name__}:KNOWN_BAD", "--report", str(report)]) == 1
    assert not json.loads(report.read_text(encoding="utf-8"))["passed"]


REF_FACTORY = factory_for(RefAdapter)
REDECIDE_FACTORY = factory_for(RedecidesOnResume)
KNOWN_OK = {"ref": f"{__name__}:REF_FACTORY"}
KNOWN_BAD = {"ref": f"{__name__}:REF_FACTORY", "redecide": f"{__name__}:REDECIDE_FACTORY"}

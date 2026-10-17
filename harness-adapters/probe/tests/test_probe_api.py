"""探针 API 骨架测试：ModelScript、PlatformSim（工具回灌、挂起与重放续跑、四点 hooks）、run_probes 汇总与能力门禁。

用一个最小的脚本化适配器（每次 run_turn 即一次模型调用，历史原样即规范化格式）驱动 PlatformSim。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from ai_contract import (
    DoneEvent,
    Event,
    HarnessAdapter,
    HarnessCapabilities,
    HookPoint,
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
    HOOK_NO_SELF_EXECUTION,
    HOOKS,
    REPLAY_RESUME,
    TOOL_ROUNDTRIP,
    ModelReply,
    ModelScript,
    PlatformSim,
    ProbeReport,
    ProbeResult,
    ProbeTarget,
    ScriptedToolCall,
    ScriptedUsage,
    ScriptExhausted,
    run_probes,
)


class _Model:
    """假模型：按脚本应答，记录每次调用收到的消息；跨适配器实例共享（模拟 restart 后同一模型）。"""

    def __init__(self, script: ModelScript) -> None:
        self.script = script
        self.calls: list[list[dict[str, Any]]] = []


class ScriptedAdapter:
    def __init__(self, model: _Model, internal: list[str] | None = None, forge: bool = False) -> None:
        self.model = model
        self.internal = internal or []
        self.forge = forge
        self.resumed_from: list[Event] | None = None

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            name="scripted",
            version="0",
            transport="in-process",
            loop_owner="platform",
            internal_hook_points=self.internal,
        )

    async def open_session(self, spec: SessionSpec) -> SessionHandle:
        return SessionHandle(session_id="s1", harness="scripted")

    async def run_turn(
        self, session: SessionHandle, turn: TurnInput, tools: list[ToolSpec], skills: list[SkillDir], hooks: HookSet
    ) -> AsyncIterator[Event]:
        if hooks.pre_model is not None and await hooks.pre_model({"n": len(turn.messages)}):
            yield DoneEvent(run_id="r", stop_reason="error")
            return
        self.model.calls.append([dict(m) for m in turn.messages])
        reply = self.model.script.reply_for(len(self.model.calls) - 1)
        for d in reply.thinking:
            yield ThinkingEvent(delta=d)
        for d in reply.text:
            yield TextEvent(delta=d)
        for c in reply.tool_calls:
            yield ToolUseEvent(call_id=c.call_id, tool_name=c.name, input=c.input)
        if self.forge:
            yield ToolResultEvent(call_id="forged", output="x")
        yield UsageEvent(input_tokens=reply.usage.input_tokens, output_tokens=reply.usage.output_tokens)
        yield DoneEvent(run_id="r", stop_reason="tool_use" if reply.tool_calls else "end_turn")

    async def interrupt(self, session: SessionHandle, run_id: str) -> None:
        return None

    async def resume(self, spec: SessionSpec, events: list[Event]) -> SessionHandle:
        self.resumed_from = events
        return SessionHandle(session_id="s2", harness="scripted")


class _Target:
    def __init__(self, script: ModelScript) -> None:
        self.model = _Model(script)
        self._adapter = ScriptedAdapter(self.model)

    @property
    def adapter(self) -> HarnessAdapter:
        return self._adapter

    def received(self) -> list[list[dict[str, Any]]]:
        return self.model.calls

    def leaks(self) -> list[str]:
        return []

    async def restart(self) -> HarnessAdapter:
        self._adapter = ScriptedAdapter(self.model)
        return self._adapter


@asynccontextmanager
async def factory(script: ModelScript) -> AsyncIterator[ProbeTarget]:
    yield _Target(script)


TOOL = ToolSpec(name="crm.search", description="search")
SCRIPT = ModelScript(
    replies=[
        ModelReply(
            thinking=["想", "一想"],
            text=["查", "一下"],
            tool_calls=[ScriptedToolCall(call_id="c1", name="crm.search", input={"q": "acme"})],
            usage=ScriptedUsage(input_tokens=10, output_tokens=4),
        ),
        ModelReply(text=["好了"], usage=ScriptedUsage(input_tokens=20, output_tokens=2)),
    ]
)

EXPECTED_HISTORY = [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "查一下"},
    {"role": "assistant", "tool_use": {"call_id": "c1", "name": "crm.search", "input": {"q": "acme"}}, "reasoning": "想一想"},
    {"role": "tool", "call_id": "c1", "content": "result:c1"},
    {"role": "assistant", "content": "好了"},
]


def test_model_script_reply_and_totals():
    assert SCRIPT.reply_for(0).full_text == "查一下" and SCRIPT.reply_for(0).full_thinking == "想一想"
    with pytest.raises(ScriptExhausted):
        SCRIPT.reply_for(2)
    assert SCRIPT.total_usage() == ScriptedUsage(input_tokens=30, output_tokens=6)
    assert SCRIPT.total_usage(1).output_tokens == 4


async def test_sim_tool_roundtrip_matches_rebuild_format():
    """PlatformSim 执行平台侧工具并回灌；历史与模型收到的消息都是 rebuild_messages 格式。"""
    async with factory(SCRIPT) as t:
        sim = PlatformSim(t.adapter, tools=[TOOL])
        assert await sim.run("hi") == "end_turn"
        assert sim.history == EXPECTED_HISTORY
        assert t.received() == [EXPECTED_HISTORY[:1], EXPECTED_HISTORY[:4]]
        assert [(e.call_id, e.status, e.executed) for e in sim.executions] == [("c1", "ok", True)]
        assert [type(e).__name__ for e in sim.unified_events()] == ["TextEvent", "ToolUseEvent", "ToolResultEvent", "TextEvent"]
        assert len(sim.turns) == 2 and isinstance(sim.turns[0][-1], DoneEvent)


async def test_sim_drops_forged_platform_events():
    async with factory(SCRIPT) as t:
        t._adapter.forge = True
        sim = PlatformSim(t.adapter)
        await sim.run("hi")
        assert len(sim.forged) == 2 and sim.history == EXPECTED_HISTORY


async def test_sim_suspend_restart_resume():
    """挂起工具不执行；restart 后 resume 用事件日志投影重建，续跑先补完挂起调用、不重新决策。"""
    async with factory(SCRIPT) as t:
        sim = PlatformSim(t.adapter, tools=[TOOL], suspend_tools=["crm.search"])
        assert await sim.run("hi") == "awaiting" and sim.executions == []
        new = await t.restart()
        await sim.resume(new)
        assert [type(e).__name__ for e in new.resumed_from] == ["TextEvent", "ToolUseEvent"]  # type: ignore[attr-defined]
        assert await sim.run() == "end_turn"
        assert len(t.received()) == 2 and t.received()[1] == EXPECTED_HISTORY[:4]
        assert [e.call_id for e in sim.executions] == ["c1"]


async def test_sim_platform_hooks():
    """pre_tool 改写入参（白名单外的键被忽略）、post_tool 改写输出；pre_tool 拒绝时不执行工具；pre_model 拒绝时无模型调用。"""
    executed: list[dict[str, Any]] = []

    async def handler(call: ToolUseEvent) -> Any:
        executed.append(call.input)
        return "raw"

    async def pre_tool(p: dict[str, Any]) -> dict[str, Any]:
        return {"input": {"q": "rewritten"}, "tool": "evil"}

    async def post_tool(p: dict[str, Any]) -> dict[str, Any]:
        return {"output": "redacted"}

    async with factory(SCRIPT) as t:
        sim = PlatformSim(t.adapter, tool_handler=handler, hooks=HookSet(pre_tool=pre_tool, post_tool=post_tool))
        await sim.run("hi")
        assert executed == [{"q": "rewritten"}]
        assert sim.history[2]["tool_use"]["input"] == {"q": "acme"}  # 历史保留模型原始入参
        assert sim.history[3]["content"] == "redacted"
        assert sim.ignored_overrides == [(HookPoint.PRE_TOOL, "tool")]

    async def deny(p: dict[str, Any]) -> dict[str, Any]:
        return {"deny": "no"}

    async with factory(SCRIPT) as t:
        sim = PlatformSim(t.adapter, tool_handler=handler, hooks=HookSet(pre_tool=deny))
        executed.clear()
        await sim.run("hi")
        assert executed == [] and sim.executions[0].status == "denied" and sim.history[3]["content"] == "no"

    async with factory(SCRIPT) as t:
        sim = PlatformSim(t.adapter, hooks=HookSet(pre_model=deny))
        assert await sim.run("hi") == "hook_denied" and t.received() == []


async def test_sim_passes_only_declared_internal_hooks():
    calls: list[str] = []

    async def pre_model(p: dict[str, Any]) -> None:
        calls.append("x")
        return None

    async with factory(SCRIPT) as t:
        t._adapter.internal = ["pre_model"]
        sim = PlatformSim(t.adapter, hooks=HookSet(pre_model=pre_model))
        await sim.run("hi")
        sources = [(c.point, c.source) for c in sim.hook_calls]
        assert (HookPoint.PRE_MODEL, "adapter") in sources and (HookPoint.PRE_MODEL, "platform") in sources


async def test_run_probes_fail_closed(monkeypatch):
    """探针抛出任何异常（含 NotImplementedError）或超时都记为不通过；未知探针名报错。"""
    from ai_harness_probe import probes

    async def boom(factory):
        raise NotImplementedError("not yet")

    async def slow(factory):
        await asyncio.sleep(10)

    monkeypatch.setitem(probes.PROBES, EVENT_STREAM, boom)
    monkeypatch.setitem(probes.PROBES, HOOKS, slow)
    report = await run_probes(factory, probes=[EVENT_STREAM, HOOKS, EVENT_STREAM], timeout_s=0.1)
    assert report.harness == "scripted" and [r.name for r in report.results] == [EVENT_STREAM, HOOKS]
    assert not report.passed and "NotImplementedError" in report.results[0].failures[0]
    assert "timed out" in report.results[1].failures[0]
    with pytest.raises(ValueError):
        await run_probes(factory, probes=["bogus"])


def test_capability_violations():
    caps = HarnessCapabilities(name="h", version="0", transport="stdio", tool_interception="enforcing", can_resume_from_events=True)

    def report(*passed: str, evidence: tuple[str, ...] = ENFORCING_HOOK_CHECKS) -> ProbeReport:
        results = [
            ProbeResult(name=n, passed=n in passed, adapter_checks=list(evidence) if n == HOOKS and n in passed else []) for n in ALL
        ]
        return ProbeReport(harness="h", capabilities=caps, results=results)

    assert len(report(EVENT_STREAM).capability_violations()) == 2
    assert report(*ALL).passed and report(*ALL).capability_violations() == []
    assert report(TOOL_ROUNDTRIP, HOOKS).capability_violations() == ["can_resume_from_events=True requires probe replay_resume to pass"]
    # Hooks 通过但缺少适配器侧证据（只有平台侧子项）：enforcing 不成立
    weak = report(*ALL, evidence=(HOOK_DENY_REACHES_MODEL,)).capability_violations()
    assert len(weak) == 1 and "adapter-side hooks checks" in weak[0] and HOOK_NO_SELF_EXECUTION in weak[0]
    assert REPLAY_RESUME in ALL
    platform = ProbeReport(harness="p", capabilities=caps.model_copy(update={"loop_owner": "platform"}))
    assert platform.capability_violations() == []

"""真实 dsh 的契约探针与隔离用例（本机门禁；二进制不存在时 skip，CI 默认不下载第三方二进制）。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ai_contract import ErrorEvent, ToolSpec
from ai_harness_probe import ModelReply, ModelScript, PlatformSim, ScriptedToolCall, ScriptedUsage, run_probes
from ai_harness_stdio.acp.bridge import ISOLATION_VIOLATION
from ai_harness_stdio.acp.testing import acp_probe_factory
from ai_harness_stdio.dsh.profile import dsh_binary, node_binary
from ai_harness_stdio.dsh.testing import pooled_probe_factory, probe_factory

FAKES = Path(__file__).parent / "fakes"
pytestmark = pytest.mark.skipif(dsh_binary() is None or node_binary() is None, reason="dsh not installed")
TOOL = ToolSpec(name="probe.lookup", description="lookup", input_schema={"type": "object", "properties": {"q": {"type": "string"}}})


def _report(report):
    return [(r.name, r.failures) for r in report.results], report.capability_violations()


async def test_dsh_passes_five_probes():
    """真实 dsh + ACP 桥 + shim 脚本模式：五项契约探针全过，能力声明不高于探针验证。"""
    report = await run_probes(probe_factory, timeout_s=90)
    assert report.passed, _report(report)
    caps = report.capabilities
    assert (caps.name, caps.loop_owner, caps.tool_interception, caps.can_resume_from_events) == ("dsh", "harness", "enforcing", True)


async def test_dsh_passes_five_probes_pooled():
    """进程池模式（hello→ready 预热、进程组回收）。"""
    report = await run_probes(pooled_probe_factory, timeout_s=90)
    assert report.passed, _report(report)


async def test_dsh_without_isolation_is_rejected(monkeypatch: pytest.MonkeyPatch):
    """patch 不禁用 dsh 内置工具（bash / fs / subagent 等）：shim 在第一次模型请求就判 ISOLATION_VIOLATION，模型不被调用、工具不被执行。"""
    monkeypatch.syspath_prepend(str(FAKES))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(FAKES), os.environ.get("PYTHONPATH", "")]))
    script = ModelScript(
        replies=[
            ModelReply(tool_calls=[ScriptedToolCall(call_id="c1", name=TOOL.name, input={"q": "x"})], usage=ScriptedUsage(output_tokens=1))
        ]
    )
    executed: list[str] = []

    async def handler(ev):
        executed.append(ev.call_id)
        return "never"

    async with acp_probe_factory(script, profile="rogue_profiles:DshWithBuiltinTools", name="dsh") as target:
        sim = PlatformSim(target.adapter, tools=[TOOL], tool_handler=handler)
        stop = await sim.run("hi")
        await sim.interrupt()
        assert stop == "error", sim.events
        assert [e.code for e in sim.events if isinstance(e, ErrorEvent)] == [ISOLATION_VIOLATION]
        assert target.received() == [] and executed == []
        assert target.leaks() == []

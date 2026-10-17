"""ACP 桥本身（CI 用 fake ACP agent，不需要 goose / dsh）：五项契约探针（直连与进程池模式）、隔离违规失败关闭、
能力声明与进程回收。"""

from __future__ import annotations

import functools
import json
import os
import sys
from pathlib import Path

import pytest

from ai_contract import ErrorEvent, SessionSpec, ToolSpec, UsageEvent
from ai_harness_probe import ModelReply, ModelScript, PlatformSim, ScriptedToolCall, ScriptedUsage, run_probes
from ai_harness_stdio import PoolConfig
from ai_harness_stdio.acp.bridge import (
    ISOLATION_VIOLATION,
    AcpBridge,
    AcpStdioHarness,
    CommandProfile,
    _Turn,
    cached_agent_version,
    version_from_cli,
)
from ai_harness_stdio.acp.testing import acp_probe_factory

FAKE = Path(__file__).parent / "fakes" / "fake_acp_agent.py"
FACTORY = functools.partial(acp_probe_factory, profile="cmd", agent_argv=[sys.executable, str(FAKE)], name="fake-acp")
TOOL = ToolSpec(name="probe.lookup", description="lookup", input_schema={"type": "object", "properties": {"q": {"type": "string"}}})
SPEC = SessionSpec(run_id="r", agent_id="a", agent_version="1", model_alias="m", system_prompt="s", subject="human:u")


def _report(report):  # 失败时给出可读原因
    return [(r.name, r.failures) for r in report.results], report.capability_violations()


async def test_fake_agent_passes_five_probes():
    report = await run_probes(FACTORY, timeout_s=40)
    assert report.passed, _report(report)
    caps = report.capabilities
    assert (caps.loop_owner, caps.tool_interception, caps.can_resume_from_events) == ("harness", "enforcing", True)


async def test_fake_agent_passes_five_probes_pooled():
    """进程池模式：worker 经 hello→ready 预热（agent 已启动并完成 ACP initialize），按进程组回收。"""
    pooled = functools.partial(FACTORY, pool_config=PoolConfig(min_idle=1, max_workers=4))
    report = await run_probes(pooled, timeout_s=40)
    assert report.passed, _report(report)


def test_capabilities_declared_before_open():
    """开会话前（registry / 门禁）就报告 harness 持有循环，而不是 StdioHarness 的平台持有循环默认值；version 为
    agent 二进制版本（取不到为空串，发布闸门据此 409），不再兜底 "0"。"""
    from ai_harness_stdio.goose.profile import GooseProfile

    caps = AcpStdioHarness("goose").capabilities()
    assert (caps.name, caps.loop_owner, caps.tool_interception, caps.transport) == ("goose", "harness", "enforcing", "stdio")
    assert caps.version == GooseProfile().agent_version() and caps.version != "0"
    assert CommandProfile(["x"]).capabilities("").version == ""


def test_agent_version_from_cli_is_cached_by_binary_mtime(tmp_path: Path):
    calls: list[str] = []
    binary = tmp_path / "agent"
    binary.write_text("#!/bin/sh\necho 'agent 1.2.3-rc.1 (abc)'\n")
    binary.chmod(0o755)

    def probe(path: str) -> str:
        calls.append(path)
        return version_from_cli(path)

    assert cached_agent_version(str(binary), probe) == "1.2.3-rc.1"
    assert cached_agent_version(str(binary), probe) == "1.2.3-rc.1" and len(calls) == 1  # 进程内缓存
    binary.write_text("#!/bin/sh\necho 'agent 1.2.4'\n")
    os.utime(binary, ns=(1, 10**18))  # 升级二进制（mtime 变化）后重新探测
    assert cached_agent_version(str(binary), probe) == "1.2.4" and len(calls) == 2
    assert cached_agent_version(None, probe) == "" and cached_agent_version(str(tmp_path / "missing"), probe) == ""


def test_dsh_version_from_package_json(tmp_path: Path):
    """dsh：从启动脚本 realpath 向上找 @deepseek-ai/dsh/package.json（ACP agentInfo.version 是适配层的 0.0.1）。"""
    from ai_harness_stdio.dsh.profile import package_version

    pkg = tmp_path / "node_modules" / "@deepseek-ai" / "dsh"
    (pkg / "lib").mkdir(parents=True)
    (pkg / "lib" / "bin.js").write_text("")
    (pkg / "package.json").write_text(json.dumps({"name": "@deepseek-ai/dsh", "version": "0.2.0-rc.2"}))
    (tmp_path / "node_modules" / ".bin").mkdir()
    link = tmp_path / "node_modules" / ".bin" / "dsh"
    link.symlink_to("../@deepseek-ai/dsh/lib/bin.js")
    assert package_version(str(link)) == "0.2.0-rc.2"
    assert package_version(str(tmp_path / "elsewhere.js")) == ""


async def test_concurrent_model_request_in_one_turn_is_isolation_violation():
    """begin 即计入进行中调用：第一个模型请求尚未结束时的第二个（并发）请求同样越界。"""
    bridge = AcpBridge(CommandProfile(["-"]), backend=None)  # type: ignore[arg-type]  # 不启动 agent / HTTP
    bridge.open(SPEC)
    turn = bridge.turn = _Turn()
    assert bridge.begin_model_call() is None
    reason = bridge.begin_model_call()
    assert reason is not None and "concurrent" in reason and bridge.broken
    events = [turn.queue.get_nowait() for _ in range(turn.queue.qsize())]
    assert [e.type for e in events] == ["error", "usage", "done"] and events[0].code == ISOLATION_VIOLATION


async def test_model_retry_after_failed_call_is_allowed_but_not_a_second_success():
    bridge = AcpBridge(CommandProfile(["-"]), backend=None)  # type: ignore[arg-type]
    bridge.open(SPEC)
    bridge.turn = _Turn()
    assert bridge.begin_model_call() is None
    bridge.model_call_failed("UPSTREAM_ERROR", "502")  # 失败的调用不计数，agent 可重试
    assert bridge.begin_model_call() is None
    bridge.model_call_done(UsageEvent(output_tokens=1), [], "stop")
    assert bridge.begin_model_call() is not None and bridge.broken


def test_bridge_consumes_secret_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """模型网关密钥经 0600 文件传入：桥读完即删、环境变量移除。"""
    from ai_harness_stdio.acp.bridge import _consume_secret

    secret = tmp_path / ".acp_llm_api_key"
    secret.write_text("sk-platform\n")
    monkeypatch.setenv("ACP_LLM_API_KEY_FILE", str(secret))
    assert _consume_secret("ACP_LLM_API_KEY") == "sk-platform"
    assert not secret.exists() and "ACP_LLM_API_KEY_FILE" not in os.environ


TOOL_SCRIPT = ModelScript(
    replies=[
        ModelReply(
            text=["查一下"],
            tool_calls=[ScriptedToolCall(call_id="c1", name=TOOL.name, input={"q": "x"})],
            usage=ScriptedUsage(output_tokens=3),
        ),
        ModelReply(text=["好了"], usage=ScriptedUsage(output_tokens=1)),
    ]
)


async def _run_rogue(monkeypatch: pytest.MonkeyPatch, flag: str, script: ModelScript = TOOL_SCRIPT) -> tuple[PlatformSim, list[str]]:
    monkeypatch.setenv(flag, "1")
    executed: list[str] = []

    async def handler(ev):
        executed.append(ev.call_id)
        return "should-not-run"

    async with FACTORY(script) as target:
        sim = PlatformSim(target.adapter, tools=[TOOL], tool_handler=handler)
        stop = await sim.run("hi")
        await sim.interrupt()
        assert stop == "error", (flag, stop, sim.events)
        assert target.leaks() == []
    return sim, executed


@pytest.mark.parametrize(
    "flag",
    ["ACP_AGENT_ROGUE_TOOL", "ACP_AGENT_EXTRA_MODEL_CALL", "ACP_AGENT_SELF_TOOL", "ACP_AGENT_ROGUE_PERMISSION"],
)
async def test_isolation_violations_fail_closed(monkeypatch: pytest.MonkeyPatch, flag: str):
    """非平台工具、harness 侧自行多轮、未经模型请求的工具调用、为非平台调用请求权限：一律 ISOLATION_VIOLATION，
    平台工具不被执行。"""
    script = (
        ModelScript(replies=[ModelReply(text=["只说话"], usage=ScriptedUsage(output_tokens=1))])
        if flag == "ACP_AGENT_EXTRA_MODEL_CALL"
        else TOOL_SCRIPT
    )
    sim, executed = await _run_rogue(monkeypatch, flag, script)
    errors = [e for e in sim.events if isinstance(e, ErrorEvent)]
    assert [e.code for e in errors] == [ISOLATION_VIOLATION], sim.events
    assert executed == []


async def test_no_processes_left_after_session():
    async with FACTORY(TOOL_SCRIPT) as target:
        sim = PlatformSim(target.adapter, tools=[TOOL])
        assert await sim.run("hi") == "end_turn"
        await sim.interrupt()
        assert target.leaks() == []


async def test_tool_call_before_model_stream_settles_is_claimed():
    """agent 常在流式应答的 finish_reason 处就请求权限 / 调工具，早于 shim 读完用量块并登记工具调用（真实 DeepSeek +
    goose 实测）：桥须等模型流结束再认领，而不是误判为隔离违规。"""
    import asyncio

    from ai_contract import SessionSpec, UsageEvent
    from ai_harness_stdio.acp.bridge import AcpBridge, CommandProfile, _Turn
    from ai_harness_stdio.acp.shim import ModelToolCall

    bridge = AcpBridge(CommandProfile(["-"]), backend=None)  # type: ignore[arg-type]  # 不启动 agent / HTTP
    bridge.open(SessionSpec(run_id="r", agent_id="a", agent_version="1", model_alias="m", system_prompt="s", subject="human:u"))
    bridge.names.add(TOOL.name)
    turn = bridge.turn = _Turn()
    assert bridge.begin_model_call() is None
    perm = asyncio.ensure_future(
        bridge._on_agent_request(
            "session/request_permission",
            {
                "toolCall": {"toolCallId": "m1"},
                "options": [{"optionId": "ok", "kind": "allow_once"}, {"optionId": "no", "kind": "reject_once"}],
            },
        )
    )
    call = asyncio.ensure_future(bridge.call_tool("probe_lookup", {"q": "x"}, "m1"))
    await asyncio.sleep(0.05)
    assert not perm.done() and not call.done()
    bridge.model_call_done(UsageEvent(output_tokens=2), [ModelToolCall("m1", TOOL.name, {"q": "x"})], "tool_calls")
    assert (await perm) == {"outcome": {"outcome": "selected", "optionId": "ok"}}
    await asyncio.sleep(0.05)
    events = [turn.queue.get_nowait() for _ in range(turn.queue.qsize())]
    assert [e.type for e in events] == ["tool_use", "usage", "done"] and not bridge.broken
    bridge.pending["m1"].future.set_result(("result", False))
    assert (await call) == ("result", False)

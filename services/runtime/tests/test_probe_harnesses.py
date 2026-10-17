"""S5 契约探针接入（WP-C）：mock / litellm / stdio 三个适配器五项探针全过；锁定探针的规范化消息格式与
invariant.rebuild_messages 一致（PlatformSim.history、各假模型记录的 received() 都按此格式比较）。"""

from __future__ import annotations

from typing import Any

import pytest

from ai_contract import RunEvent, RunEventType, ToolSpec
from ai_harness_probe import ModelReply, ModelScript, PlatformSim, ScriptedToolCall, ScriptedUsage, run_probes
from ai_harness_probe.gate import load_entry
from runtime.harness.mock import openai_to_history
from runtime.harness.registry import KNOWN
from runtime.llm.client import to_openai_messages
from runtime.run.invariant import rebuild_messages


@pytest.mark.parametrize("name", ["mock", "litellm", "stdio"])
async def test_adapter_passes_all_probes(name: str):
    report = await run_probes(load_entry(KNOWN[name]))
    assert report.passed, {r.name: r.failures for r in report.results if not r.passed} or report.capability_violations()


SCRIPT = ModelScript(
    replies=[
        ModelReply(
            thinking=["想一想"],
            text=["先查"],
            tool_calls=[ScriptedToolCall(call_id="c1", name="crm.search", input={"q": "acme"})],
            usage=ScriptedUsage(input_tokens=3, output_tokens=2),
        ),
        ModelReply(text=["好了"], usage=ScriptedUsage(input_tokens=5, output_tokens=1)),
    ]
)


async def test_sim_history_matches_rebuild_messages():
    """PlatformSim 的历史 == 由等价事件日志 rebuild_messages 重建的历史；模型第二次调用收到的正是该历史的前缀。"""
    async with load_entry(KNOWN["mock"])(SCRIPT) as target:
        sim = PlatformSim(target.adapter, tools=[ToolSpec(name="crm.search", description="search")])
        assert await sim.run("hi") == "end_turn"
        received = target.received()
    ex = sim.executions[0]
    log = [
        RunEvent(run_id="r", seq=1, type=RunEventType.MODEL_RESPONSE, payload={"text": "先查"}),
        RunEvent(
            run_id="r",
            seq=2,
            type=RunEventType.TOOL_REQUESTED,
            payload={"call_id": "c1", "tool": "crm.search", "input": {"q": "acme"}, "reasoning": "想一想"},
        ),
        RunEvent(
            run_id="r", seq=3, type=RunEventType.TOOL_RESULT, payload={"call_id": ex.call_id, "status": ex.status, "output": ex.output}
        ),
        RunEvent(run_id="r", seq=4, type=RunEventType.MODEL_RESPONSE, payload={"text": "好了"}),
    ]
    initial: list[dict[str, Any]] = [{"role": "user", "content": "hi"}]
    assert sim.history == rebuild_messages(log, initial)
    assert received == [initial, rebuild_messages(log[:3], initial)]


def test_litellm_fake_gateway_inverts_openai_translation():
    """litellm 假网关把 OpenAI 消息还原为历史格式：to_openai_messages 的逆变换（系统提示词除外）。"""
    history: list[dict[str, Any]] = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "先查"},
        {"role": "assistant", "tool_use": {"call_id": "c1", "name": "crm.search", "input": {"q": "值"}}, "reasoning": "想"},
        {"role": "tool", "call_id": "c1", "content": "result"},
        {"role": "assistant", "content": "好了"},
    ]
    sent = [{"role": "system", "content": "sys"}, *to_openai_messages(history)]
    assert openai_to_history(sent) == history

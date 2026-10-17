"""runner 对 harness 停止原因的终态映射：error → failed、cancelled → canceled、max_tokens → completed（如实记录）；
以及 drive 收尾时的会话中断、取消 API 中断运行中的 harness、思维链进入可重建历史、harness 异常与无错误事件的
error 收尾。

用一个按脚本产出事件的假 harness 注册到 registry，经 /v1/messages 跑完整条链路，断言 Run 终态与事件日志。
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from ai_contract import (
    DoneEvent,
    ErrorEvent,
    Event,
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
from runtime.harness import registry
from runtime.loop import active


class ScriptedHarness:
    """按脚本产出事件的 harness：每次 run_turn 弹出一段脚本；脚本里的异常实例在该位置抛出。
    同时记录打开会话的 run_id、每轮收到的消息与 interrupt 调用，供断言。"""

    def __init__(self, name: str, turns: list[list[Event | Exception]]) -> None:
        self._name = name
        self._turns = turns
        self.run_ids: list[str] = []  # 每次 open_session / resume 的 run_id
        self.seen: list[list[dict[str, Any]]] = []  # 每轮 run_turn 收到的 turn.messages
        self.interrupts: list[str] = []  # 每次 interrupt 的 run_id

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(name=self._name, version="test", transport="in-process", loop_owner="platform")

    async def open_session(self, spec: SessionSpec) -> SessionHandle:
        self.run_ids.append(spec.run_id)
        return SessionHandle(session_id=uuid.uuid4().hex, harness=self._name)

    async def run_turn(
        self, session: SessionHandle, turn: TurnInput, tools: list[ToolSpec], skills: list[SkillDir], hooks: HookSet
    ) -> AsyncIterator[Event]:
        self.seen.append(json.loads(json.dumps(turn.messages)))
        for ev in self._turns.pop(0):
            if isinstance(ev, Exception):
                raise ev
            yield ev

    async def interrupt(self, session: SessionHandle, run_id: str) -> None:
        self.interrupts.append(run_id)

    async def resume(self, spec: SessionSpec, events: list[Event]) -> SessionHandle:
        return await self.open_session(spec)


def _events(resp: Any) -> list[dict[str, Any]]:
    return [json.loads(line[5:].strip()) for line in resp.iter_lines() if line.startswith("data:")]


def _publish(client: Any, name: str, harness: ScriptedHarness, config: dict[str, Any] | None = None) -> None:
    registry.register(name, harness)
    body: dict[str, Any] = {"agent_id": name, "version": "1", "harness": name, "system_prompt": "x"}
    if config is not None:
        body["config"] = config
    r = client.post("/admin/agents", json=body)
    assert r.status_code == 201, r.text
    assert client.post(f"/admin/agents/{name}/versions/1/publish", json={"eval_passed": True}).status_code == 200


def _run_with(
    client: Any, name: str, turns: list[list[Event | Exception]], config: dict[str, Any] | None = None
) -> tuple[list[dict[str, Any]], str, ScriptedHarness]:
    harness = ScriptedHarness(name, turns)
    _publish(client, name, harness, config)
    with client.stream("POST", "/v1/messages", json={"agent_id": name, "messages": [{"role": "user", "content": "hi"}]}) as resp:
        assert resp.status_code == 200
        evs = _events(resp)
    run_id = evs[-1]["run_id"]
    return evs, run_id, harness


def _log(client: Any, run_id: str) -> list[dict[str, Any]]:
    return list(client.get(f"/v1/runs/{run_id}/events").json())


@pytest.mark.parametrize(
    ("stop", "expect_state", "expect_stop"),
    [("error", "failed", "error"), ("cancelled", "canceled", "cancelled"), ("max_tokens", "completed", "max_tokens")],
)
def test_harness_stop_reason_maps_to_run_terminal_state(client, stop: str, expect_state: str, expect_stop: str):
    """harness 以 error 收尾的 Run 必须是 failed，而不是 completed；cancelled/max_tokens 也如实落终态。"""
    name = f"scripted-{stop}-{uuid.uuid4().hex[:6]}"
    turn: list[Event | Exception] = []
    if stop == "error":
        turn.append(ErrorEvent(code="LLM_ERROR", message="HTTP 502 from gateway", retryable=True))
    else:
        turn.append(TextEvent(delta="部分回答"))
    turn += [UsageEvent(input_tokens=3, output_tokens=2, model="scripted"), DoneEvent(run_id="ignored", stop_reason=stop)]  # type: ignore[arg-type]
    evs, run_id, _ = _run_with(client, name, [turn])

    assert evs[-1]["type"] == "done" and evs[-1]["stop_reason"] == expect_stop
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == expect_state
    types = [e["type"] for e in client.get(f"/v1/runs/{run_id}/events").json()]
    if stop == "error":
        assert "run.error" in types, types
        assert next(e for e in evs if e["type"] == "error")["code"] == "LLM_ERROR"
    else:
        assert "run.done" in types, types


@pytest.mark.parametrize("ending", ["end_turn", "awaiting"])
def test_drive_end_interrupts_harness_session(client, ending: str):
    """drive 结束（正常完成 / 挂起等待审批）后都会调用 harness.interrupt 关闭会话，并注销活动 Run 登记。"""
    name = f"scripted-close-{ending}-{uuid.uuid4().hex[:6]}"
    if ending == "end_turn":
        turns: list[list[Event | Exception]] = [[TextEvent(delta="好"), UsageEvent(model="scripted"), DoneEvent(run_id="x")]]
    else:
        call = ToolUseEvent(call_id="c-" + uuid.uuid4().hex[:8], tool_name="ticket.close_batch", input={"ids": ["T-1"]})
        turns = [[call, DoneEvent(run_id="x", stop_reason="tool_use")]]
    evs, run_id, harness = _run_with(client, name, turns, {"tools": ["ticket.close_batch"]})
    assert evs[-1]["stop_reason"] == ending
    assert harness.interrupts == [run_id]
    assert active.get(run_id) is None


class BlockingHarness(ScriptedHarness):
    """在 run_turn 中阻塞等待、直到被 interrupt 才返回的 harness（返回时不产出 done，由 runner 判定终态）。"""

    def __init__(self, name: str) -> None:
        super().__init__(name, [])
        self.entered = threading.Event()  # 测试线程据此得知 run_turn 已在等待
        self._release: asyncio.Event | None = None

    async def run_turn(
        self, session: SessionHandle, turn: TurnInput, tools: list[ToolSpec], skills: list[SkillDir], hooks: HookSet
    ) -> AsyncIterator[Event]:
        self._release = asyncio.Event()
        self.entered.set()
        await self._release.wait()
        return
        yield  # 使本方法成为异步生成器

    async def interrupt(self, session: SessionHandle, run_id: str) -> None:
        await super().interrupt(session, run_id)
        if self._release is not None:
            self._release.set()


def test_cancel_interrupts_running_harness(client):
    """运行中的 Run 被取消：取消 API 中断阻塞在 run_turn 中的 harness，runner 发现 Run 已是 canceled，
    流以 done(cancelled) 正常结束（无 500 / ValueError），Run 终态 canceled。"""
    name = f"scripted-block-{uuid.uuid4().hex[:6]}"
    harness = BlockingHarness(name)
    _publish(client, name, harness)
    result: dict[str, Any] = {}

    def consume() -> None:
        with client.stream("POST", "/v1/messages", json={"agent_id": name, "messages": [{"role": "user", "content": "hi"}]}) as resp:
            result["status"] = resp.status_code
            result["events"] = _events(resp)

    t = threading.Thread(target=consume)
    t.start()
    assert harness.entered.wait(10), "run_turn 未被调用"
    (run_id,) = harness.run_ids
    assert active.get(run_id) is not None  # 运行中：已登记为活动 Run
    r = client.post(f"/v1/runs/{run_id}/cancel")
    assert r.status_code == 200 and r.json()["state"] == "canceled"
    t.join(10)
    assert not t.is_alive(), "取消后流未结束"

    assert result["status"] == 200
    assert result["events"][-1]["type"] == "done" and result["events"][-1]["stop_reason"] == "cancelled"
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "canceled"
    assert harness.interrupts and set(harness.interrupts) == {run_id}  # 取消 API 中断 + drive 收尾中断
    assert active.get(run_id) is None
    log = _log(client, run_id)
    assert "run.error" not in [e["type"] for e in log]
    assert [e["payload"]["stop_reason"] for e in log if e["type"] == "run.done"] == ["cancelled"]


def test_reasoning_enters_rebuildable_history(client):
    """带思维链的工具调用轮次：第二轮发给 harness 的 tool_use 消息带 reasoning，TOOL_REQUESTED 也记录它，
    不变量校验通过、Run 正常完成。"""
    name = f"scripted-reason-{uuid.uuid4().hex[:6]}"
    call = ToolUseEvent(call_id="c-" + uuid.uuid4().hex[:8], tool_name="echo", input={"text": "x"})
    turns: list[list[Event | Exception]] = [
        [
            ThinkingEvent(delta="先查一下"),
            ThinkingEvent(delta="再回答"),
            call,
            UsageEvent(model="scripted"),
            DoneEvent(run_id="x", stop_reason="tool_use"),
        ],
        [TextEvent(delta="完成"), UsageEvent(model="scripted"), DoneEvent(run_id="x")],
    ]
    evs, run_id, harness = _run_with(client, name, turns, {"tools": ["echo"]})
    assert evs[-1]["stop_reason"] == "end_turn"
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "completed"
    (tool_msg,) = [m for m in harness.seen[1] if "tool_use" in m]
    assert tool_msg["reasoning"] == "先查一下再回答" and tool_msg["tool_use"]["call_id"] == call.call_id
    log = _log(client, run_id)
    requested = next(e for e in log if e["type"] == "tool.requested")
    assert requested["payload"]["reasoning"] == "先查一下再回答"
    assert "INVARIANT_VIOLATION" not in json.dumps(log)


def test_reasoning_absent_without_thinking(client):
    """没有思维链的工具调用轮次：tool_use 消息与 TOOL_REQUESTED 都不带 reasoning 键。"""
    name = f"scripted-noreason-{uuid.uuid4().hex[:6]}"
    call = ToolUseEvent(call_id="c-" + uuid.uuid4().hex[:8], tool_name="echo", input={})
    turns: list[list[Event | Exception]] = [
        [call, DoneEvent(run_id="x", stop_reason="tool_use")],
        [TextEvent(delta="完成"), UsageEvent(model="scripted"), DoneEvent(run_id="x")],
    ]
    evs, run_id, harness = _run_with(client, name, turns, {"tools": ["echo"]})
    assert evs[-1]["stop_reason"] == "end_turn"
    (tool_msg,) = [m for m in harness.seen[1] if "tool_use" in m]
    assert "reasoning" not in tool_msg
    assert "reasoning" not in next(e for e in _log(client, run_id) if e["type"] == "tool.requested")["payload"]


def test_harness_exception_fails_run(client):
    """harness 在 run_turn 中途抛出非预期异常：记 HARNESS_EXCEPTION、Run 迁移 failed，流以 error + done(error) 收尾。"""
    name = f"scripted-raise-{uuid.uuid4().hex[:6]}"
    turns: list[list[Event | Exception]] = [[TextEvent(delta="部分"), RuntimeError("sdk crashed")]]
    evs, run_id, harness = _run_with(client, name, turns)
    assert [e["type"] for e in evs][-2:] == ["error", "done"]
    assert evs[-2]["code"] == "HARNESS_EXCEPTION" and "RuntimeError: sdk crashed" in evs[-2]["message"]
    assert evs[-1]["stop_reason"] == "error"
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "failed"
    errors = [e["payload"] for e in _log(client, run_id) if e["type"] == "run.error"]
    assert errors == [{"code": "HARNESS_EXCEPTION", "message": "RuntimeError: sdk crashed"}]
    assert harness.interrupts == [run_id]


def test_error_stop_without_error_event_logs_harness_error(client):
    """harness 以 done(error) 收尾却没有产出 ErrorEvent：补记 RUN_ERROR(HARNESS_ERROR)，Run 判为 failed。"""
    name = f"scripted-bare-error-{uuid.uuid4().hex[:6]}"
    evs, run_id, _ = _run_with(client, name, [[DoneEvent(run_id="x", stop_reason="error")]])
    assert evs[-1]["stop_reason"] == "error"
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "failed"
    errors = [e["payload"] for e in _log(client, run_id) if e["type"] == "run.error"]
    assert [x["code"] for x in errors] == ["HARNESS_ERROR"]

"""S5 WP-A 污点统一（D88/D89）与 credential_subject（D87）。

- `taint_of(status, data_class)`：机密工具的 ok 与 error 结果都计污点，denied 不计；工具代理实时传播与 runner 续跑重建共用；
- 机密工具的 error 结果同样加污点，且挂起审批前后（实时 / 重放）一致；
- `create_run(attachments=)`：附件写入 RUN_CREATED，并据此写 inherited_taint（任一 confidential / 分级缺失即 confidential），
  新 Run 首次驱动即带该污点；attachments 为 None 时不写这两个键；
- `drive(credential_subject=)`：与 Run 发起主体不同时记入 TOOL_EXECUTING 与 tool.decision 审计，相同或 None 时不写。
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select

from ai_contract import (
    DoneEvent,
    Event,
    HarnessCapabilities,
    HookSet,
    RunEvent,
    SessionHandle,
    SessionSpec,
    SkillDir,
    TextEvent,
    ToolSpec,
    ToolUseEvent,
    TurnInput,
    UsageEvent,
)
from runtime.harness import registry
from runtime.loop import runner, toolbroker
from runtime.loop.toolbroker import taint_of
from runtime.state.db import AgentVersion, Audit, SessionLocal
from runtime.state.eventlog import EventLog


class ScriptedHarness:
    """按脚本产出事件的平台循环 harness（每次 run_turn 弹出一段脚本）。"""

    def __init__(self, name: str, turns: list[list[Event]]) -> None:
        self._name = name
        self._turns = turns

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(name=self._name, version="test", transport="in-process", loop_owner="platform")

    async def open_session(self, spec: SessionSpec) -> SessionHandle:
        return SessionHandle(session_id=uuid.uuid4().hex, harness=self._name)

    async def resume(self, spec: SessionSpec, events: list[Event]) -> SessionHandle:
        return await self.open_session(spec)

    async def run_turn(
        self, session: SessionHandle, turn: TurnInput, tools: list[ToolSpec], skills: list[SkillDir], hooks: HookSet
    ) -> AsyncIterator[Event]:
        for ev in self._turns.pop(0):
            yield ev

    async def interrupt(self, session: SessionHandle, run_id: str) -> None:
        return None


def _tool_turn(call_id: str, tool: str, inp: dict[str, Any]) -> list[Event]:
    return [ToolUseEvent(call_id=call_id, tool_name=tool, input=inp), UsageEvent(model="s"), DoneEvent(run_id="x", stop_reason="tool_use")]


def _text_turn() -> list[Event]:
    return [TextEvent(delta="好"), UsageEvent(model="s"), DoneEvent(run_id="x")]


def _publish(client: Any, turns: list[list[Event]], tools: list[str]) -> str:
    name = f"s5t-{uuid.uuid4().hex[:8]}"
    registry.register(name, ScriptedHarness(name, turns))
    r = client.post("/admin/agents", json={"agent_id": name, "version": "1", "harness": name, "config": {"tools": tools}})
    assert r.status_code == 201, r.text
    assert client.post(f"/admin/agents/{name}/versions/1/publish", json={"eval_passed": True}).status_code == 200
    return name


def _sse(resp: Any) -> list[dict[str, Any]]:
    return [json.loads(line[5:].strip()) for line in resp.iter_lines() if line.startswith("data:")]


def _log(client: Any, run_id: str) -> list[RunEvent]:
    return [
        RunEvent(run_id=run_id, seq=e["seq"], type=e["type"], payload=e["payload"]) for e in client.get(f"/v1/runs/{run_id}/events").json()
    ]


def _block_confidential(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型别名不在机密允许清单内：带 confidential 污点的 Run 下一轮被 DATA_CLASS_MODEL_BLOCKED 拦截。"""
    monkeypatch.setattr(runner, "settings", dataclasses.replace(runner.settings, confidential_model_aliases=()))


@pytest.fixture
def confidential_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """data.query（confidential）执行返回业务错误结果（status=error）。"""
    real = toolbroker.execute_tool

    async def fake(tool: ToolSpec, tool_input: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        if tool.name == "data.query":
            return {"error": "合同 C-1 金额 120 万：无权查看", "status_code": 403}
        return await real(tool, tool_input, *args, **kwargs)

    monkeypatch.setattr(toolbroker, "execute_tool", fake)


def test_taint_of_counts_ok_and_error_only():
    assert taint_of("ok", "confidential") == {"confidential"}
    assert taint_of("error", "confidential") == {"confidential"}
    assert taint_of("denied", "confidential") == set()
    assert taint_of("ok", "internal") == set() and taint_of("error", None) == set()


def test_confidential_error_result_taints_live(client, monkeypatch, confidential_error):
    """机密工具返回 error 结果同样带污点（实时）：下一轮发给未授权模型前被拦截。"""
    _block_confidential(monkeypatch)
    name = _publish(client, [_tool_turn("c-err", "data.query", {}), _text_turn()], ["data.query"])
    with client.stream("POST", "/v1/messages", json={"agent_id": name, "messages": [{"role": "user", "content": "q"}]}) as r:
        evs = _sse(r)
        run_id = r.headers["x-run-id"]
    assert next(e for e in evs if e["type"] == "tool_result")["status"] == "error"
    assert next(e for e in evs if e["type"] == "error")["code"] == "DATA_CLASS_MODEL_BLOCKED"
    assert runner._replayed_taint(_log(client, run_id), {}) == {"confidential"}


def test_confidential_error_taint_consistent_across_resume(client, monkeypatch, confidential_error):
    """机密 error 结果之后挂起审批：重放重建出同样的污点，续跑后的模型轮次同样被拦截（续跑前后一致）。"""
    turns = [_tool_turn("c-q", "data.query", {}), _tool_turn("c-close", "ticket.close_batch", {"ids": ["T-1"]}), _text_turn()]
    name = _publish(client, turns, ["data.query", "ticket.close_batch"])
    msgs = [{"role": "user", "content": "q"}]
    with client.stream("POST", "/v1/messages", json={"agent_id": name, "messages": msgs}) as r:
        evs = _sse(r)
        run_id = r.headers["x-run-id"]
    assert evs[-1]["stop_reason"] == "awaiting"
    assert runner._replayed_taint(_log(client, run_id), {}) == {"confidential"}
    card = next(e for e in evs if e["type"] == "card")
    approve = {"call_id": "c-close", "params_hash": card["payload"]["params_hash"]}
    assert client.post(f"/v1/runs/{run_id}/approve", json=approve).status_code == 200
    _block_confidential(monkeypatch)
    with client.stream("POST", f"/v1/runs/{run_id}/resume", json={"messages": msgs}) as r:
        evs2 = _sse(r)
    # S7：审批后 /resume 尾随事件日志（日志行）
    assert next(e for e in evs2 if e["type"] == "tool.result")["payload"]["status"] == "ok"  # 挂起的调用照常补完
    assert next(e for e in evs2 if e["type"] == "run.error")["payload"]["code"] == "DATA_CLASS_MODEL_BLOCKED"


def test_replayed_taint_ignores_denied_and_reads_inherited():
    def ev(seq: int, typ: str, payload: dict[str, Any]) -> RunEvent:
        return RunEvent(run_id="r", seq=seq, type=typ, payload=payload)

    denied = [
        ev(1, "run.created", {}),
        ev(2, "tool.result", {"call_id": "a", "status": "denied", "output": "x", "data_class": "confidential"}),
    ]
    assert runner._replayed_taint(denied, {}) == set()
    inherited = [ev(1, "run.created", {"attachments": [], "inherited_taint": "confidential"})]
    assert runner._replayed_taint(inherited, {}) == {"confidential"}


async def _agent(agent_id: str) -> AgentVersion:
    async with SessionLocal() as s:
        q = select(AgentVersion).where(AgentVersion.agent_id == agent_id, AgentVersion.version == "1")
        av = (await s.execute(q)).scalars().one()
        s.expunge(av)
        return av


async def _events(run_id: str) -> list[RunEvent]:
    async with SessionLocal() as s:
        return await EventLog(s).list(run_id)


@pytest.mark.parametrize(
    ("attachments", "inherited"),
    [
        ([{"artifact_id": "a1", "source_run_id": "run_x", "data_class": "internal"}], None),
        (
            [
                {"artifact_id": "a1", "source_run_id": "run_x", "data_class": "public"},
                {"artifact_id": "a2", "source_run_id": "run_y", "data_class": "confidential"},
            ],
            "confidential",
        ),
        ([{"artifact_id": "a3", "source_run_id": "run_z"}], "confidential"),  # 分级缺失：失败关闭
    ],
)
async def test_create_run_writes_attachments_and_inherited_taint(client, attachments: list[dict[str, Any]], inherited: str | None):
    agent = await _agent("demo")
    run_id = await runner.create_run(agent, "human:alice", None, attachments=attachments)
    created = (await _events(run_id))[0].payload
    assert created["attachments"] == attachments and created["inherited_taint"] == inherited


async def test_create_run_without_attachments_keeps_payload(client):
    agent = await _agent("demo")
    run_id = await runner.create_run(agent, "human:alice", None)
    assert set((await _events(run_id))[0].payload) == {"agent_id", "agent_version", "model_alias", "subject", "actor", "rollout"}


async def test_inherited_taint_applies_from_first_drive(client, monkeypatch):
    """带机密附件的新 Run：首次驱动即带污点，模型不在机密允许清单内时被 DATA_CLASS_MODEL_BLOCKED 拦截。"""
    _block_confidential(monkeypatch)
    name = _publish(client, [_text_turn()], ["echo"])
    agent = await _agent(name)
    att = [{"artifact_id": "a9", "source_run_id": "run_src", "data_class": "confidential"}]
    run_id = await runner.create_run(agent, "human:alice", None, attachments=att)
    evs = [ev async for ev in runner.drive(run_id, agent, "human:alice", [{"role": "user", "content": "看附件"}])]
    assert any(getattr(e, "code", None) == "DATA_CLASS_MODEL_BLOCKED" for e in evs)


@pytest.mark.parametrize(("cred_subject", "recorded"), [("human:approver", True), ("human:alice", False), (None, False)])
async def test_credential_subject_recorded_only_when_foreign(client, cred_subject: str | None, recorded: bool):
    name = _publish(client, [_tool_turn("c-cs", "echo", {"text": "x"}), _text_turn()], ["echo"])
    agent = await _agent(name)
    run_id = await runner.create_run(agent, "human:alice", None)
    msgs = [{"role": "user", "content": "hi"}]
    _ = [ev async for ev in runner.drive(run_id, agent, "human:alice", msgs, credential="tok", credential_subject=cred_subject)]
    executing = next(e.payload for e in await _events(run_id) if e.type == "tool.executing")
    async with SessionLocal() as s:
        rows = (await s.execute(select(Audit).where(Audit.action == "tool.decision"))).scalars().all()
    detail = next(r.detail for r in rows if (r.detail or {}).get("run_id") == run_id)
    if recorded:
        assert executing["credential_subject"] == cred_subject and detail["credential_subject"] == cred_subject
    else:
        assert "credential_subject" not in executing and "credential_subject" not in detail

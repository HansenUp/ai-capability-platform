"""S9 并行工具调用（Contract V1.4，D149）。

- 一轮多个工具调用作为一批：TOOL_REQUESTED 带 batch{id,index,size}；只读并发（≤ PARALLEL_TOOL_MAX）、写按 index 串行；
- 同批待审批合成一张影响预览卡（calls[]）；审批接口兼容单个 call_id 与批量 calls[]，全部批准后才回 working；
- 遇结果未知即停，处置后续跑同批剩余调用；接管时只读孤儿 EXECUTING 直接重执行、写走对账（对账卡列出全部未知调用）；
- 重放同形：每次 MODEL_REQUEST 的 messages_hash 都能从事件日志重建，发给模型的历史中同批调用为一条 assistant 消息；
- litellm 适配器：默认请求带 parallel_tool_calls=false 且只交回第一个调用；会话开启并行后交回全部；发布闸门。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest
from test_harness_litellm import ALIAS, Gateway, delta, make_harness, sse, text_turn, usage

from ai_contract import CardEvent, CardType, DoneEvent, RunEvent, RunEventType, RunState, ToolUseEvent, TurnInput
from runtime.assemble.assembler import LOCAL_TOOLS
from runtime.harness.mock import openai_to_history
from runtime.llm import set_transport_for_tests, to_openai_messages, to_openai_tool_name
from runtime.loop import runner, toolbroker, user_input
from runtime.run import invariant
from runtime.state import db
from runtime.state.db import AgentVersion, RunInput
from runtime.state.eventlog import EventLog

SUBJECT = "u-s9-par"


def multi_turn(calls: list[tuple[str, str, dict[str, Any]]], reasoning: str = "这几件事可以一起做") -> bytes:
    """一轮"推理 → 多个工具调用"的模型响应（同一条消息里的多个 tool_calls）。"""
    tool_calls = [
        {"index": i, "id": cid, "type": "function", "function": {"name": to_openai_tool_name(name), "arguments": json.dumps(args)}}
        for i, (cid, name, args) in enumerate(calls)
    ]
    return sse(delta(reasoning_content=reasoning), delta(tool_calls=tool_calls), delta(finish="tool_calls"), usage())


class Exec:
    """替身 execute_tool：记录执行顺序与并发度；dry-run 返回影响预览；fail 中的调用抛异常（模拟结果未知）。"""

    def __init__(self, delay: float = 0.05, fail: set[str] | None = None) -> None:
        self.delay, self.fail = delay, fail or set()
        self.inflight = self.peak = 0
        self.trace: list[tuple[str, str]] = []

    async def __call__(self, tool: Any, tool_input: dict[str, Any], dry_run: bool = False, **kw: Any) -> Any:
        if dry_run:
            return {"impact_preview": {"tickets": len(tool_input.get("ids", []))}}
        call_id = str(kw.get("call_id"))
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        self.trace.append(("start", call_id))
        try:
            await asyncio.sleep(self.delay)
            if call_id in self.fail:
                raise RuntimeError("worker died mid-call")
            return {"done": call_id}
        finally:
            self.inflight -= 1
            self.trace.append(("end", call_id))

    def started(self) -> list[str]:
        return [c for kind, c in self.trace if kind == "start"]


def _events(resp: Any) -> list[dict[str, Any]]:
    return [json.loads(line[5:].strip()) for line in resp.iter_lines() if line.startswith("data:")]


def _publish(client: Any, name: str, tools: list[str], **cfg: Any) -> None:
    body = {
        "agent_id": name,
        "version": "1",
        "harness": "litellm",
        "model_alias": ALIAS,
        "system_prompt": "你是 S9 并行测试助手。",
        "config": {"tools": tools, "parallel_tool_calls": True, **cfg},
    }
    assert client.post("/admin/agents", json=body).status_code == 201
    r = client.post(f"/admin/agents/{name}/versions/1/publish", json={"eval_passed": True})
    assert r.status_code == 200, r.text


def _log(client: Any, run_id: str) -> list[RunEvent]:
    return [RunEvent.model_validate(e) for e in client.get(f"/v1/runs/{run_id}/events").json()]


def assert_replay_same_shape(log: list[RunEvent], initial: list[dict[str, Any]]) -> None:
    """每次 MODEL_REQUEST 记录的 messages_hash 都等于"该请求之前的事件日志"重建出的历史指纹（重放与实时同形）。"""
    requests = [i for i, e in enumerate(log) if e.type == RunEventType.MODEL_REQUEST]
    assert requests
    for i in requests:
        assert log[i].payload["messages_hash"] == invariant.message_hash(invariant.rebuild_messages(log[:i], initial)), i


def test_reads_run_concurrently_writes_serially_and_replay_has_same_shape(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    ex = Exec()
    monkeypatch.setattr(toolbroker, "execute_tool", ex)
    calls = [
        ("p1", "ticket.search", {"q": "a"}),
        ("p2", "mail.send", {"to": "x@example.com"}),
        ("p3", "echo", {"text": "b"}),
        ("p4", "ticket.search", {"q": "c"}),
    ]
    gw = Gateway(multi_turn(calls), text_turn("都完成了"))
    set_transport_for_tests(httpx.MockTransport(gw), max_retries=0, backoff_s=0)
    try:
        name = f"s9-par-{uuid.uuid4().hex[:6]}"
        _publish(client, name, ["echo", "ticket.search", "mail.send"])
        initial = [{"role": "user", "content": "查两次工单、回显并发邮件"}]
        with client.stream("POST", "/v1/messages", json={"agent_id": name, "messages": initial}) as resp:
            run_id = resp.headers["x-run-id"]
            evs = _events(resp)
    finally:
        set_transport_for_tests(None)

    assert evs[-1]["type"] == "done" and evs[-1]["stop_reason"] == "end_turn", evs
    assert [e["call_id"] for e in evs if e["type"] == "tool_use"] == ["p1", "p2", "p3", "p4"]
    assert sorted(e["call_id"] for e in evs if e["type"] == "tool_result") == ["p1", "p2", "p3", "p4"]
    # 只读三个并发执行（PARALLEL_TOOL_MAX=4），写在全部只读结束之后才开始、且只执行一次
    assert ex.peak == 3
    reads_done = max(i for i, (k, c) in enumerate(ex.trace) if k == "end" and c in ("p1", "p3", "p4"))
    assert ex.trace.index(("start", "p2")) > reads_done and ex.started().count("p2") == 1
    log = _log(client, run_id)
    requested = [e for e in log if e.type == RunEventType.TOOL_REQUESTED]
    batch_ids = {e.payload["batch"]["id"] for e in requested}
    assert len(batch_ids) == 1 and [e.payload["batch"]["index"] for e in requested] == [0, 1, 2, 3]
    assert all(e.payload["batch"]["size"] == 4 for e in requested)
    assert requested[0].payload.get("reasoning") == "这几件事可以一起做" and "reasoning" not in requested[1].payload
    seqs = [e.seq for e in requested]
    assert seqs == list(range(seqs[0], seqs[0] + 4))  # 先按 index 写全部请求，再逐条过闸口
    decisions = [e.payload["call_id"] for e in log if e.type == RunEventType.TOOL_DECISION]
    assert decisions == ["p1", "p2", "p3", "p4"]  # 逐条过闸口，顺序与 index 一致
    assert gw.requests[0]["parallel_tool_calls"] is True
    # 发给模型的第二轮历史：同批为一条 assistant 消息（4 个 tool_calls + reasoning_content），结果按 index 排列
    second = gw.requests[1]["messages"]
    batch_msg = next(m for m in second if m.get("tool_calls"))
    assert [c["id"] for c in batch_msg["tool_calls"]] == ["p1", "p2", "p3", "p4"]
    assert batch_msg["reasoning_content"] == "这几件事可以一起做"
    assert [m["tool_call_id"] for m in second if m["role"] == "tool"] == ["p1", "p2", "p3", "p4"]
    # 重放同形：从日志重建的历史即模型实际收到的历史，messages_hash 逐次一致
    assert_replay_same_shape(log, initial)
    rebuilt = invariant.rebuild_messages(
        [e for e in log if e.seq < next(x.seq for x in log if x.type == RunEventType.MODEL_REQUEST and x.seq > seqs[0])], initial
    )
    assert to_openai_messages(rebuilt) == second[1:]
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "completed"


def test_batch_approval_single_card_partial_and_bulk_approve(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    ex = Exec(delay=0.01)
    monkeypatch.setattr(toolbroker, "execute_tool", ex)
    calls = [
        ("a1", "echo", {"text": "先读"}),
        ("a2", "ticket.close_batch", {"ids": ["T-1"]}),
        ("a3", "mail.send", {"to": "x@example.com"}),
        ("a4", "ticket.close_batch", {"ids": ["T-2", "T-3"]}),
    ]
    gw = Gateway(multi_turn(calls), text_turn("已关闭并通知"))
    set_transport_for_tests(httpx.MockTransport(gw), max_retries=0, backoff_s=0)
    try:
        name = f"s9-appr-{uuid.uuid4().hex[:6]}"
        _publish(client, name, ["echo", "ticket.close_batch", "mail.send"])
        initial = [{"role": "user", "content": "关两批工单并通知"}]
        with client.stream("POST", "/v1/messages", json={"agent_id": name, "messages": initial}) as resp:
            run_id = resp.headers["x-run-id"]
            evs = _events(resp)
        # 一张卡：两个待审批调用（calls[]），首个待批调用的字段保持单卡形状；只读已执行，排在待批之后的写尚未执行
        cards = [e for e in evs if e["type"] == "card"]
        assert len(cards) == 1 and cards[0]["card_type"] == "impact_preview"
        card = cards[0]["payload"]
        assert card["call_id"] == "a2" and [c["call_id"] for c in card["calls"]] == ["a2", "a4"]
        assert card["calls"][1]["preview"] == {"impact_preview": {"tickets": 2}} and all(c["params_hash"] for c in card["calls"])
        assert evs[-1]["stop_reason"] == "awaiting" and ex.started() == ["a1"]
        assert client.get(f"/v1/runs/{run_id}").json()["state"] == "auth-required"
        # 单个批准：同批还有待批调用，保持 auth-required
        first = {"call_id": "a2", "params_hash": card["calls"][0]["params_hash"]}
        r = client.post(f"/v1/runs/{run_id}/approve", json=first)
        assert r.status_code == 200 and r.json()["state"] == "auth-required"
        bad = client.post(f"/v1/runs/{run_id}/approve", json={"calls": [{"call_id": "a4", "params_hash": "0" * 16}]})
        assert bad.status_code == 409  # 指纹不符
        assert client.post(f"/v1/runs/{run_id}/approve", json={"call_id": "a4"}).status_code == 422  # 单个写法缺指纹
        rest = {"calls": [{"call_id": "a4", "params_hash": card["calls"][1]["params_hash"]}]}
        r = client.post(f"/v1/runs/{run_id}/approve", json=rest)
        assert r.status_code == 200 and r.json()["state"] == "working"
        with client.stream("POST", f"/v1/runs/{run_id}/resume", json={"messages": initial}) as resp:
            resumed = _events(resp)
    finally:
        set_transport_for_tests(None)
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "completed", resumed
    assert ex.started() == ["a1", "a2", "a3", "a4"]  # 写按 index 串行：批准的两个高风险写与中间的低风险写依序执行
    log = _log(client, run_id)
    assert [e.payload["call_id"] for e in log if e.type == RunEventType.TOOL_APPROVED] == ["a2", "a4"]
    assert_replay_same_shape(log, initial)


def test_unknown_write_stops_batch_then_reconcile_resumes_rest(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    ex = Exec(delay=0.01, fail={"u1"})
    monkeypatch.setattr(toolbroker, "execute_tool", ex)
    calls = [("u1", "mail.send", {"to": "a@example.com"}), ("u2", "mail.send", {"to": "b@example.com"}), ("u3", "echo", {"text": "r"})]
    gw = Gateway(multi_turn(calls), text_turn("处理完毕"))
    set_transport_for_tests(httpx.MockTransport(gw), max_retries=0, backoff_s=0)
    try:
        name = f"s9-unk-{uuid.uuid4().hex[:6]}"
        _publish(client, name, ["echo", "mail.send"])
        initial = [{"role": "user", "content": "发两封邮件"}]
        with client.stream("POST", "/v1/messages", json={"agent_id": name, "messages": initial}) as resp:
            run_id = resp.headers["x-run-id"]
            evs = _events(resp)
        card = next(e for e in evs if e["type"] == "card")
        assert card["card_type"] == "reconciliation_required" and card["payload"]["call_id"] == "u1"
        assert ex.started() == ["u3", "u1"]  # 只读先执行；第一个写结果未知即停，第二个写不执行
        log = _log(client, run_id)
        assert [e.payload["call_id"] for e in log if e.type == RunEventType.TOOL_EXECUTING] == ["u3", "u1"]
        assert client.get(f"/v1/runs/{run_id}").json()["state"] == "input-required"
        r = client.post(f"/v1/runs/{run_id}/input", json={"kind": "reconcile", "call_id": "u1", "action": "skip"})
        assert r.status_code == 200 and r.json()["state"] == "working"
        ex.fail.clear()
        with client.stream("POST", f"/v1/runs/{run_id}/resume", json={"messages": initial}) as resp:
            _events(resp)
    finally:
        set_transport_for_tests(None)
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "completed"
    assert ex.started() == ["u3", "u1", "u2"]  # 处置后续跑同批剩余的写，u1 不重做
    log = _log(client, run_id)
    assert_replay_same_shape(log, initial)
    second = gw.requests[1]["messages"]
    assert [m["tool_call_id"] for m in second if m["role"] == "tool"] == ["u1", "u2", "u3"]


# ---------------- 接管：只读孤儿重执行、写走对账（对账卡列出全部未知调用） ----------------


async def _agent(config: dict[str, Any]) -> AgentVersion:
    av = AgentVersion(agent_id=f"s9p-{uuid.uuid4().hex[:8]}", version="1", status="published", system_prompt="x", config=config)
    async with db.SessionLocal() as s:
        s.add(av)
        await s.commit()
    return av


async def test_takeover_reexecutes_orphan_reads_and_reconciles_all_unknown_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    ex = Exec(delay=0.0)
    monkeypatch.setattr(toolbroker, "execute_tool", ex)
    agent = await _agent({"tools": ["echo", "mail.send"]})
    msgs = [{"role": "user", "content": "接管场景"}]
    rid = await runner.create_run(agent, SUBJECT, None, initial_messages=msgs)
    bid = "b-takeover"
    batch = [
        ("r1", "echo", "read", {"text": "x"}),
        ("w1", "mail.send", "write_low", {"to": "a"}),
        ("w2", "mail.send", "write_low", {"to": "b"}),
    ]
    async with db.SessionLocal() as s:
        s.add(RunInput(run_id=rid, messages=msgs, fingerprint=invariant.message_hash(msgs)))
        log = EventLog(s)
        await log.transition(rid, RunState.WORKING, "start")
        await log.append(
            rid, RunEventType.MODEL_REQUEST, {"model_alias": "m", "messages_hash": invariant.message_hash(msgs), "n_messages": 1}
        )
        await log.append(rid, RunEventType.MODEL_RESPONSE, {"text": "", "usage": {}})
        for i, (cid, tool, risk, args) in enumerate(batch):
            payload = {"call_id": cid, "tool": tool, "input": args, "risk_level": risk, "batch": {"id": bid, "index": i, "size": 3}}
            await log.append(rid, RunEventType.TOOL_REQUESTED, payload)
        for cid, tool, _risk, _args in batch:  # 失租的旧 Worker：三个调用都写了 TOOL_EXECUTING 后进程消失
            await log.append(rid, RunEventType.TOOL_DECISION, {"call_id": cid, "decision": "allow", "reason": "ok"})
            op = None if cid == "r1" else f"{rid}:{cid}:op"
            await log.append(rid, RunEventType.TOOL_EXECUTING, {"call_id": cid, "tool": tool, "operation_id": op, "params_hash": "h"})
        await s.commit()

    evs = [ev async for ev in runner.drive_durable(rid)]
    card = next(e for e in evs if isinstance(e, CardEvent))
    assert card.card_type == CardType.RECONCILIATION_REQUIRED
    assert [c["call_id"] for c in card.payload["calls"]] == ["w1", "w2"] and card.payload["call_id"] == "w2"  # 只读孤儿不在卡上
    assert isinstance(evs[-1], DoneEvent) and evs[-1].stop_reason == "awaiting" and ex.started() == []
    async with db.SessionLocal() as s:
        prior = await EventLog(s).list(rid)
    (err,) = [e for e in prior if e.type == RunEventType.RUN_ERROR]
    assert err.payload["code"] == "UNKNOWN_TOOL_RESULT" and [c["call_id"] for c in err.payload["calls"]] == ["w1", "w2"]

    out = await user_input.provide_input(rid, SUBJECT, "reconcile", "w1", action="done")
    assert out["state"] == "input-required"  # 还有 w2 未处置
    out = await user_input.provide_input(rid, SUBJECT, "reconcile", "w2", action="skip")
    assert out["state"] == "working"

    evs = [ev async for ev in runner.drive_durable(rid)]
    assert isinstance(evs[-1], DoneEvent) and evs[-1].stop_reason == "end_turn"
    assert ex.started() == ["r1"]  # 只读孤儿直接重执行；写不重做
    async with db.SessionLocal() as s:
        log_after = await EventLog(s).list(rid)
        run = await s.get(db.Run, rid)
    assert run is not None and run.state == RunState.COMPLETED
    results = [e.payload["call_id"] for e in log_after if e.type == RunEventType.TOOL_RESULT]
    assert sorted(results) == ["r1", "w1", "w2"]
    assert_replay_same_shape(log_after, msgs)
    rebuilt = invariant.rebuild_messages(log_after, msgs)
    assert rebuilt[1]["tool_uses"] == [{"call_id": c, "name": t, "input": a} for c, t, _r, a in batch]
    assert [m["call_id"] for m in rebuilt[2:5]] == ["r1", "w1", "w2"]  # 结果按 index 排列，与落日志先后无关


# ---------------- 重建与消息翻译 ----------------


def _ev(seq: int, type_: RunEventType, **payload: Any) -> RunEvent:
    return RunEvent(run_id="r", seq=seq, type=type_, payload=payload)


def test_rebuild_orders_batch_results_by_index_and_keeps_single_shape() -> None:
    b = {"id": "b1", "size": 3}
    log = [
        _ev(1, RunEventType.MODEL_RESPONSE, text="我同时查三处"),
        _ev(2, RunEventType.TOOL_REQUESTED, call_id="c0", tool="t", input={"i": 0}, batch=b | {"index": 0}, reasoning="想"),
        _ev(3, RunEventType.TOOL_REQUESTED, call_id="c1", tool="t", input={"i": 1}, batch=b | {"index": 1}),
        _ev(4, RunEventType.TOOL_REQUESTED, call_id="c2", tool="t", input={"i": 2}, batch=b | {"index": 2}),
        _ev(5, RunEventType.TOOL_RESULT, call_id="c2", status="ok", output="r2"),
        _ev(6, RunEventType.TOOL_RESULT, call_id="c0", status="ok", output="r0"),
        _ev(7, RunEventType.TOOL_RESULT, call_id="c1", status="ok", output="r1"),
        _ev(8, RunEventType.TOOL_REQUESTED, call_id="s1", tool="t", input={}),
        _ev(9, RunEventType.TOOL_RESULT, call_id="s1", status="ok", output="single"),
    ]
    msgs = invariant.rebuild_messages(log, [{"role": "user", "content": "q"}])
    assert msgs == [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "我同时查三处"},
        {
            "role": "assistant",
            "tool_uses": [{"call_id": f"c{i}", "name": "t", "input": {"i": i}} for i in range(3)],
            "reasoning": "想",
        },
        {"role": "tool", "call_id": "c0", "content": "r0"},
        {"role": "tool", "call_id": "c1", "content": "r1"},
        {"role": "tool", "call_id": "c2", "content": "r2"},
        {"role": "assistant", "tool_use": {"call_id": "s1", "name": "t", "input": {}}},
        {"role": "tool", "call_id": "s1", "content": "single"},
    ]
    openai = to_openai_messages(msgs)
    assert openai[1]["content"] == "我同时查三处" and [c["id"] for c in openai[1]["tool_calls"]] == ["c0", "c1", "c2"]
    assert openai[1]["reasoning_content"] == "想"
    assert openai_to_history([{"role": "system", "content": "s"}, *openai]) == msgs  # 探针的逆变换与重建同形


async def test_litellm_parallel_flag_is_off_by_default_and_per_session() -> None:
    calls = [("x1", "echo", {"text": "a"}), ("x2", "echo", {"text": "b"})]
    gw = Gateway(multi_turn(calls), multi_turn(calls))
    h = make_harness(gw)
    from test_harness_litellm import spec

    from ai_contract import HookSet

    tools = [LOCAL_TOOLS["echo"]]
    off = await h.open_session(spec())
    evs = [e async for e in h.run_turn(off, TurnInput(messages=[{"role": "user", "content": "q"}]), tools, [], HookSet())]
    assert [e.call_id for e in evs if isinstance(e, ToolUseEvent)] == ["x1"] and gw.requests[0]["parallel_tool_calls"] is False
    assert "disable_fallbacks" not in gw.requests[0]
    on = await h.open_session(spec())
    extra = runner.request_extra(runner.Level.INTERNAL, ALIAS)
    h.configure_session(on, parallel_tool_calls=True, request_extra=extra)
    evs = [e async for e in h.run_turn(on, TurnInput(messages=[{"role": "user", "content": "q"}]), tools, [], HookSet())]
    assert [e.call_id for e in evs if isinstance(e, ToolUseEvent)] == ["x1", "x2"] and evs[-1].stop_reason == "tool_use"
    body = gw.requests[1]
    assert body["parallel_tool_calls"] is True and body["disable_fallbacks"] is True
    assert body["metadata"] == {"data_class": "internal", "mask_input": True, "mask_output": True}
    assert h.capabilities().parallel_tool_calls is True


def test_publish_rejects_parallel_on_unsupported_harness_and_non_bool(client: Any) -> None:
    for name, cfg, fragment in (
        (f"s9-par-mock-{uuid.uuid4().hex[:6]}", {"tools": ["echo"], "parallel_tool_calls": True}, "不支持并行工具调用"),
        (f"s9-par-bad-{uuid.uuid4().hex[:6]}", {"tools": ["echo"], "parallel_tool_calls": "yes"}, "parallel_tool_calls 配置不合规"),
    ):
        assert client.post("/admin/agents", json={"agent_id": name, "version": "1", "config": cfg}).status_code == 201
        r = client.post(f"/admin/agents/{name}/versions/1/publish", json={"eval_passed": True})
        assert r.status_code == 409 and fragment in r.json()["detail"], r.text

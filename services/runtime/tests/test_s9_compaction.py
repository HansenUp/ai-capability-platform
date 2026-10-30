"""S9 DEF-2 上下文压缩事件化（D150）与 S5 #7 非推理摘要别名（D151）。

- 边界：不拆工具调用与结果、不拆同批；取能落进目标预算的最小切点；
- 事件化：history 超预算时在 MODEL_REQUEST 前写 CONTEXT_COMPACTED，重建与实时同形，审批挂起后续跑一致；
- 摘要：truncate 确定性；llm 走 THINKING_SUMMARY_MODEL_ALIAS（发布校验别名存在），矩阵不准许（机密）或失败时退回截断；
- budget.assemble 不再截断 history。
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from test_harness_litellm import ALIAS, delta, sse, text_turn, usage
from test_s9_parallel import assert_replay_same_shape

from ai_contract import RunEvent, RunEventType
from ai_contract.data_class import Level
from runtime.config import settings
from runtime.context import compact
from runtime.llm import set_transport_for_tests, to_openai_tool_name
from runtime.loop import toolbroker
from runtime.run import invariant

LONG = "这是一段很长的背景材料。" * 120  # 约 1440 字符 ≈ 480 token


def _agent(cfg: dict[str, Any] | None = None, harness: str | None = None) -> Any:
    return SimpleNamespace(config=cfg or {}, harness=harness)


def _tool_use(cid: str) -> dict[str, Any]:
    return {"role": "assistant", "tool_use": {"call_id": cid, "name": "echo", "input": {}}}


def _tool(cid: str, content: str = "ok") -> dict[str, Any]:
    return {"role": "tool", "call_id": cid, "content": content}


def test_boundaries_never_split_calls_results_or_batches() -> None:
    history = [
        {"role": "user", "content": "q"},  # 0
        {"role": "assistant", "content": "先查"},  # 1
        _tool_use("a"),  # 2
        _tool("a"),  # 3
        {
            "role": "assistant",
            "tool_uses": [{"call_id": "b", "name": "echo", "input": {}}, {"call_id": "c", "name": "echo", "input": {}}],
        },  # 4
        _tool("b"),  # 5
        _tool("c"),  # 6
        {"role": "assistant", "content": "好的"},  # 7
    ]
    # 切点 k：history[k] 不是工具结果、history[k-1] 不是工具调用消息
    assert compact.boundaries(history) == [1, 2, 4, 7]
    assert compact.choose_boundary(history, 10**6) == 1  # 都放得下时丢得最少
    assert compact.choose_boundary(history, 1) == 7  # 都放不下时取最大的合法切点
    assert compact.choose_boundary([{"role": "user", "content": "x"}], 1) is None


def test_config_validation_and_defaults() -> None:
    conf = compact.config_of(_agent())
    assert (conf.method, conf.history_budget_tokens, conf.summary_model_alias) == (
        "truncate",
        settings.context_history_budget_tokens,
        settings.thinking_summary_model_alias,
    )
    assert settings.thinking_summary_model_alias == "summary-fast-2026-10"
    for bad in (
        {"compaction": "magic"},
        {"history_budget_tokens": 10},
        {"history_budget_tokens": True},
        {"x": 1},
        {"summary_model_alias": ""},
    ):
        with pytest.raises(ValueError):
            compact.config_of(_agent({"context": bad}))
    with pytest.raises(ValueError):
        compact.config_of(_agent({"context": []}))


async def test_truncate_compaction_payload_and_apply() -> None:
    history = [{"role": "user", "content": LONG}, _tool_use("a"), _tool("a", "结果" * 10), {"role": "assistant", "content": "收尾"}]
    agent = _agent({"context": {"history_budget_tokens": 256}})
    plan = await compact.plan_compaction(history, agent, Level.INTERNAL, model_allowed=lambda a, lv: True)
    assert plan is not None and plan.method == "truncate" and plan.upto_index == 1
    p = plan.payload()
    assert set(p) == {"upto_index", "summary", "method", "tokens", "dropped_hmac", "data_class"}
    assert p["data_class"] == "internal" and p["tokens"]["before"] > 256 >= p["tokens"]["after"]
    assert p["dropped_hmac"] == toolbroker.hmac_json(history[:1])  # 与工具代理同一指纹算法（日志不留原文）
    applied = plan.apply(history)
    assert applied[0] == {"role": "system", "content": "[compacted summary] " + plan.summary} and applied[1:] == history[1:]
    # 预算内 / off 不压缩
    assert await compact.plan_compaction(history, _agent(), Level.PUBLIC, model_allowed=lambda a, lv: True) is None
    off = _agent({"context": {"compaction": "off", "history_budget_tokens": 256}})
    assert await compact.plan_compaction(history, off, Level.PUBLIC, model_allowed=lambda a, lv: True) is None


async def test_llm_summary_uses_summary_alias_and_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "用户要背景材料的要点"}, "finish_reason": "stop"}], "usage": {}}
        )

    set_transport_for_tests(httpx.MockTransport(handler), max_retries=0, backoff_s=0)
    try:
        history = [{"role": "user", "content": LONG}, {"role": "assistant", "content": "收到"}]
        agent = _agent({"context": {"compaction": "llm", "history_budget_tokens": 256}})
        extra = {"disable_fallbacks": True}
        plan = await compact.plan_compaction(history, agent, Level.INTERNAL, model_allowed=lambda a, lv: True, request_extra=extra)
        assert plan is not None and plan.method == "llm" and plan.summary == "用户要背景材料的要点" and plan.fallback is None
        record = plan.payload()["model_usage_record"]
        assert record["source"] == "context_compaction"
        assert record["coverage"] == "response_record_only" and record["billing_verified"] is False
        assert record["usage"]["input_tokens"] == 0  # legacy parser default, not verified free usage
        assert seen[0]["model"] == "summary-fast-2026-10" and seen[0]["disable_fallbacks"] is True and not seen[0].get("stream")
        # 机密：矩阵不准许把上下文发给摘要模型 → 不出网，退回截断
        seen.clear()
        plan = await compact.plan_compaction(history, agent, Level.CONFIDENTIAL, model_allowed=lambda a, lv: lv < Level.CONFIDENTIAL)
        assert plan is not None and plan.method == "truncate" and plan.fallback == "data_class" and seen == []
        assert "model_usage_record" not in plan.payload()
        assert plan.payload()["data_class"] == "confidential" and plan.payload()["fallback"] == "data_class"
    finally:
        set_transport_for_tests(None)
    set_transport_for_tests(httpx.MockTransport(lambda r: httpx.Response(503)), max_retries=0, backoff_s=0)
    try:
        plan = await compact.plan_compaction(history, agent, Level.PUBLIC, model_allowed=lambda a, lv: True)
        assert plan is not None and plan.method == "truncate" and plan.fallback == "llm_error"
        assert "model_usage_record" not in plan.payload()
    finally:
        set_transport_for_tests(None)


def test_rebuild_applies_compaction_like_live() -> None:
    initial = [{"role": "user", "content": LONG}]
    log = [
        RunEvent(run_id="r", seq=1, type=RunEventType.TOOL_REQUESTED, payload={"call_id": "a", "tool": "echo", "input": {}}),
        RunEvent(run_id="r", seq=2, type=RunEventType.TOOL_RESULT, payload={"call_id": "a", "status": "ok", "output": "x"}),
        RunEvent(run_id="r", seq=3, type=RunEventType.CONTEXT_COMPACTED, payload={"upto_index": 1, "summary": "摘要"}),
    ]
    rebuilt = invariant.rebuild_messages(log, initial)
    assert rebuilt == [{"role": "system", "content": "[compacted summary] 摘要"}, _tool_use("a"), _tool("a", "x")]


def _turns() -> list[bytes]:
    call = sse(
        delta(content="先回显一下"),
        delta(tool_calls=[{"index": 0, "id": "k1", "type": "function", "function": {"name": "echo", "arguments": '{"text": "x"}'}}]),
        delta(finish="tool_calls"),
        usage(),
    )
    close = sse(
        delta(
            tool_calls=[
                {
                    "index": 0,
                    "id": "k2",
                    "type": "function",
                    "function": {"name": to_openai_tool_name("ticket.close_batch"), "arguments": '{"ids": ["T-9"]}'},
                }
            ]
        ),
        delta(finish="tool_calls"),
        usage(),
    )
    return [call, close, text_turn("已关闭")]


class _Gateway:
    """模拟网关：流式请求依次应答；/model/info 含 Agent 别名与摘要别名（发布时校验）。"""

    def __init__(self, turns: list[bytes], aliases: list[str]) -> None:
        self.turns, self.requests = list(turns), []  # type: ignore[var-annotated]
        self.info = {"data": [{"model_name": a, "litellm_params": {"model": f"x/{a}"}, "model_info": {}} for a in aliases]}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/model/info":
            return httpx.Response(200, json=self.info)
        body = json.loads(req.content)
        self.requests.append(body)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=self.turns.pop(0))


def _events(resp: Any) -> list[dict[str, Any]]:
    return [json.loads(line[5:].strip()) for line in resp.iter_lines() if line.startswith("data:")]


def test_compaction_is_event_sourced_and_resume_after_approval_is_consistent(client: Any) -> None:
    gw = _Gateway(_turns(), [ALIAS])
    set_transport_for_tests(httpx.MockTransport(gw), max_retries=0, backoff_s=0)
    try:
        name = f"s9-cmp-{uuid.uuid4().hex[:6]}"
        cfg = {"tools": ["echo", "ticket.close_batch"], "context": {"history_budget_tokens": 256}}
        body = {"agent_id": name, "version": "1", "harness": "litellm", "model_alias": ALIAS, "system_prompt": "s9", "config": cfg}
        assert client.post("/admin/agents", json=body).status_code == 201
        assert client.post(f"/admin/agents/{name}/versions/1/publish", json={"eval_passed": True}).status_code == 200
        initial = [{"role": "user", "content": LONG}]
        with client.stream("POST", "/v1/messages", json={"agent_id": name, "messages": initial}) as resp:
            run_id = resp.headers["x-run-id"]
            evs = _events(resp)
        card = next(e for e in evs if e["type"] == "card")
        approval = {"call_id": "k2", "params_hash": card["payload"]["params_hash"]}
        assert client.post(f"/v1/runs/{run_id}/approve", json=approval).status_code == 200
        with client.stream("POST", f"/v1/runs/{run_id}/resume", json={"messages": initial}) as resp:
            resumed = _events(resp)
    finally:
        set_transport_for_tests(None)
    assert resumed[-1]["stop_reason"] == "end_turn" and client.get(f"/v1/runs/{run_id}").json()["state"] == "completed"
    log = [RunEvent.model_validate(e) for e in client.get(f"/v1/runs/{run_id}/events").json()]
    compactions = [e for e in log if e.type == RunEventType.CONTEXT_COMPACTED]
    assert compactions and compactions[0].payload["method"] == "truncate" and compactions[0].payload["upto_index"] == 1
    assert compactions[0].payload["data_class"] == "internal"  # 摘要继承 Run 的数据等级（echo 结果为 internal）
    first_compaction = compactions[0].seq
    assert next(e for e in log if e.seq > first_compaction).type == RunEventType.MODEL_REQUEST  # 紧接着才是模型请求
    # CONTEXT_ASSEMBLED 不再截断 history
    assert "history" not in next(e for e in log if e.type == RunEventType.CONTEXT_ASSEMBLED).payload["truncated"]
    # 第二轮起模型看到压缩摘要而不是原文；续跑（审批后重放）得到同一份历史
    second = gw.requests[1]["messages"]
    assert second[1]["role"] == "system" and second[1]["content"].startswith("[compacted summary] ")
    assert all(LONG not in json.dumps(m, ensure_ascii=False) for m in second[1:])
    assert_replay_same_shape(log, initial)


def test_publish_checks_summary_alias_exists(client: Any) -> None:
    for aliases, status in (([ALIAS], 409), ([ALIAS, "summary-fast-2026-10"], 200)):
        gw = _Gateway([], aliases)
        set_transport_for_tests(httpx.MockTransport(gw), max_retries=0, backoff_s=0)
        try:
            name = f"s9-alias-{uuid.uuid4().hex[:6]}"
            cfg = {"tools": ["echo"], "context": {"compaction": "llm"}}
            body = {"agent_id": name, "version": "1", "harness": "litellm", "model_alias": ALIAS, "config": cfg}
            assert client.post("/admin/agents", json=body).status_code == 201
            r = client.post(f"/admin/agents/{name}/versions/1/publish", json={"eval_passed": True})
        finally:
            set_transport_for_tests(None)
        assert r.status_code == status, r.text
        if status == 409:
            assert "summary model alias 'summary-fast-2026-10'" in r.json()["detail"]
    name = f"s9-ctx-bad-{uuid.uuid4().hex[:6]}"
    assert (
        client.post("/admin/agents", json={"agent_id": name, "version": "1", "config": {"context": {"compaction": "x"}}}).status_code == 201
    )
    r = client.post(f"/admin/agents/{name}/versions/1/publish", json={"eval_passed": True})
    assert r.status_code == 409 and "context 配置不合规" in r.json()["detail"]

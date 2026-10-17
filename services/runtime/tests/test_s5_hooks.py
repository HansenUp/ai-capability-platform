"""S5 WP-A Hooks 引擎：平台在 pre_model / post_model / pre_tool / post_tool 四个点统一执行内置 hook。

- 单元：HookChain 四个点 × allow / override / deny / error / timeout 的结论矩阵、白名单过滤、data_class 只升不降、
  配置校验、隐式 thinking_summary、PII 脱敏；
- 集成（经 /v1/messages 跑完整链路，模型由按脚本产出事件的 harness 扮演）：hook 效果写入受影响事件；
  未配置 hooks 时事件 payload 形态与此前一致；挂起调用按 effective_input 续跑且不重跑 hook；
  summary 可见性只下发一条摘要思维事件；传给适配器的 HookSet 只含 internal_hook_points 声明的点。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from ai_contract import (
    DoneEvent,
    Event,
    HarnessCapabilities,
    HookPoint,
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
from runtime.config import settings
from runtime.guard import hooks
from runtime.guard.gate import params_hash
from runtime.harness import registry
from runtime.llm import LLMClient
from runtime.loop import toolbroker

# ---------------- 测试替身 ----------------


class ScriptedHarness:
    """按脚本产出事件的平台循环 harness：每次 run_turn 弹出一段脚本，并记录收到的 HookSet。"""

    def __init__(self, name: str, turns: list[list[Event]], internal: list[HookPoint] | None = None) -> None:
        self._name = name
        self._turns = turns
        self._internal = internal or []
        self.hooksets: list[HookSet] = []
        self.turn_inputs: list[TurnInput] = []
        self.adapter_verdicts: list[Any] = []

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            name=self._name, version="test", transport="in-process", loop_owner="platform", internal_hook_points=self._internal
        )

    async def open_session(self, spec: SessionSpec) -> SessionHandle:
        return SessionHandle(session_id=uuid.uuid4().hex, harness=self._name)

    async def resume(self, spec: SessionSpec, events: list[Event]) -> SessionHandle:
        return await self.open_session(spec)

    async def run_turn(
        self, session: SessionHandle, turn: TurnInput, tools: list[ToolSpec], skills: list[SkillDir], hooks: HookSet
    ) -> AsyncIterator[Event]:
        self.hooksets.append(hooks)
        self.turn_inputs.append(turn)
        if hooks.pre_tool is not None:  # 声明了内部 pre_tool 的适配器自己调用（平台看不到的点）
            self.adapter_verdicts.append(await hooks.pre_tool({"call_id": "inner", "tool": "mail.send", "input": {}}))
        for ev in self._turns.pop(0):
            yield ev

    async def interrupt(self, session: SessionHandle, run_id: str) -> None:
        return None


def _usage(n: int = 10) -> UsageEvent:
    return UsageEvent(input_tokens=n, output_tokens=n, model="scripted")


def _tool_turn(call_id: str, tool: str, inp: dict[str, Any], usage: int = 10) -> list[Event]:
    return [ToolUseEvent(call_id=call_id, tool_name=tool, input=inp), _usage(usage), DoneEvent(run_id="x", stop_reason="tool_use")]


def _text_turn(text: str = "完成", thinking: str | None = None) -> list[Event]:
    head: list[Event] = [ThinkingEvent(delta=thinking)] if thinking else []
    return head + [TextEvent(delta=text), _usage(), DoneEvent(run_id="x")]


def _sse(resp: Any) -> list[dict[str, Any]]:
    return [json.loads(line[5:].strip()) for line in resp.iter_lines() if line.startswith("data:")]


def _publish(client: Any, name: str, harness: ScriptedHarness, config: dict[str, Any]) -> None:
    registry.register(name, harness)
    r = client.post("/admin/agents", json={"agent_id": name, "version": "1", "harness": name, "system_prompt": "x", "config": config})
    assert r.status_code == 201, r.text
    r = client.post(f"/admin/agents/{name}/versions/1/publish", json={"eval_passed": True})
    assert r.status_code == 200, r.text


def _run(
    client: Any, turns: list[list[Event]], config: dict[str, Any], internal: list[HookPoint] | None = None
) -> tuple[list[dict[str, Any]], str, ScriptedHarness, list[dict[str, Any]]]:
    name = f"s5h-{uuid.uuid4().hex[:8]}"
    harness = ScriptedHarness(name, turns, internal)
    _publish(client, name, harness, config)
    with client.stream("POST", "/v1/messages", json={"agent_id": name, "messages": [{"role": "user", "content": "hi"}]}) as resp:
        assert resp.status_code == 200
        run_id = resp.headers["x-run-id"]
        evs = _sse(resp)
    return evs, run_id, harness, _log(client, run_id)


def _log(client: Any, run_id: str) -> list[dict[str, Any]]:
    return list(client.get(f"/v1/runs/{run_id}/events").json())


def _one(log: list[dict[str, Any]], typ: str) -> dict[str, Any]:
    return next(e["payload"] for e in log if e["type"] == typ)


def _state(client: Any, run_id: str) -> str:
    return str(client.get(f"/v1/runs/{run_id}").json()["state"])


def _custom(monkeypatch: pytest.MonkeyPatch, name: str, point: HookPoint, impl: hooks.HookImpl) -> None:
    """注册一个仅供测试的“内置” hook（发布校验与运行期都按名称查 BUILTINS）。"""
    monkeypatch.setitem(hooks.BUILTINS, name, hooks.Builtin(frozenset({point}), lambda _p: [], impl))


async def _boom(params: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
    raise RuntimeError("hook exploded")


async def _slow(params: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
    await asyncio.sleep(1)
    return None


@pytest.fixture
def fast_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hooks, "PURE_TIMEOUT_S", 0.05)


# ---------------- 单元：结论矩阵 ----------------

POINT_PAYLOAD: dict[HookPoint, dict[str, Any]] = {
    HookPoint.PRE_MODEL: {"round": 1, "model_alias": "m", "n_messages": 1, "messages_hash": "h", "taint": [], "usage": {"total_tokens": 0}},
    HookPoint.POST_MODEL: {"text": "t", "thinking": "长长的思考", "tool_call": None, "usage": {}},
    HookPoint.PRE_TOOL: {"call_id": "c", "tool": "echo", "input": {"a": 1}, "risk_level": "read", "data_class": "internal", "taint": []},
    HookPoint.POST_TOOL: {"call_id": "c", "tool": "echo", "status": "ok", "output": {"x": 1}, "data_class": "internal"},
}
OVERRIDE: dict[HookPoint, dict[str, Any]] = {
    HookPoint.PRE_MODEL: {"model_alias": "other"},  # pre_model 没有可覆盖键：被忽略
    HookPoint.POST_MODEL: {"thinking_summary": "要点"},
    HookPoint.PRE_TOOL: {"input": {"a": 2}},
    HookPoint.POST_TOOL: {"output": "redacted"},
}


@pytest.mark.parametrize("point", list(HookPoint))
@pytest.mark.parametrize("verdict", ["allow", "override", "deny", "error", "timeout"])
async def test_chain_verdict_matrix(monkeypatch, fast_timeout, point: HookPoint, verdict: str):
    """每个点 × 每种结论：deny 一律阻断；error / timeout 在 pre_* 与 post_tool 按拒绝处理（失败关闭），
    post_model 只记录不阻断；override 只应用白名单内的键。"""

    async def impl(params: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
        if verdict == "allow":
            return None
        if verdict == "override":
            return dict(OVERRIDE[point])
        if verdict == "deny":
            return {"deny": "nope"}
        if verdict == "error":
            raise ValueError("bad")
        await asyncio.sleep(1)
        return None

    _custom(monkeypatch, "t_matrix", point, impl)
    chain = hooks.HookChain([hooks.HookSpec(point, "t_matrix")])
    out = await chain.run(point, POINT_PAYLOAD[point])
    rec = out.records[0]
    assert rec["point"] == point.value and rec["name"] == "t_matrix"
    if verdict == "allow":
        assert rec["verdict"] == "allow" and not out.denied and out.overrides == {}
    elif verdict == "override":
        if point == HookPoint.PRE_MODEL:
            assert rec["verdict"] == "allow" and rec["ignored"] == ["model_alias"] and out.overrides == {}
        else:
            assert rec["verdict"] == "override" and out.overrides == OVERRIDE[point] and not out.denied
            assert out.payload | OVERRIDE[point] == out.payload
    elif verdict == "deny":
        assert rec["verdict"] == "deny" and out.denied_by == "t_matrix" and out.reason == "nope"
    else:
        assert rec["verdict"] == verdict
        assert out.denied is (point != HookPoint.POST_MODEL)
        assert out.overrides == {}


async def test_chain_stops_at_first_deny_and_chains_overrides(monkeypatch):
    """按声明顺序成链：前一个 hook 的覆盖对后一个可见；首个 deny 即停，后续不执行。"""
    seen: list[Any] = []

    async def rewrite(params, payload):
        return {"input": {"q": payload["input"]["q"] + "!"}}

    async def observe(params, payload):
        seen.append(payload["input"])
        return {"deny": "stop"}

    async def never(params, payload):
        seen.append("never")
        return None

    for name, impl in (("t_rw", rewrite), ("t_obs", observe), ("t_never", never)):
        _custom(monkeypatch, name, HookPoint.PRE_TOOL, impl)
    chain = hooks.HookChain([hooks.HookSpec(HookPoint.PRE_TOOL, n) for n in ("t_rw", "t_obs", "t_never")])
    out = await chain.run(HookPoint.PRE_TOOL, {"call_id": "c", "tool": "x", "input": {"q": "a"}})
    assert seen == [{"q": "a!"}] and out.denied_by == "t_obs"
    assert [r["verdict"] for r in out.records] == ["override", "deny"]


async def test_post_tool_data_class_only_upgrades(monkeypatch):
    """post_tool 的 data_class 只升不降；未知取值按 confidential（失败关闭）。"""

    async def down(params, payload):
        return {"data_class": "public"}

    async def weird(params, payload):
        return {"data_class": "top-secret"}

    _custom(monkeypatch, "t_down", HookPoint.POST_TOOL, down)
    _custom(monkeypatch, "t_weird", HookPoint.POST_TOOL, weird)
    base = {"call_id": "c", "tool": "x", "status": "ok", "output": 1, "data_class": "internal"}
    out = await hooks.HookChain([hooks.HookSpec(HookPoint.POST_TOOL, "t_down")]).run(HookPoint.POST_TOOL, base)
    assert out.payload["data_class"] == "internal"
    out = await hooks.HookChain([hooks.HookSpec(HookPoint.POST_TOOL, "t_weird")]).run(HookPoint.POST_TOOL, base)
    assert out.payload["data_class"] == "confidential"


async def test_pre_tool_non_object_input_override_is_denied(monkeypatch):
    async def bad(params, payload):
        return {"input": "rm -rf /"}

    _custom(monkeypatch, "t_bad", HookPoint.PRE_TOOL, bad)
    out = await hooks.HookChain([hooks.HookSpec(HookPoint.PRE_TOOL, "t_bad")]).run(HookPoint.PRE_TOOL, {"input": {}})
    assert out.denied and out.records[0]["verdict"] == "error"


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ("nope", "must be a list"),
        ([{"point": "pre_model", "name": "rm_rf"}], "unknown hook"),
        ([{"point": "pre_launch", "name": "token_budget"}], "unknown point"),
        ([{"point": "post_tool", "name": "token_budget", "params": {"max_total_tokens": 1}}], "cannot run at post_tool"),
        ([{"point": "pre_model", "name": "token_budget", "params": {}}], "max_total_tokens is required"),
        ([{"point": "pre_model", "name": "token_budget", "params": {"max_total_tokens": -1}}], "positive integer"),
        ([{"point": "post_model", "name": "thinking_summary", "params": {"mode": "magic"}}], "params.mode"),
        ([{"point": "pre_tool", "name": "deny_tool_pattern", "params": {"patterns": []}}], "params.patterns"),
        ([{"point": "post_tool", "name": "redact_pii", "params": {"kinds": ["dna"]}}], "params.kinds"),
        ([{"point": "post_tool", "name": "redact_pii", "params": {"x": 1}}], "unknown param"),
        ([{"point": "pre_tool", "name": "deny_tool_pattern", "params": {"patterns": ["a"]}, "code": "x"}], "unknown keys"),
    ],
)
def test_validate_hooks_config_rejects(raw: Any, fragment: str):
    problems = hooks.validate_hooks_config(raw)
    assert problems and any(fragment in p for p in problems), problems
    with pytest.raises(hooks.HookConfigError):
        hooks.parse_hooks(raw)


def test_validate_hooks_config_accepts_builtins_and_implicit_summary():
    cfg = [
        {"point": "pre_model", "name": "token_budget", "params": {"max_total_tokens": 1000}},
        {"point": "post_model", "name": "thinking_summary", "params": {"mode": "llm", "max_chars": 80}},
        {"point": "pre_tool", "name": "deny_tool_pattern", "params": {"patterns": ["mail.*"]}},
        {"point": "post_tool", "name": "redact_pii"},
    ]
    assert hooks.validate_hooks_config(cfg) == [] and hooks.validate_hooks_config(None) == []
    assert len(hooks.HookChain.for_agent({"hooks": cfg}, "summary").specs) == 4  # 已声明 thinking_summary：不重复追加
    implicit = hooks.HookChain.for_agent({}, "summary")
    assert [(s.point, s.name) for s in implicit.specs] == [(HookPoint.POST_MODEL, "thinking_summary")]
    assert hooks.HookChain.for_agent({}, "full").specs == [] and hooks.HookChain.for_agent(None).specs == []


def test_redact_text_patterns():
    text = "电话 13812345678，邮箱 a.b@corp.cn，身份证 11010519491231002X，卡号 6222021234567890123，编号 12345"
    out, hits = hooks.redact_text(text, list(hooks.PII_PATTERNS))
    assert hits == 4 and "12345" in out
    for raw in ("13812345678", "a.b@corp.cn", "11010519491231002X", "6222021234567890123"):
        assert raw not in out
    assert "[REDACTED:id_card]" in out and "[REDACTED:bank_card]" in out


# ---------------- 集成 ----------------

NEW_KEYS = {
    "hooks",
    "effective_input",
    "raw_output_hmac",
    "thinking_summary",
    "withheld_text_hmac",
    "credential_subject",
    "artifact_data_class",
}


def test_no_hooks_event_log_unchanged(client):
    """未配置 hooks（且非 summary 可见性）：事件 payload 形态与此前完全一致，适配器收到空 HookSet。"""
    evs, run_id, harness, log = _run(client, [_tool_turn("c-plain", "echo", {"text": "x"}), _text_turn()], {"tools": ["echo"]})
    assert evs[-1]["stop_reason"] == "end_turn"
    for e in log:
        assert not NEW_KEYS & set(e["payload"]), e
    assert set(_one(log, "model.request")) == {"model_alias", "messages_hash", "n_messages"}
    assert set(_one(log, "tool.requested")) == {"call_id", "tool", "input", "risk_level"}
    assert set(_one(log, "tool.result")) == {"call_id", "status", "output", "output_ref", "data_class"}
    # initial_messages_hash 与 hooks 无关：所有经 API 创建的 Run 都记录（初始消息与事件日志绑定）
    assert set(_one(log, "run.created")) == {
        "agent_id",
        "agent_version",
        "model_alias",
        "subject",
        "actor",
        "rollout",
        "initial_messages_hash",
    }
    assert all(hs.model_dump() == HookSet().model_dump() for hs in harness.hooksets)


def test_pre_model_token_budget_denies_second_round(client):
    """token_budget：首轮累计用量达到上限后，第二轮 pre_model 拒绝 → HOOK_DENIED、Run failed、不再调用模型。"""
    cfg = {"tools": ["echo"], "hooks": [{"point": "pre_model", "name": "token_budget", "params": {"max_total_tokens": 30}}]}
    evs, run_id, harness, log = _run(client, [_tool_turn("c-budget", "echo", {"text": "x"}, usage=20), _text_turn()], cfg)
    assert evs[-1]["stop_reason"] == "error"
    assert next(e for e in evs if e["type"] == "error")["code"] == "HOOK_DENIED"
    assert _state(client, run_id) == "failed"
    assert len(harness.hooksets) == 1  # 第二轮没有调用模型
    reqs = [e["payload"] for e in log if e["type"] == "model.request"]
    assert [r["hooks"][0]["verdict"] for r in reqs] == ["allow", "deny"]
    err = _one(log, "run.error")
    assert err["code"] == "HOOK_DENIED" and err["hook"] == "token_budget" and err["point"] == "pre_model"


@pytest.mark.parametrize("kind", ["error", "timeout"])
def test_pre_model_failure_is_fail_closed(client, monkeypatch, fast_timeout, kind: str):
    _custom(monkeypatch, "t_pm", HookPoint.PRE_MODEL, _boom if kind == "error" else _slow)
    cfg = {"tools": ["echo"], "hooks": [{"point": "pre_model", "name": "t_pm"}]}
    evs, run_id, harness, log = _run(client, [_text_turn()], cfg)
    assert evs[-1]["stop_reason"] == "error" and harness.hooksets == []
    assert _one(log, "model.request")["hooks"] == [
        {"point": "pre_model", "name": "t_pm", "verdict": kind, "reason": "RuntimeError" if kind == "error" else "timed out after 0.05s"}
    ]
    assert _state(client, run_id) == "failed"


def test_post_model_deny_withholds_round_and_fails(client, monkeypatch):
    """post_model 拒绝：本轮文本不进可重建历史（MODEL_RESPONSE 只留指纹）、请求的工具不执行，Run failed。"""

    async def deny(params, payload):
        return {"deny": "unsafe output"}

    _custom(monkeypatch, "t_post_deny", HookPoint.POST_MODEL, deny)
    cfg = {"tools": ["echo"], "hooks": [{"point": "post_model", "name": "t_post_deny"}]}
    turn = [TextEvent(delta="机密回答"), ToolUseEvent(call_id="c-pd", tool_name="echo", input={}), _usage(), DoneEvent(run_id="x")]
    evs, run_id, _, log = _run(client, [turn], cfg)
    assert evs[-1]["stop_reason"] == "error" and _state(client, run_id) == "failed"
    resp = _one(log, "model.response")
    # 被扣文本只留带密钥的 HMAC（与 raw_output_hmac 同一密钥与规范化），不是可穷举的裸 sha256
    assert resp["text"] == "" and resp["withheld_text_hmac"] == toolbroker.hmac_json("机密回答")
    canon = json.dumps("机密回答", ensure_ascii=False).encode()
    assert resp["withheld_text_hmac"] == hmac.new(settings.hook_digest_key.encode(), canon, hashlib.sha256).hexdigest()
    assert "withheld_text_sha256" not in resp and resp["withheld_text_hmac"] != hashlib.sha256("机密回答".encode()).hexdigest()
    assert resp["hooks"][0]["verdict"] == "deny"
    assert "tool.requested" not in [e["type"] for e in log]
    assert _one(log, "run.error")["code"] == "HOOK_DENIED"


def test_turn_input_carries_tool_statuses_from_event_log(client, monkeypatch):
    """TurnInput.tool_statuses（Contract V1.1）：runner 按事件日志 TOOL_RESULT 为本轮历史中的工具结果填写状态，
    实时路径上适配器即可把 denied / error 结果以错误形式交给模型；首轮没有工具结果时为空字典。"""

    async def deny_marked(params, payload):
        return {"deny": "blocked by test"} if payload["input"].get("block") else None

    _custom(monkeypatch, "t_pre_deny_marked", HookPoint.PRE_TOOL, deny_marked)
    cfg = {"tools": ["echo"], "hooks": [{"point": "pre_tool", "name": "t_pre_deny_marked"}]}
    turns = [_tool_turn("c-ok", "echo", {"text": "x"}), _tool_turn("c-den", "echo", {"text": "y", "block": True}), _text_turn()]
    evs, run_id, harness, log = _run(client, turns, cfg)
    assert evs[-1]["stop_reason"] == "end_turn"
    assert [ti.tool_statuses for ti in harness.turn_inputs] == [{}, {"c-ok": "ok"}, {"c-ok": "ok", "c-den": "denied"}]
    assert [e["payload"]["status"] for e in log if e["type"] == "tool.result"] == ["ok", "denied"]


def test_post_model_deny_text_never_reaches_client(client, monkeypatch):
    """配置 post_model 时本轮文本 / 工具调用先缓冲：被拒绝的一轮，客户端一个 text / tool_use 增量都收不到。"""

    async def deny(params, payload):
        return {"deny": "unsafe output"}

    _custom(monkeypatch, "t_post_deny_live", HookPoint.POST_MODEL, deny)
    cfg = {"tools": ["echo"], "hooks": [{"point": "post_model", "name": "t_post_deny_live"}]}
    turn = [
        TextEvent(delta="机密"),
        TextEvent(delta="回答"),
        ToolUseEvent(call_id="c-live", tool_name="echo", input={}),
        _usage(),
        DoneEvent(run_id="x"),
    ]
    evs, run_id, _, _ = _run(client, [turn], cfg)
    assert not {"text", "tool_use"} & {e["type"] for e in evs}
    assert "机密" not in json.dumps(evs, ensure_ascii=False)
    assert [e["type"] for e in evs][-2:] == ["error", "done"] and _state(client, run_id) == "failed"


def test_post_model_pass_releases_buffered_events_in_order(client, monkeypatch):
    """post_model 通过：缓冲的事件按原顺序下发（文本 → 工具调用 → 用量），之后照常执行工具；未配置时保持流式（见其他用例）。"""

    async def allow(params, payload):
        return None

    _custom(monkeypatch, "t_post_allow", HookPoint.POST_MODEL, allow)
    cfg = {"tools": ["echo"], "hooks": [{"point": "post_model", "name": "t_post_allow"}]}
    first = [TextEvent(delta="我来"), TextEvent(delta="查"), ToolUseEvent(call_id="c-ok", tool_name="echo", input={"t": 1}), _usage()]
    evs, run_id, _, _ = _run(client, [[*first, DoneEvent(run_id="x", stop_reason="tool_use")], _text_turn("完成")], cfg)
    types = [e["type"] for e in evs]
    assert types[:5] == ["text", "text", "tool_use", "usage", "tool_result"], types
    assert "".join(e["delta"] for e in evs if e["type"] == "text") == "我来查完成"
    assert evs[-1]["stop_reason"] == "end_turn" and _state(client, run_id) == "completed"


@pytest.mark.parametrize("kind", ["error", "timeout"])
def test_post_model_failure_only_drops_summary(client, monkeypatch, fast_timeout, kind: str):
    """post_model 出错 / 超时不阻断 Run：只记录、不覆盖（thinking_summary 失败即不下发思维链，见 llm 失败用例）。"""
    _custom(monkeypatch, "t_post_fail", HookPoint.POST_MODEL, _boom if kind == "error" else _slow)
    cfg = {"hooks": [{"point": "post_model", "name": "t_post_fail"}]}
    evs, run_id, _, log = _run(client, [_text_turn(thinking="想法")], cfg)
    assert evs[-1]["stop_reason"] == "end_turn" and _state(client, run_id) == "completed"
    assert "thinking" not in [e["type"] for e in evs]
    resp = _one(log, "model.response")
    assert [r["verdict"] for r in resp["hooks"]] == [kind] and "thinking_summary" not in resp


def test_pre_tool_deny_tool_pattern(client):
    """deny_tool_pattern：TOOL_RESULT 记 denied（reason=hook:<名称>），写 tool.decision，不进闸口、不执行，Run 继续。"""
    cfg = {
        "tools": ["echo", "mail.send"],
        "hooks": [{"point": "pre_tool", "name": "deny_tool_pattern", "params": {"patterns": ["mail.*"]}}],
    }
    evs, run_id, _, log = _run(client, [_tool_turn("c-mail", "mail.send", {"to": "x@y.z"}), _text_turn()], cfg)
    tr = next(e for e in evs if e["type"] == "tool_result")
    assert tr["status"] == "denied" and tr["output"] == "hook:deny_tool_pattern"
    req = _one(log, "tool.requested")
    assert req["input"] == {"to": "x@y.z"} and "effective_input" not in req and req["hooks"][0]["verdict"] == "deny"
    dec = _one(log, "tool.decision")
    assert dec["decision"] == "deny" and dec["reason"] == "hook:deny_tool_pattern"
    assert "tool.executing" not in [e["type"] for e in log]
    assert evs[-1]["stop_reason"] == "end_turn" and _state(client, run_id) == "completed"


def test_pre_tool_override_uses_effective_input(client, monkeypatch):
    """pre_tool 改写入参：TOOL_REQUESTED 同时记模型原始 input 与 effective_input，执行用 effective_input；
    历史中的 tool_use 保持原始入参（可重建性不变量照常通过，Run 正常结束）。"""

    async def rewrite(params, payload):
        return {"input": {"text": payload["input"]["text"].upper()}, "tool": "rm"}

    _custom(monkeypatch, "t_upper", HookPoint.PRE_TOOL, rewrite)
    cfg = {"tools": ["echo"], "hooks": [{"point": "pre_tool", "name": "t_upper"}]}
    evs, run_id, _, log = _run(client, [_tool_turn("c-up", "echo", {"text": "abc"}), _text_turn()], cfg)
    req = _one(log, "tool.requested")
    assert req["input"] == {"text": "abc"} and req["effective_input"] == {"text": "ABC"}
    assert req["hooks"] == [{"point": "pre_tool", "name": "t_upper", "verdict": "override", "ignored": ["tool"]}]
    assert _one(log, "tool.decision")["params_hash"] == params_hash({"text": "ABC"})
    assert next(e for e in evs if e["type"] == "tool_result")["output"] == {"echo": {"text": "ABC"}}
    assert evs[-1]["stop_reason"] == "end_turn"


@pytest.mark.parametrize("kind", ["error", "timeout"])
def test_pre_tool_failure_denies(client, monkeypatch, fast_timeout, kind: str):
    _custom(monkeypatch, "t_pt", HookPoint.PRE_TOOL, _boom if kind == "error" else _slow)
    cfg = {"tools": ["echo"], "hooks": [{"point": "pre_tool", "name": "t_pt"}]}
    evs, _, _, log = _run(client, [_tool_turn("c-ptf", "echo", {}), _text_turn()], cfg)
    tr = next(e for e in evs if e["type"] == "tool_result")
    assert tr["status"] == "denied" and tr["output"] == "hook:t_pt"
    assert _one(log, "tool.requested")["hooks"][0]["verdict"] == kind


def test_post_tool_redact_pii_and_upgrade_data_class(client):
    """redact_pii：TOOL_RESULT 与回灌模型的输出已脱敏，原文只留带服务端密钥的 HMAC（不是可穷举的裸 sha256）；
    data_class 按参数升级为 confidential 并计污点。"""
    cfg = {"tools": ["echo"], "hooks": [{"point": "post_tool", "name": "redact_pii", "params": {"data_class": "confidential"}}]}
    evs, run_id, _, log = _run(client, [_tool_turn("c-pii", "echo", {"t": "13812345678"}), _text_turn()], cfg)
    res = _one(log, "tool.result")
    assert "13812345678" not in json.dumps(res, ensure_ascii=False)
    assert "13812345678" not in json.dumps(next(e for e in evs if e["type"] == "tool_result"), ensure_ascii=False)
    raw = {"echo": {"t": "13812345678"}}
    canon = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert (
        res["raw_output_hmac"]
        == toolbroker.hmac_json(raw)
        == hmac.new(settings.hook_digest_key.encode(), canon, hashlib.sha256).hexdigest()
    )
    assert res["raw_output_hmac"] != hashlib.sha256(canon).hexdigest() and "raw_output_sha256" not in res
    assert res["data_class"] == "confidential" and res["status"] == "ok"
    assert res["hooks"][0]["verdict"] == "override"
    assert evs[-1]["stop_reason"] == "end_turn"  # conftest 允许 default-chat 接收机密数据


@pytest.mark.parametrize("kind", ["deny", "error", "timeout"])
def test_post_tool_failure_withholds_output(client, monkeypatch, fast_timeout, kind: str):
    async def deny(params, payload):
        return {"deny": "no"}

    impl = {"deny": deny, "error": _boom, "timeout": _slow}[kind]
    _custom(monkeypatch, "t_ptool", HookPoint.POST_TOOL, impl)
    cfg = {"tools": ["echo"], "hooks": [{"point": "post_tool", "name": "t_ptool"}]}
    evs, _, _, log = _run(client, [_tool_turn("c-wh", "echo", {"secret": "s3"}), _text_turn()], cfg)
    res = _one(log, "tool.result")
    assert res["status"] == "error" and res["output"] == {"error": "output withheld by hook:t_ptool"}
    assert "s3" not in json.dumps(res) and res["hooks"][0]["verdict"] == kind
    assert evs[-1]["stop_reason"] == "end_turn"


def test_pending_call_resumes_with_effective_input_without_rerunning_hooks(client, monkeypatch):
    """高风险写经 pre_tool 改写后挂起审批：审批指纹按 effective_input；续跑按 effective_input 执行，pre_tool 不重跑。"""
    calls: list[Any] = []

    async def retarget(params, payload):
        calls.append(payload["input"])
        return {"input": {"ids": ["T-9"]}}

    _custom(monkeypatch, "t_retarget", HookPoint.PRE_TOOL, retarget)
    cfg = {"tools": ["ticket.close_batch"], "hooks": [{"point": "pre_tool", "name": "t_retarget"}]}
    evs, run_id, harness, log = _run(client, [_tool_turn("c-close", "ticket.close_batch", {"ids": ["T-1"]}), _text_turn()], cfg)
    card = next(e for e in evs if e["type"] == "card")
    assert evs[-1]["stop_reason"] == "awaiting" and card["payload"]["params_hash"] == params_hash({"ids": ["T-9"]})
    assert card["payload"]["preview"]["impact_preview"]["tickets"] == 1
    ok = client.post(f"/v1/runs/{run_id}/approve", json={"call_id": "c-close", "params_hash": card["payload"]["params_hash"]})
    assert ok.status_code == 200, ok.text
    with client.stream("POST", f"/v1/runs/{run_id}/resume", json={"messages": [{"role": "user", "content": "hi"}]}) as r:
        evs2 = _sse(r)
    assert evs2[-1]["stop_reason"] == "end_turn", evs2
    assert calls == [{"ids": ["T-1"]}]  # 续跑没有重跑 pre_tool
    log = _log(client, run_id)
    assert len([e for e in log if e["type"] == "tool.requested"]) == 1
    assert _one(log, "tool.executing")["params_hash"] == params_hash({"ids": ["T-9"]})
    assert next(e for e in evs2 if e["type"] == "tool.result")["payload"]["output"] == {"closed": ["T-9"]}  # 尾随的日志行


def _summary_client(monkeypatch: pytest.MonkeyPatch, seen: list[dict[str, Any]], status: int = 200) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        if status != 200:
            return httpx.Response(status, json={"error": "boom"})
        return httpx.Response(200, json={"choices": [{"message": {"content": " 先查工单再总结 "}, "finish_reason": "stop"}], "usage": {}})

    client = LLMClient(base_url="http://gw.test", api_key="k", max_retries=0, backoff_s=0, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(hooks.llm, "get_client", lambda: client)


def test_summary_visibility_llm_mode_streams_one_summary(client, monkeypatch):
    """D4：summary 可见性下原始思维增量不下发；post_model 经 LiteLLM 客户端生成摘要后只下发一条 summary 思维事件。"""
    seen: list[dict[str, Any]] = []
    _summary_client(monkeypatch, seen)
    cfg = {"thinking_visibility": "summary", "hooks": [{"point": "post_model", "name": "thinking_summary", "params": {"mode": "llm"}}]}
    evs, run_id, _, log = _run(client, [_text_turn(thinking="第一段思考。第二段思考。")], cfg)
    thinking = [e for e in evs if e["type"] == "thinking"]
    assert [(t["delta"], t["visibility"]) for t in thinking] == [("先查工单再总结", "summary")]
    assert seen and seen[0]["model"] == "default-chat" and seen[0]["messages"][1]["content"] == "第一段思考。第二段思考。"
    resp = _one(log, "model.response")
    assert resp["thinking"] == "第一段思考。第二段思考。" and resp["thinking_summary"] == "先查工单再总结"
    assert resp["hooks"] == [{"point": "post_model", "name": "thinking_summary", "verdict": "override"}]


def test_summary_llm_failure_streams_nothing(client, monkeypatch):
    seen: list[dict[str, Any]] = []
    _summary_client(monkeypatch, seen, status=500)
    cfg = {"thinking_visibility": "summary", "hooks": [{"point": "post_model", "name": "thinking_summary", "params": {"mode": "llm"}}]}
    evs, run_id, _, log = _run(client, [_text_turn(thinking="思考")], cfg)
    assert "thinking" not in [e["type"] for e in evs] and evs[-1]["stop_reason"] == "end_turn"
    assert _one(log, "model.response")["hooks"][0]["verdict"] == "error"


def test_summary_not_generated_by_unauthorized_model_under_confidential_taint(client, monkeypatch):
    """Run 带机密污点时，摘要模型不在 CONFIDENTIAL_MODEL_ALIASES 内：不调用摘要模型、不下发摘要。"""
    seen: list[dict[str, Any]] = []
    _summary_client(monkeypatch, seen)
    cfg = {
        "tools": ["data.query"],
        "thinking_visibility": "summary",
        "hooks": [{"point": "post_model", "name": "thinking_summary", "params": {"mode": "llm", "model_alias": "public-cloud"}}],
    }
    evs, run_id, _, log = _run(client, [_tool_turn("c-conf", "data.query", {}), _text_turn(thinking="机密推理")], cfg)
    assert evs[-1]["stop_reason"] == "end_turn"
    assert "thinking" not in [e["type"] for e in evs] and seen == []
    assert all("thinking_summary" not in e["payload"] for e in log if e["type"] == "model.response")


def test_implicit_truncate_summary_when_visibility_summary(client):
    """未声明 thinking_summary 时 summary 可见性隐式挂默认（truncate，200 字）。"""
    long = "想" * 250
    evs, _, _, log = _run(client, [_text_turn(thinking=long)], {"thinking_visibility": "summary"})
    thinking = [e for e in evs if e["type"] == "thinking"]
    assert len(thinking) == 1 and thinking[0]["delta"] == "想" * 200 + "…" and thinking[0]["visibility"] == "summary"
    assert _one(log, "model.response")["thinking"] == long


def test_adapter_hookset_contains_only_declared_internal_points(client):
    """传给 run_turn 的 HookSet 只含适配器声明的 internal_hook_points；适配器侧结论随 MODEL_RESPONSE 记录（source=adapter）。"""
    cfg = {
        "hooks": [
            {"point": "pre_model", "name": "token_budget", "params": {"max_total_tokens": 10000}},
            {"point": "pre_tool", "name": "deny_tool_pattern", "params": {"patterns": ["mail.*"]}},
        ]
    }
    evs, run_id, harness, log = _run(client, [_text_turn()], cfg, internal=[HookPoint.PRE_TOOL, HookPoint.POST_TOOL])
    hs = harness.hooksets[0]
    assert hs.pre_tool is not None and hs.pre_model is None and hs.post_model is None and hs.post_tool is None
    assert harness.adapter_verdicts == [{"deny": "hook:deny_tool_pattern"}]
    resp = _one(log, "model.response")
    assert resp["hooks"] == [
        {"point": "pre_tool", "name": "deny_tool_pattern", "verdict": "deny", "reason": "tool 'mail.send' matches deny pattern 'mail.*'"}
        | {"source": "adapter"}
    ]
    assert evs[-1]["stop_reason"] == "end_turn"


def test_invalid_hooks_config_at_runtime_fails_closed(client, monkeypatch):
    """运行期发现 hooks 配置不合规（如发布后内置 hook 被移除）：不带 hook 继续跑，而是 HOOK_CONFIG_INVALID 失败。"""
    _custom(monkeypatch, "t_gone", HookPoint.PRE_MODEL, _boom)
    name = f"s5h-{uuid.uuid4().hex[:8]}"
    harness = ScriptedHarness(name, [_text_turn()])
    _publish(client, name, harness, {"hooks": [{"point": "pre_model", "name": "t_gone"}]})
    monkeypatch.delitem(hooks.BUILTINS, "t_gone")
    with client.stream("POST", "/v1/messages", json={"agent_id": name, "messages": [{"role": "user", "content": "hi"}]}) as resp:
        evs = _sse(resp)
        run_id = resp.headers["x-run-id"]
    assert next(e for e in evs if e["type"] == "error")["code"] == "HOOK_CONFIG_INVALID"
    assert _state(client, run_id) == "failed" and harness.hooksets == []


def test_hook_results_recorded_on_spans(client, monkeypatch):
    """Trace：hook 结论作为 span 属性（只记名称 / 结论），不记负载原文。"""
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from runtime.observe import tracing

    exporter = InMemorySpanExporter()
    tracing.add_span_processor(SimpleSpanProcessor(exporter))
    cfg = {
        "tools": ["echo"],
        "hooks": [
            {"point": "pre_model", "name": "token_budget", "params": {"max_total_tokens": 10000}},
            {"point": "post_tool", "name": "redact_pii"},
        ],
    }
    _run(client, [_tool_turn("c-span", "echo", {"t": "13812345678"}), _text_turn()], cfg)
    spans = exporter.get_finished_spans()
    tool = next(s for s in spans if s.attributes and s.attributes.get("gen_ai.tool.call.id") == "c-span")
    assert tool.attributes["ai.hook.post_tool.names"] == ("redact_pii",)
    assert tool.attributes["ai.hook.post_tool.verdicts"] == ("override",)
    chat = [s for s in spans if s.name.startswith("chat ") and s.attributes and "ai.hook.pre_model.names" in s.attributes]
    assert chat and all("13812345678" not in json.dumps(dict(s.attributes or {}), ensure_ascii=False) for s in spans)

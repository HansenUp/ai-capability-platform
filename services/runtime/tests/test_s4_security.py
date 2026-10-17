"""S4.8 最小安全闭环与 S4.9 数据分级最小矩阵：非白名单工具、附件越权、机密数据外发 / 检索 / 模型阻断、
受控模型出口、令牌不进审计。跨用户访问（行级权限）见 mcp-servers/ticket 的测试与 make verify-s4。"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest
from sqlalchemy import select
from test_runner_stop_reasons import ScriptedHarness, _log, _publish, _run_with

from ai_contract import DoneEvent, TextEvent, ToolUseEvent, UsageEvent
from runtime.api import admin
from runtime.loop import runner, toolbroker
from runtime.state.db import Audit, SessionLocal

H = {"X-AI-Subject": "human:alice"}


def _tool_turn(name: str, args: dict[str, Any], call_id: str) -> list[Any]:
    return [ToolUseEvent(call_id=call_id, tool_name=name, input=args), UsageEvent(), DoneEvent(run_id="x", stop_reason="tool_use")]


def _final(text: str = "好的") -> list[Any]:
    return [TextEvent(delta=text), UsageEvent(), DoneEvent(run_id="x")]


def _results(log: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {e["payload"]["call_id"]: e["payload"] for e in log if e["type"] == "tool.result"}


# ---------------- 非白名单工具 ----------------


def test_non_whitelisted_tool_is_never_executed(client: Any) -> None:
    """Agent 只声明了 ticket.search：模型请求 ticket.close_batch 时记为错误结果，不进闸口、不执行。"""
    turns = [_tool_turn("ticket.close_batch", {"ids": ["T-1"]}, "c1"), _final()]
    _, run_id, _ = _run_with(client, "wl-agent", turns, {"tools": ["ticket.search"]})
    log = _log(client, run_id)
    assert _results(log)["c1"]["status"] == "error"
    assert not any(e["type"] in ("tool.decision", "tool.executed") and e["payload"].get("call_id") == "c1" for e in log)
    assembled = next(e for e in log if e["type"] == "context.assembled")["payload"]["tools"]
    assert "ticket.close_batch" not in assembled


# ---------------- 附件越权 ----------------


def _offloaded_ref(client: Any, headers: dict[str, str]) -> str:
    """以 headers 身份触发一次大结果工具调用（测试阈值 20 token，结果会被外置），返回 artifact 引用。"""
    body = {"agent_id": "demo", "messages": [{"role": "user", "content": "!tool ticket.search {}"}], "stream": False}
    run_id = client.post("/v1/messages", json=body, headers=headers).json()["run_id"]
    refs = [
        e["payload"].get("output_ref")
        for e in client.get(f"/v1/runs/{run_id}/events", headers=headers).json()
        if e["type"] == "tool.result"
    ]
    assert refs and refs[0], "工具结果应已外置"
    return str(refs[0])


def test_attachment_must_belong_to_caller(client: Any) -> None:
    ref = _offloaded_ref(client, H)
    body = {"agent_id": "demo", "messages": [{"role": "user", "content": "看附件"}], "attachments": [ref], "stream": False}
    assert client.post("/v1/messages", json=body, headers=H).status_code == 200  # 本人的工件
    assert client.post("/v1/messages", json=body, headers={"X-AI-Subject": "human:bob"}).status_code == 403  # 他人的工件
    other_tenant = H | {"X-AI-Tenant": "t2"}
    assert client.post("/v1/messages", json=body, headers=other_tenant).status_code in (403, 404)
    bad = body | {"attachments": ["artifact://../../etc/passwd"]}
    assert client.post("/v1/messages", json=bad, headers=H).status_code == 403


def test_read_artifact_is_confined_to_current_run(tmp_path: Any) -> None:
    run_a, run_b = tmp_path / "a", tmp_path / "b"
    run_a.mkdir()
    run_b.mkdir()
    (run_a / ("0" * 32 + ".txt")).write_text("secret-a", encoding="utf-8")
    assert toolbroker.read_artifact(str(run_a), "artifact://" + "0" * 32)["content"] == "secret-a"
    assert "error" in toolbroker.read_artifact(str(run_b), "artifact://" + "0" * 32)  # 其他 Run 的工件
    assert "error" in toolbroker.read_artifact(str(run_b), "artifact://../a/" + "0" * 32)  # 路径穿越


# ---------------- 机密数据外发 / 检索 / 模型 ----------------


def test_confidential_data_cannot_be_sent_out_without_approval(client: Any) -> None:
    """读过机密数据后：外发（mail.send）收紧为需审批并挂起；检索服务（knowledge.search）直接拒绝。"""
    turns = [
        _tool_turn("data.query", {"sql": "select 1"}, "c1"),
        _tool_turn("knowledge.search", {"q": "合同"}, "c2"),
        _tool_turn("mail.send", {"to": "x@example.com"}, "c3"),
        _final(),
    ]
    evs, run_id, _ = _run_with(client, "dc-agent", turns, {"tools": ["data.query", "knowledge.search", "mail.send"]})
    log = _log(client, run_id)
    decisions = {e["payload"]["call_id"]: e["payload"] for e in log if e["type"] == "tool.decision"}
    assert decisions["c2"]["decision"] == "deny" and decisions["c2"]["reason"] == "data_class:confidential"
    assert decisions["c3"]["decision"] == "require_approval"
    assert evs[-1]["stop_reason"] == "awaiting" and not any(e["type"] == "tool.executed" and e["payload"]["call_id"] == "c3" for e in log)


def test_confidential_taint_blocks_unauthorized_model(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """机密污点下，模型别名不在允许清单内：不再调用模型，Run 以 DATA_CLASS_MODEL_BLOCKED 失败。"""
    monkeypatch.setattr(runner, "settings", dataclasses.replace(runner.settings, confidential_model_aliases=()))
    turns = [_tool_turn("data.query", {"sql": "select 1"}, "c1"), _final("不应到达")]
    evs, run_id, harness = _run_with(client, "dc-block", turns, {"tools": ["data.query"]})
    assert evs[-1]["stop_reason"] == "error"
    assert any(e["type"] == "error" and e["code"] == "DATA_CLASS_MODEL_BLOCKED" for e in evs)
    assert len(harness.seen) == 1  # 第二轮模型调用被阻断
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "failed"


# ---------------- 受控模型出口 ----------------


def test_publish_rejects_unregistered_model_alias(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from runtime import llm

    class FakeClient:
        async def resolve_alias(self, alias: str) -> Any:
            if alias == "known-model":
                return object()
            raise llm.UnknownModelAlias(alias)

    monkeypatch.setattr(admin.llm, "get_client", lambda: FakeClient())
    for alias, expect in (("rogue-model", 409), ("known-model", 200)):
        aid = f"egress-{alias}"
        assert (
            client.post("/admin/agents", json={"agent_id": aid, "version": "1", "harness": "litellm", "model_alias": alias}).status_code
            == 201
        )
        assert client.post(f"/admin/agents/{aid}/versions/1/publish", json={"eval_passed": True}).status_code == expect


# ---------------- 敏感日志 ----------------


async def test_tokens_never_reach_audit_or_event_log(client: Any) -> None:
    """Bearer 令牌只在内存中随 Run 传递：审计行与事件日志里都不出现。"""
    secret = "sensitive-token-value-should-not-leak"  # noqa: S105 —— 测试用假令牌
    harness = ScriptedHarness("tok-agent", [_final()])
    _publish(client, "tok-agent", harness)
    body = {"agent_id": "tok-agent", "messages": [{"role": "user", "content": "hi"}], "stream": False}
    run_id = client.post("/v1/messages", json=body, headers=H | {"Authorization": f"Bearer {secret}"}).json()["run_id"]
    assert secret not in json.dumps(client.get(f"/v1/runs/{run_id}/events").json())
    async with SessionLocal() as s:
        rows = (await s.execute(select(Audit))).scalars().all()
    assert all(secret not in json.dumps(r.detail or {}) for r in rows)


def test_read_artifact_result_is_not_offloaded_again(client: Any) -> None:
    """外置结果经 read_artifact 读回时不再外置（测试阈值 20 token，否则会无限外置直到轮数耗尽）。"""
    body = {"agent_id": "demo", "messages": [{"role": "user", "content": "!tool ticket.search {}"}], "stream": False}
    run_id = client.post("/v1/messages", json=body, headers=H).json()["run_id"]
    ref = next(e["payload"]["output_ref"] for e in client.get(f"/v1/runs/{run_id}/events").json() if e["type"] == "tool.result")
    turns = [_tool_turn("read_artifact", {"ref": ref}, "c1"), _final()]
    harness = ScriptedHarness("ra-agent", turns)
    _publish(client, "ra-agent", harness)
    body = {"agent_id": "ra-agent", "messages": [{"role": "user", "content": "读附件"}], "attachments": [ref], "stream": False}
    run2 = client.post("/v1/messages", json=body, headers=H).json()["run_id"]
    result = _results(client.get(f"/v1/runs/{run2}/events").json())["c1"]
    assert result["status"] == "ok" and result["output_ref"] is None and "示例工单" in json.dumps(result["output"], ensure_ascii=False)


def test_read_tool_gateway_failure_is_a_tool_error_not_reconciliation(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """S4 评审：只读工具经网关失败（令牌过期、网关不可达）作为工具错误交还模型，Run 正常继续，不走结果未知 / 对账。"""
    import httpx

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("gateway down")

    monkeypatch.setattr(toolbroker, "settings", dataclasses.replace(toolbroker.settings, tool_dispatch="gateway"))
    toolbroker.set_gateway_transport_for_tests(httpx.MockTransport(down))
    try:
        turns = [_tool_turn("ticket.search", {"q": "x"}, "c1"), _final("工单系统暂时不可用")]
        evs, run_id, _ = _run_with(client, "gw-down", turns, {"tools": ["ticket.search"]})
    finally:
        toolbroker.set_gateway_transport_for_tests(None)
    log = _log(client, run_id)
    assert _results(log)["c1"]["status"] == "error"
    assert evs[-1]["stop_reason"] == "end_turn" and client.get(f"/v1/runs/{run_id}").json()["state"] == "completed"
    assert not any(e["type"] == "card" for e in evs)


def test_rollback_to_pre_release_version_is_refused(client: Any) -> None:
    """S4 评审：0% 预发布版本没过评测闸门，不能借回滚成为全量稳定版。"""
    assert client.post("/admin/agents", json={"agent_id": "pre", "version": "1"}).status_code == 201
    body = {"eval_passed": True, "rollout_percent": 0, "rollout_allowlist": ["human:alice"]}
    assert client.post("/admin/agents/pre/versions/1/publish", json=body).status_code == 200
    assert client.post("/admin/agents/pre/versions/1/rollback").status_code == 409

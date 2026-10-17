"""S5 WP-D 安全遗留：续跑只允许 Run 发起人（D87 修订：审批人只能 approve / reject）、续跑回传的初始消息须与
RUN_CREATED 记录的指纹一致、附件按源工件分级继承污点（D88，含外置时 Run 已有污点）、Run 读取限发起人 / 审批人 / 管理员，
待审批期间有审批资格者只见收窄的审批视图（D90）。

强制鉴权的用例复用 test_auth 的测试 IdP（AUTH_REQUIRED=true，令牌现场签发）；
credential_subject 与附件元信息通过包装 runner.drive / runner.create_run 捕获调用参数断言（写入事件由 runner 负责）。
"""

from __future__ import annotations

import dataclasses
import json
import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from test_auth import AUDIENCE, ISSUER, JWKS_URL, IdP, app_claims, bearer
from test_runner_stop_reasons import ScriptedHarness, _publish
from test_s4_security import _final, _offloaded_ref, _sse_events, _tool_turn

from ai_contract import RunEvent, RunEventType
from runtime.api import auth, messages
from runtime.config import settings
from runtime.harness import registry
from runtime.loop import runner, toolbroker
from runtime.run import invariant

CLOSE = [{"role": "user", "content": '!tool ticket.close_batch {"ids":["T-1"]}'}]


@pytest.fixture
def idp(monkeypatch: pytest.MonkeyPatch) -> Iterator[IdP]:
    """开启强制鉴权（同 test_auth.idp）。"""
    provider = IdP()
    strict = dataclasses.replace(auth.settings, auth_required=True, jwks_url=JWKS_URL, jwt_issuer=ISSUER, jwt_audience=AUDIENCE)
    monkeypatch.setattr(auth, "settings", strict)
    auth.set_transport_for_tests(provider.transport())
    yield provider
    auth.set_transport_for_tests(None)


@pytest.fixture
def drive_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """包装 runner.drive：记录每次调用的发起主体与 credential_subject，再交给原实现。"""
    calls: list[dict[str, Any]] = []
    original = runner.drive

    def wrapped(run_id: str, agent: Any, subject: str, msgs: list[dict[str, Any]], **kw: Any) -> Any:
        calls.append({"run_id": run_id, "subject": subject} | kw)
        return original(run_id, agent, subject, msgs, **kw)

    monkeypatch.setattr(runner, "drive", wrapped)
    return calls


@pytest.fixture
def create_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """包装 runner.create_run：记录传入的 attachments。"""
    calls: list[dict[str, Any]] = []
    original = runner.create_run

    async def wrapped(*args: Any, **kw: Any) -> str:
        calls.append(kw)
        return await original(*args, **kw)

    monkeypatch.setattr(runner, "create_run", wrapped)
    return calls


def _user(idp: IdP, name: str, **extra: Any) -> dict[str, str]:
    return bearer(idp.token({"sub": f"u-{name}"} | extra))


def _pending_run(client: Any, initiator: dict[str, str]) -> tuple[str, dict[str, str]]:
    """以 initiator 发起一次高风险写（demo 的 ticket.close_batch），挂起待审批；返回 run_id 与审批请求体。"""
    body = {"agent_id": "demo", "messages": CLOSE}
    with client.stream("POST", "/v1/messages", json=body, headers=initiator) as resp:
        assert resp.status_code == 200
        evs = _sse_events(resp)
    card = next(e for e in evs if e["type"] == "card")
    return evs[-1]["run_id"], {"call_id": card["payload"]["call_id"], "params_hash": card["payload"]["params_hash"]}


def _resume(client: Any, run_id: str, headers: dict[str, str], msgs: list[dict[str, Any]] = CLOSE) -> tuple[int, list[dict[str, Any]]]:
    with client.stream("POST", f"/v1/runs/{run_id}/resume", json={"messages": msgs}, headers=headers) as resp:
        return resp.status_code, (_sse_events(resp) if resp.status_code == 200 else [])


# ---------------- D87 续跑者 / D90 Run 读取（强制鉴权） ----------------


def test_resume_is_initiator_only_and_read_is_limited(client: Any, idp: IdP, drive_calls: list[dict[str, Any]]) -> None:
    alice, bob, carol = _user(idp, "alice"), _user(idp, "bob"), _user(idp, "carol")
    admin = _user(idp, "root", realm_access={"roles": ["platform-admin"]})
    app = bearer(idp.token(app_claims()))
    dave = _user(idp, "dave", tenant="t2")
    run_id, approval = _pending_run(client, alice)
    drive_calls.clear()  # 只看续跑的 drive 调用
    urls = (f"/v1/runs/{run_id}", f"/v1/runs/{run_id}/events")

    # 待审批：有审批资格的同租户人员可读审批视图；应用令牌没有审批资格；其他租户 404
    assert client.get(urls[0], headers=bob).json()["state"] == "auth-required"
    pending_view = {e["type"] for e in client.get(urls[1], headers=bob).json()}
    assert "tool.decision" in pending_view and pending_view <= set(messages.APPROVAL_VIEW_EVENTS)
    assert "context.assembled" in {e["type"] for e in client.get(urls[1], headers=alice).json()} - pending_view
    assert all(client.get(u, headers=app).status_code == 403 for u in urls)
    assert all(client.get(u, headers=dave).status_code == 404 for u in urls)

    assert client.post(f"/v1/runs/{run_id}/approve", json=approval, headers=bob).status_code == 200

    # 审批后：审批人、管理员、第三人都不能续跑（403），其他租户 404；第三人不能读
    assert all(client.get(u, headers=carol).status_code == 403 for u in urls)
    for who in (bob, admin, carol):
        assert _resume(client, run_id, who)[0] == 403
    assert _resume(client, run_id, dave)[0] == 404
    assert not drive_calls

    # 发起人续跑：工具以发起人令牌执行，credential_subject 即发起人（不写入事件）
    status, evs = _resume(client, run_id, alice)
    assert status == 200 and evs[-1]["type"] == "done"
    (call,) = drive_calls
    assert call["credential_subject"] == call["subject"] == "human:u-alice"
    assert call["credential"]  # 发起人本人的访问令牌
    log = client.get(urls[1], headers=alice).json()
    assert all("credential_subject" not in e["payload"] for e in log if e["type"] == "tool.executing")

    # 发起人 / 审批人 / 管理员可读全部
    for who in (alice, bob, admin):
        assert client.get(urls[0], headers=who).json()["state"] == "completed"
        assert "tool.executed" in {e["type"] for e in client.get(urls[1], headers=who).json()}
    assert all(client.get(u, headers=carol).status_code == 403 for u in urls)


def test_approval_view_is_narrowed_to_pending_call(client: Any, idp: IdP) -> None:
    """审批视图（D90 收窄）：只有待批调用的 tool.requested（去掉 reasoning 等）与其 tool.decision、run.created（只留元数据）、
    run.state_changed；approve 后审批人升级为完整读者。"""
    alice, bob = _user(idp, "alice2"), _user(idp, "bob2")
    admin = _user(idp, "root2", realm_access={"roles": ["platform-admin"]})
    name = f"s5-view-{uuid.uuid4().hex[:6]}"
    turns = [
        _tool_turn("echo", {"text": "先查"}, "c-echo"),
        _tool_turn("ticket.close_batch", {"ids": ["T-1"]}, "c-close"),
        _final(),
    ]
    registry.register(name, ScriptedHarness(name, turns))
    body = {"agent_id": name, "version": "1", "harness": name, "system_prompt": "x", "config": {"tools": ["echo", "ticket.close_batch"]}}
    assert client.post("/admin/agents", json=body, headers=admin).status_code == 201
    assert client.post(f"/admin/agents/{name}/versions/1/publish", json={"eval_passed": True}, headers=admin).status_code == 200
    with client.stream(
        "POST", "/v1/messages", json={"agent_id": name, "messages": [{"role": "user", "content": "关单"}]}, headers=alice
    ) as r:
        evs = _sse_events(r)
    run_id = evs[-1]["run_id"]
    card = next(e for e in evs if e["type"] == "card")
    full = client.get(f"/v1/runs/{run_id}/events", headers=alice).json()
    assert {"c-echo", "c-close"} <= {e["payload"].get("call_id") for e in full}

    view = client.get(f"/v1/runs/{run_id}/events", headers=bob).json()
    assert {e["type"] for e in view} == {"run.created", "run.state_changed", "tool.requested", "tool.decision"}
    assert {e["payload"]["call_id"] for e in view if e["type"] in ("tool.requested", "tool.decision")} == {"c-close"}
    (req,) = [e["payload"] for e in view if e["type"] == "tool.requested"]
    assert set(req) <= {"call_id", "tool", "input", "effective_input", "risk_level"} and req["input"] == {"ids": ["T-1"]}
    (created,) = [e["payload"] for e in view if e["type"] == "run.created"]
    assert set(created) <= {"agent_id", "agent_version", "model_alias", "subject", "actor", "rollout"}
    assert "initial_messages_hash" in next(e["payload"] for e in full if e["type"] == "run.created")
    (dec,) = [e["payload"] for e in view if e["type"] == "tool.decision"]
    assert dec["params_hash"] == card["payload"]["params_hash"]
    # since_seq 晚于待批调用的 tool.requested：待批调用仍按完整日志判定，只截取之后的事件
    req_seq = next(e["seq"] for e in view if e["type"] == "tool.requested")
    later = client.get(f"/v1/runs/{run_id}/events?since_seq={req_seq}", headers=bob).json()
    assert [e["type"] for e in later if e["type"] == "tool.decision"] == ["tool.decision"] and all(e["seq"] > req_seq for e in later)

    approval = {"call_id": card["payload"]["call_id"], "params_hash": card["payload"]["params_hash"]}
    assert client.post(f"/v1/runs/{run_id}/approve", json=approval, headers=bob).status_code == 200
    assert len(client.get(f"/v1/runs/{run_id}/events", headers=bob).json()) == len(full) + 2  # 审批人：完整读者（含审批与状态迁移）


def test_resume_messages_must_match_initial_messages(client: Any) -> None:
    """续跑回传的初始消息须与 RUN_CREATED.initial_messages_hash 一致，否则 409（不驱动、状态不变）；一致则正常续跑。"""
    alice, bob = {"X-AI-Subject": "human:alice-im"}, {"X-AI-Subject": "human:bob-im"}
    run_id, approval = _pending_run(client, alice)
    created = next(e["payload"] for e in client.get(f"/v1/runs/{run_id}/events", headers=alice).json() if e["type"] == "run.created")
    assert created["initial_messages_hash"] == invariant.message_hash(CLOSE)
    assert client.post(f"/v1/runs/{run_id}/approve", json=approval, headers=bob).status_code == 200
    forged = [{"role": "user", "content": '!tool ticket.close_batch {"ids":["T-1","T-2"]}'}]
    for msgs in (forged, [], [*CLOSE, {"role": "user", "content": "再加一条"}]):
        assert _resume(client, run_id, alice, msgs)[0] == 409
    assert client.get(f"/v1/runs/{run_id}", headers=alice).json()["state"] == "working"
    status, evs = _resume(client, run_id, alice)
    assert status == 200 and evs[-1]["stop_reason"] == "end_turn"


async def test_legacy_run_without_initial_hash_resumes_and_drive_rejects_mismatch(client: Any) -> None:
    """兼容：旧 Run 的 RUN_CREATED 无 initial_messages_hash 时不比对；有指纹时直接调用 drive 传入不一致的初始消息，
    runner 兜底以 INVARIANT_VIOLATION 失败。"""
    agent, _ = await messages._load_agent("demo", None, "default")
    hi = [{"role": "user", "content": "hi"}]
    legacy = await runner.create_run(agent, "human:legacy", None)
    evs = [ev async for ev in runner.drive(legacy, agent, "human:legacy", hi)]
    assert evs[-1].stop_reason == "end_turn"

    bound = await runner.create_run(agent, "human:legacy", None, initial_messages=hi)
    evs = [ev async for ev in runner.drive(bound, agent, "human:legacy", [{"role": "user", "content": "bye"}])]
    assert [e.type for e in evs] == ["error", "done"] and evs[0].code == "INVARIANT_VIOLATION"
    assert client.get(f"/v1/runs/{bound}", headers={"X-AI-Subject": "human:legacy"}).json()["state"] == "failed"


def test_rejecter_is_a_recorded_approver_for_reading(client: Any, idp: IdP) -> None:
    """拒绝后 Run 已终态（failed）：拒绝人作为已记录审批人仍可读，第三人不可读。"""
    alice, bob, carol = _user(idp, "alice3"), _user(idp, "bob3"), _user(idp, "carol3")
    run_id, approval = _pending_run(client, alice)
    assert client.post(f"/v1/runs/{run_id}/reject", json=approval, headers=bob).status_code == 200
    assert client.get(f"/v1/runs/{run_id}/events", headers=bob).status_code == 200
    assert client.get(f"/v1/runs/{run_id}", headers=carol).status_code == 403


def test_cancel_is_limited_to_initiator_approver_admin(client: Any, idp: IdP) -> None:
    """取消（D90）：第三人与待审批期间仅有审批资格者 403，其他租户 404；发起人、管理员、已记录审批人可取消。"""
    alice, bob, carol = _user(idp, "alice4"), _user(idp, "bob4"), _user(idp, "carol4")
    admin = _user(idp, "root4", realm_access={"roles": ["platform-admin"]})
    dave = _user(idp, "dave4", tenant="t2")

    run_id, _ = _pending_run(client, alice)
    assert client.post(f"/v1/runs/{run_id}/cancel", headers=carol).status_code == 403  # 有审批资格但未审批
    assert client.post(f"/v1/runs/{run_id}/cancel", headers=dave).status_code == 404
    r = client.post(f"/v1/runs/{run_id}/cancel", headers=alice)
    assert r.status_code == 200 and r.json()["state"] == "canceled"

    run_id, _ = _pending_run(client, alice)
    assert client.post(f"/v1/runs/{run_id}/cancel", headers=admin).status_code == 200

    run_id, approval = _pending_run(client, alice)
    assert client.post(f"/v1/runs/{run_id}/approve", json=approval, headers=bob).status_code == 200
    assert client.post(f"/v1/runs/{run_id}/cancel", headers=carol).status_code == 403
    assert client.post(f"/v1/runs/{run_id}/cancel", headers=bob).status_code == 200
    assert client.get(f"/v1/runs/{run_id}", headers=alice).json()["state"] == "canceled"


def test_non_initiator_resume_is_refused_in_dev_mode(client: Any, drive_calls: list[dict[str, Any]]) -> None:
    """续跑者限制按主体判定，开发态（身份头）同样生效：审批人与第三人 403，发起人可续跑。"""
    alice, bob, carol = ({"X-AI-Subject": f"human:{n}-dev"} for n in ("alice", "bob", "carol"))
    run_id, approval = _pending_run(client, alice)
    assert client.post(f"/v1/runs/{run_id}/approve", json=approval, headers=bob).status_code == 200
    assert _resume(client, run_id, carol)[0] == 403
    assert _resume(client, run_id, bob)[0] == 403
    status, _ = _resume(client, run_id, alice)
    # S7：审批后由 Worker 以发起人身份续跑（开发态无令牌，不存凭据），/resume 尾随至完成
    assert status == 200 and drive_calls[-1]["run_id"] == run_id and drive_calls[-1]["subject"] == "human:alice-dev"


# ---------------- D88 附件按源工件分级 ----------------

H = {"X-AI-Subject": "human:s5-attach"}


def _chat_with(client: Any, ref: str) -> None:
    body = {"agent_id": "demo", "messages": [{"role": "user", "content": "看附件"}], "attachments": [ref], "stream": False}
    assert client.post("/v1/messages", json=body, headers=H).status_code == 200


def _source_run(ref: str) -> str:
    hexid = ref.removeprefix("artifact://")
    return next(r for r in os.listdir(settings.workspace_dir) if os.path.isfile(os.path.join(settings.workspace_dir, r, f"{hexid}.txt")))


def test_attachment_inherits_internal_class(client: Any, create_calls: list[dict[str, Any]]) -> None:
    ref = _offloaded_ref(client, H)  # ticket.search：internal
    src_run = _source_run(ref)
    create_calls.clear()
    _chat_with(client, ref)
    hexid = ref.removeprefix("artifact://")
    assert create_calls[-1]["attachments"] == [{"artifact_id": hexid, "source_run_id": src_run, "data_class": "internal"}]


def test_attachment_from_confidential_tool_is_confidential(
    client: Any, monkeypatch: pytest.MonkeyPatch, create_calls: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(toolbroker, "settings", dataclasses.replace(toolbroker.settings, tool_result_offload_tokens=1))
    name = f"s5-conf-{uuid.uuid4().hex[:6]}"
    harness = ScriptedHarness(name, [_tool_turn("data.query", {"sql": "select 1"}, "c1"), _final()])
    _publish(client, name, harness, {"tools": ["data.query"]})
    body = {"agent_id": name, "messages": [{"role": "user", "content": "查"}], "stream": False}
    run_id = client.post("/v1/messages", json=body, headers=H).json()["run_id"]
    result = next(e["payload"] for e in client.get(f"/v1/runs/{run_id}/events", headers=H).json() if e["type"] == "tool.result")
    assert result["data_class"] == "confidential" and result["output_ref"]
    create_calls.clear()
    _chat_with(client, result["output_ref"])
    assert create_calls[-1]["attachments"][0]["data_class"] == "confidential"
    assert create_calls[-1]["attachments"][0]["source_run_id"] == run_id


def test_internal_artifact_written_under_confidential_taint_is_confidential(
    client: Any, monkeypatch: pytest.MonkeyPatch, create_calls: list[dict[str, Any]]
) -> None:
    """D88 防洗白：Run 已带 confidential 污点后，internal 工具（ticket.search）的外置工件按两者较高者记
    artifact_data_class=confidential，附进新 Run 时继承 confidential；未带污点的 Run 中同一工具的工件仍是 internal。"""
    monkeypatch.setattr(toolbroker, "settings", dataclasses.replace(toolbroker.settings, tool_result_offload_tokens=1))
    name = f"s5-wash-{uuid.uuid4().hex[:6]}"
    turns = [_tool_turn("data.query", {"sql": "select 1"}, "c1"), _tool_turn("ticket.search", {"q": "x"}, "c2"), _final()]
    _publish(client, name, ScriptedHarness(name, turns), {"tools": ["data.query", "ticket.search"]})
    body = {"agent_id": name, "messages": [{"role": "user", "content": "查"}], "stream": False}
    run_id = client.post("/v1/messages", json=body, headers=H).json()["run_id"]
    results = {
        e["payload"]["call_id"]: e["payload"]
        for e in client.get(f"/v1/runs/{run_id}/events", headers=H).json()
        if e["type"] == "tool.result"
    }
    internal = results["c2"]
    assert internal["data_class"] == "internal" and internal["artifact_data_class"] == "confidential" and internal["output_ref"]
    assert "artifact_data_class" not in results["c1"]  # 工具本身即 confidential：不额外记录
    create_calls.clear()
    _chat_with(client, internal["output_ref"])
    assert create_calls[-1]["attachments"][0]["data_class"] == "confidential"

    clean = ScriptedHarness(name + "-c", [_tool_turn("ticket.search", {"q": "x"}, "c3"), _final()])
    _publish(client, name + "-c", clean, {"tools": ["ticket.search"]})
    run2 = client.post("/v1/messages", json=body | {"agent_id": name + "-c"}, headers=H).json()["run_id"]
    res = next(e["payload"] for e in client.get(f"/v1/runs/{run2}/events", headers=H).json() if e["type"] == "tool.result")
    assert "artifact_data_class" not in res
    create_calls.clear()
    _chat_with(client, res["output_ref"])
    assert create_calls[-1]["attachments"][0]["data_class"] == "internal"


def test_attachment_without_classification_record_is_confidential(client: Any, create_calls: list[dict[str, Any]]) -> None:
    """工作区里有文件、但源 Run 事件日志中查不到产生它的记录：按 confidential（失败关闭）。"""
    ref = _offloaded_ref(client, H)
    src_run = _source_run(ref)
    orphan = uuid.uuid4().hex
    with open(os.path.join(settings.workspace_dir, src_run, f"{orphan}.txt"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"x": 1}))
    create_calls.clear()
    _chat_with(client, f"artifact://{orphan}")
    assert create_calls[-1]["attachments"] == [{"artifact_id": orphan, "source_run_id": src_run, "data_class": "confidential"}]


def test_artifact_data_class_lookup_rules() -> None:
    """分级查找：TOOL_RESULT 的 output_ref / RUN_CREATED.attachments 两种来源；多条取最高级；缺失或未知按 confidential。"""
    a, b = "a" * 32, "b" * 32

    def ev(seq: int, type_: RunEventType, payload: dict[str, Any]) -> RunEvent:
        return RunEvent(run_id="r", seq=seq, type=type_, payload=payload)

    created = ev(1, RunEventType.RUN_CREATED, {"attachments": [{"artifact_id": b, "source_run_id": "r0", "data_class": "internal"}]})
    result = ev(2, RunEventType.TOOL_RESULT, {"call_id": "c1", "status": "ok", "output_ref": f"artifact://{a}", "data_class": "public"})
    assert messages._artifact_data_class([created, result], a) == "public"
    assert messages._artifact_data_class([created, result], b) == "internal"
    assert messages._artifact_data_class([created, result], "c" * 32) == "confidential"
    legacy = ev(3, RunEventType.TOOL_RESULT, {"call_id": "c2", "status": "ok", "output_ref": f"artifact://{a}"})  # 旧事件无 data_class
    assert messages._artifact_data_class([result, legacy], a) == "confidential"
    upgraded = ev(4, RunEventType.TOOL_RESULT, {"call_id": "c3", "status": "ok", "output_ref": a, "data_class": "confidential"})
    assert messages._artifact_data_class([result, upgraded], a) == "confidential"
    tainted = ev(
        5,
        RunEventType.TOOL_RESULT,
        {"call_id": "c4", "status": "ok", "output_ref": b, "data_class": "internal", "artifact_data_class": "confidential"},
    )
    assert messages._artifact_data_class([tainted], b) == "confidential"  # 外置时 Run 已有污点：取较高者

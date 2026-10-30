"""S7 WP-D Run API：列表分页 / 过滤 / 越权、事件流、/input 各类型与异常映射、审批后自动入队、/resume 抢不到租约时尾随、
跨节点取消转发（fakeredis）、终态删凭据、发布时 budget / user_input 配置闸门。

会话级 TestClient 内有进程内 Worker（InMemoryQueue）：POST /v1/runs 与 /input 入队后由它执行。强制鉴权的用例复用 test_auth 的
测试 IdP（令牌现场签发，发起人令牌加密存 t_run_credential）。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from test_auth import AUDIENCE, ISSUER, JWKS_URL, IdP, bearer

from ai_contract import RunState
from runtime.api import auth, messages
from runtime.loop import toolbroker, user_input
from runtime.state import db
from runtime.state.db import RunCredential, RunInput
from runtime.state.eventlog import RunNotFound, SeqConflict, acquire_lease, fence_scope, release_lease

fakeredis = pytest.importorskip("fakeredis")

DEV = {"X-AI-Subject": "human:s7-api"}
CLOSE = [{"role": "user", "content": '!tool ticket.close_batch {"ids":["T-1"]}'}]
SCHEMA = {"type": "object", "properties": {"id": {"type": "string", "minLength": 2}}, "required": ["id"], "additionalProperties": False}


@pytest.fixture
def idp(monkeypatch: pytest.MonkeyPatch) -> Iterator[IdP]:
    """开启强制鉴权（同 test_auth.idp）。"""
    provider = IdP()
    strict = dataclasses.replace(auth.settings, auth_required=True, jwks_url=JWKS_URL, jwt_issuer=ISSUER, jwt_audience=AUDIENCE)
    monkeypatch.setattr(auth, "settings", strict)
    auth.set_transport_for_tests(provider.transport())
    yield provider
    auth.set_transport_for_tests(None)


def _user(idp: IdP, name: str, **claims: Any) -> dict[str, str]:
    return bearer(idp.token({"sub": f"u-{name}"} | claims))


def _admin(idp: IdP) -> dict[str, str]:
    return _user(idp, "root", realm_access={"roles": ["platform-admin"]})


def _publish(client: Any, config: dict[str, Any], headers: dict[str, str] | None = None) -> str:
    name = f"s7api-{uuid.uuid4().hex[:8]}"
    body = {"agent_id": name, "version": "1", "system_prompt": "x", "config": config}
    assert client.post("/admin/agents", json=body, headers=headers or {}).status_code == 201
    r = client.post(f"/admin/agents/{name}/versions/1/publish", json={"eval_passed": True}, headers=headers or {})
    assert r.status_code == 200, r.text
    return name


def _create(client: Any, agent: str, content: str, headers: dict[str, str], **extra: Any) -> str:
    r = client.post("/v1/runs", json={"agent_id": agent, "input": [{"role": "user", "content": content}]} | extra, headers=headers)
    assert r.status_code == 202, r.text
    assert r.json()["state"] == "submitted"
    return str(r.json()["run_id"])


def _wait(client: Any, run_id: str, headers: dict[str, str], *states: str, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    run: dict[str, Any] = {}
    while time.time() < deadline:
        run = client.get(f"/v1/runs/{run_id}", headers=headers).json()
        if run.get("state") in states:
            return run
        time.sleep(0.03)
    pytest.fail(f"run {run_id} did not reach {states}: {run}")


def _log(client: Any, run_id: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    return list(client.get(f"/v1/runs/{run_id}/events", headers=headers).json())


def _frames(resp: Any) -> list[dict[str, Any]]:
    """SSE 帧：[{event, id, data}]（data 已解析为 JSON）。"""
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] = {}
    for line in resp.iter_lines():
        if not line:
            if "data" in cur:
                out.append(cur)
            cur = {}
        elif line.startswith("event:"):
            cur["event"] = line[6:].strip()
        elif line.startswith("id:"):
            cur["id"] = line[3:].strip()
        elif line.startswith("data:"):
            cur["data"] = json.loads(line[5:].strip())
    if "data" in cur:
        out.append(cur)
    return out


async def _db(fn: Callable[[Any], Any]) -> Any:
    async with db.SessionLocal() as s:
        return await fn(s)


async def _credential_row(run_id: str) -> RunCredential | None:
    return await _db(lambda s: s.get(RunCredential, run_id))


# ---------------- 创建与列表 ----------------


async def test_create_run_is_durable_with_stored_input_and_credential(client: Any, idp: IdP) -> None:
    """POST /v1/runs：durable=1、session_id、t_run_input 与发起人凭据同事务写入；入队后由 Worker 执行完成，终态删凭据。"""
    alice = _user(idp, "alice-c")
    run_id = _create(client, "demo", "你好", alice, session_id="sess-c")
    _wait(client, run_id, alice, "completed")
    async with db.SessionLocal() as s:
        run = await s.get(db.Run, run_id)
        stored = await s.get(RunInput, run_id)
    assert run is not None and run.durable and run.session_id == "sess-c" and run.lease_owner is None
    assert stored is not None and stored.messages == [{"role": "user", "content": "你好"}]
    created = next(e for e in _log(client, run_id, alice) if e["type"] == "run.created")
    assert created["payload"]["initial_messages_hash"] == stored.fingerprint
    assert await _credential_row(run_id) is None  # 终态即删

    # /v1/messages 同样持久化：挂起（非终态）时凭据仍在
    with client.stream("POST", "/v1/messages", json={"agent_id": "demo", "messages": CLOSE}, headers=alice) as r:
        pending = r.headers["x-run-id"]
        evs = _frames(r)
    assert evs[-1]["data"]["stop_reason"] == "awaiting"
    row = await _credential_row(pending)
    assert row is not None and row.subject == "human:u-alice-c" and b"alice" not in row.ciphertext
    assert await _db(lambda s: s.get(RunInput, pending)) is not None


def test_list_runs_pagination_filters_and_ownership(client: Any, idp: IdP) -> None:
    alice, bob, admin = _user(idp, "alice-l"), _user(idp, "bob-l"), _admin(idp)
    dave = _user(idp, "dave-l", tenant="t2")
    other_agent = _publish(client, {"tools": ["echo"]}, admin)
    mine = [_create(client, "demo", f"m{i}", alice, session_id="sess-1" if i < 3 else "sess-2") for i in range(5)]
    mine.append(_create(client, other_agent, "x", alice, session_id="sess-2"))
    bobs = _create(client, "demo", "b", bob)
    for rid in mine:
        _wait(client, rid, alice, "completed")

    # 键集分页：最新在前、不重不漏，游标不透明
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        q = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        page = client.get("/v1/runs", params=q, headers=alice).json()
        seen += [r["run_id"] for r in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert "run_" not in cursor
    assert seen == list(reversed(mine)) and bobs not in seen  # 最新在前、不重不漏、只有本人的
    assert pages == 3

    def ids(params: dict[str, Any], who: dict[str, str] = alice) -> list[str]:
        r = client.get("/v1/runs", params=params | {"limit": 100}, headers=who)
        assert r.status_code == 200, r.text
        return [x["run_id"] for x in r.json()["items"]]

    assert ids({"session_id": "sess-1"}) == list(reversed(mine[:3]))
    assert set(ids({"session_id": "sess-2"})) == set(mine[3:])
    assert ids({"agent_id": other_agent}) == [mine[-1]]
    assert set(mine) <= set(ids({"state": "completed"})) and not set(mine) & set(ids({"state": "failed"}))
    item = client.get("/v1/runs", params={"session_id": "sess-1", "limit": 1}, headers=alice).json()["items"][0]
    assert item["session_id"] == "sess-1" and item["subject"] == "human:u-alice-l"

    # 越权：非管理员只能看自己（指定他人 403）；管理员看本租户全部并可按 subject 过滤；其他租户看不到
    assert ids({}, bob) == [bobs]
    assert client.get("/v1/runs", params={"subject": "human:u-alice-l"}, headers=bob).status_code == 403
    assert set(mine) | {bobs} <= set(ids({}, admin))
    assert set(ids({"subject": "human:u-alice-l"}, admin)) == set(mine)
    assert ids({}, dave) == []
    assert client.get(f"/v1/runs/{mine[0]}", headers=dave).status_code == 404
    assert client.get(f"/v1/runs/{mine[0]}/stream", headers=dave).status_code == 404
    assert client.get(f"/v1/runs/{mine[0]}/stream", headers=bob).status_code == 403
    # 参数校验
    assert client.get("/v1/runs", params={"limit": 101}, headers=alice).status_code == 422
    assert client.get("/v1/runs", params={"cursor": "not-a-cursor"}, headers=alice).status_code == 422
    assert client.get("/v1/runs", params={"state": "bogus"}, headers=alice).status_code == 422


def test_stream_replays_log_and_ends_with_done(client: Any) -> None:
    """GET /stream：补发 since_seq 之后的事件（SSE id 为 seq），Run 终态后以 done 收尾；Last-Event-ID 续读。"""
    run_id = _create(client, "demo", "你好", DEV)
    _wait(client, run_id, DEV, "completed")
    log = _log(client, run_id, DEV)
    with client.stream("GET", f"/v1/runs/{run_id}/stream", headers=DEV) as r:
        assert r.status_code == 200
        frames = _frames(r)
    assert [f["data"]["seq"] for f in frames[:-1]] == [e["seq"] for e in log]
    assert [f["id"] for f in frames[:-1]] == [str(e["seq"]) for e in log]
    assert frames[-1]["event"] == "done" and frames[-1]["data"]["stop_reason"] == "end_turn" and frames[-1]["data"]["trace_id"]
    with client.stream("GET", f"/v1/runs/{run_id}/stream", params={"since_seq": 3}, headers=DEV) as r:
        assert [f["data"]["seq"] for f in _frames(r)[:-1]] == [e["seq"] for e in log if e["seq"] > 3]
    with client.stream("GET", f"/v1/runs/{run_id}/stream", headers=DEV | {"Last-Event-ID": str(log[-2]["seq"])}) as r:
        assert [f["data"]["seq"] for f in _frames(r)[:-1]] == [log[-1]["seq"]]


async def test_stream_follows_running_run_and_approval_view(client: Any, idp: IdP) -> None:
    """尾随进行中的 Run：待审批期间有审批资格者只收审批视图，拒绝后（Run 终态）流以 done(error) 结束。"""
    alice, carol = _user(idp, "alice-s"), _user(idp, "carol-s")
    with client.stream("POST", "/v1/messages", json={"agent_id": "demo", "messages": CLOSE}, headers=alice) as r:
        run_id = r.headers["x-run-id"]
        card = next(f["data"] for f in _frames(r) if f["event"] == "card")

    def follow() -> list[dict[str, Any]]:
        with client.stream("GET", f"/v1/runs/{run_id}/stream", headers=carol) as resp:
            assert resp.status_code == 200
            return _frames(resp)

    task = asyncio.create_task(asyncio.to_thread(follow))
    await asyncio.sleep(0.5)
    assert not task.done()  # Run 仍待审批：流保持
    approval = {"call_id": card["payload"]["call_id"], "params_hash": card["payload"]["params_hash"]}
    assert client.post(f"/v1/runs/{run_id}/reject", json=approval, headers=carol).status_code == 200
    frames = await asyncio.wait_for(task, 10)
    assert {f["event"] for f in frames[:-1]} <= set(messages.APPROVAL_VIEW_EVENTS)
    assert frames[-1]["event"] == "done" and frames[-1]["data"]["trace_id"] is None


# ---------------- /input ----------------


def test_input_answer_validation_and_resume(client: Any) -> None:
    agent = _publish(client, {"tools": ["echo"], "user_input": True})
    ask = "!tool request_user_input " + json.dumps({"prompt": "哪个工单？", "schema": SCHEMA}, ensure_ascii=False)
    run_id = _create(client, agent, ask, DEV)
    _wait(client, run_id, DEV, "input-required")
    call_id = next(e for e in _log(client, run_id, DEV) if e["type"] == "input.requested")["payload"]["call_id"]
    url = f"/v1/runs/{run_id}/input"
    other = {"X-AI-Subject": "human:s7-other"}
    assert client.post(url, json={"kind": "answer", "call_id": call_id, "value": {"id": "T-1"}}, headers=other).status_code == 403
    assert client.post(url, json={"kind": "answer", "call_id": call_id, "value": {"x": 1}}, headers=DEV).status_code == 400
    assert client.post(url, json={"kind": "answer", "call_id": call_id, "value": {"id": "T"}}, headers=DEV).status_code == 400
    assert client.post(url, json={"kind": "answer", "call_id": "nope", "value": {"id": "T-1"}}, headers=DEV).status_code == 409
    assert client.post(url, json={"kind": "reconcile", "call_id": call_id, "action": "done"}, headers=DEV).status_code == 409
    assert client.post(url, json={"kind": "bogus"}, headers=DEV).status_code == 422
    assert (
        client.post(url, json={"kind": "answer", "call_id": call_id, "value": {"id": "T-1"}}, headers={"X-AI-Tenant": "t9"}).status_code
        == 404
    )
    r = client.post(url, json={"kind": "answer", "call_id": call_id, "value": {"id": "T-1001"}}, headers=DEV)
    assert r.status_code == 200 and r.json() == {"run_id": run_id, "state": "working", "kind": "answer", "call_id": call_id}
    _wait(client, run_id, DEV, "completed")  # 入队后由 Worker 续跑完成
    log = _log(client, run_id, DEV)
    result = next(e for e in log if e["type"] == "tool.result" and e["payload"]["call_id"] == call_id)
    assert result["payload"]["output"] == {"id": "T-1001"}
    assert client.post(url, json={"kind": "answer", "call_id": call_id, "value": {"id": "T-1001"}}, headers=DEV).status_code == 409


def test_input_reconcile_after_unknown_result(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def crash(tool: Any, tool_input: dict[str, Any], dry_run: bool = False) -> Any:
        calls.append(tool_input)
        raise RuntimeError("connection reset mid-write")

    monkeypatch.setattr(toolbroker, "_dev_execute", crash)
    agent = _publish(client, {"tools": ["mail.send"]})
    run_id = _create(client, agent, '!tool mail.send {"to": "a@b"}', DEV)
    _wait(client, run_id, DEV, "input-required")
    executing = next(e for e in _log(client, run_id, DEV) if e["type"] == "tool.executing")
    call_id = executing["payload"]["call_id"]
    url = f"/v1/runs/{run_id}/input"
    assert client.post(url, json={"kind": "reconcile", "call_id": call_id, "action": "redo"}, headers=DEV).status_code == 422
    r = client.post(url, json={"kind": "reconcile", "call_id": call_id, "action": "retry"}, headers=DEV)
    assert r.status_code == 400 and "idempotency" in r.json()["detail"] and r.json()["code"] == "INPUT_INVALID"
    r = client.post(url, json={"kind": "reconcile", "call_id": call_id, "action": "done", "value": {"sent": True}}, headers=DEV)
    assert r.status_code == 200 and r.json()["kind"] == "reconcile"
    _wait(client, run_id, DEV, "completed")
    log = _log(client, run_id, DEV)
    executed = [e for e in log if e["type"] == "tool.executed"]
    assert len(executed) == 1 and executed[0]["idempotency_key"] == executing["payload"]["operation_id"] and len(calls) == 1


async def test_input_credential_refresh(client: Any, idp: IdP) -> None:
    """发起人令牌剩余有效期不足：Worker 挂起为 input-required{credential}；他人令牌 403、仍将过期的令牌 400，
    发起人新令牌刷新后入队完成。"""
    expiring = bearer(idp.token({"sub": "u-alice-k", "exp": int(time.time()) + 30}))
    fresh, bob = _user(idp, "alice-k"), _user(idp, "bob-k")
    run_id = _create(client, "demo", "你好", expiring)
    _wait(client, run_id, fresh, "input-required")
    req = next(e for e in _log(client, run_id, fresh) if e["type"] == "input.requested")
    assert req["payload"] == {"kind": "credential", "call_id": None, "reason": "expiring"}
    url = f"/v1/runs/{run_id}/input"
    assert client.post(url, json={"kind": "credential"}, headers=bob).status_code == 403
    assert client.post(url, json={"kind": "credential"}, headers=expiring).status_code == 400
    r = client.post(url, json={"kind": "credential", "value": "ignored-token"}, headers=fresh)
    assert r.status_code == 200 and r.json()["kind"] == "credential"
    _wait(client, run_id, fresh, "completed")
    assert await _credential_row(run_id) is None


def test_input_error_mapping(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _create(client, "demo", "hi", DEV)
    _wait(client, run_id, DEV, "completed")
    url = f"/v1/runs/{run_id}/input"
    assert client.post(url, json={"kind": "answer", "call_id": "c"}, headers=DEV).status_code == 409  # 不在 input-required
    for exc, code in (
        (SeqConflict("x"), 409),
        (RunNotFound(run_id), 404),
        (ValueError("bad"), 400),
        (user_input.InputForbidden("no"), 403),
    ):

        async def boom(*args: Any, exc: Exception = exc, **kw: Any) -> Any:
            raise exc

        monkeypatch.setattr(user_input, "provide_input", boom)
        assert client.post(url, json={"kind": "answer", "call_id": "c"}, headers=DEV).status_code == code


# ---------------- 审批后自动续跑 / resume 尾随 ----------------


def _suspend(client: Any, headers: dict[str, str]) -> tuple[str, dict[str, str]]:
    with client.stream("POST", "/v1/messages", json={"agent_id": "demo", "messages": CLOSE}, headers=headers) as r:
        run_id = r.headers["x-run-id"]
        frames = _frames(r)
    card = next(f["data"] for f in frames if f["event"] == "card")
    assert frames[-1]["data"]["stop_reason"] == "awaiting"
    return run_id, {"call_id": card["payload"]["call_id"], "params_hash": card["payload"]["params_hash"]}


@pytest.mark.auto_resume
def test_approve_enqueues_durable_run_for_automatic_resume(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    reasons: list[tuple[str, str]] = []
    real = messages.enqueue

    async def spy(run_id: str, reason: str = "created") -> None:
        reasons.append((run_id, reason))
        await real(run_id, reason)

    monkeypatch.setattr(messages, "enqueue", spy)
    alice, bob = {"X-AI-Subject": "human:alice-ar"}, {"X-AI-Subject": "human:bob-ar"}
    run_id, approval = _suspend(client, alice)
    r = client.post(f"/v1/runs/{run_id}/approve", json=approval, headers=bob)
    assert r.status_code == 200 and r.json()["state"] == "working"
    assert (run_id, "approved") in reasons
    _wait(client, run_id, alice, "completed")  # 无需 /resume：Worker 以发起人身份续跑
    log = _log(client, run_id, alice)
    assert [e["type"] for e in log].count("tool.executed") == 1
    seqs = [e["seq"] for e in log]
    assert seqs == list(range(1, len(seqs) + 1))


async def test_legacy_non_durable_run_is_not_enqueued_on_approve(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """S7 之前创建的 Run（durable=0、无存储的初始消息）审批后不入队（否则 Worker 会因快照缺失判失败），仍由发起人 /resume。"""
    from runtime.loop import runner

    continued: list[str] = []

    async def spy(run_id: str) -> None:
        continued.append(run_id)

    monkeypatch.setattr(messages, "continue_after_approval", spy)
    agent, _ = await messages._load_agent("demo", None, "default")
    run_id = await runner.create_run(agent, "human:legacy-ap", None, initial_messages=CLOSE)
    evs = [ev async for ev in runner.drive(run_id, agent, "human:legacy-ap", CLOSE)]
    card = next(e for e in evs if e.type == "card")
    approval = {"call_id": card.payload["call_id"], "params_hash": card.payload["params_hash"]}
    assert client.post(f"/v1/runs/{run_id}/approve", json=approval, headers={"X-AI-Subject": "human:x"}).status_code == 200
    assert continued == []
    who = {"X-AI-Subject": "human:legacy-ap"}
    with client.stream("POST", f"/v1/runs/{run_id}/resume", json={}, headers=who) as r:
        assert r.status_code == 409  # 无存储的初始消息时 messages 必填
    with client.stream("POST", f"/v1/runs/{run_id}/resume", json={"messages": CLOSE}, headers=who) as r:
        assert _frames(r)[-1]["data"]["stop_reason"] == "end_turn"


async def test_resume_without_messages_uses_stored_input_and_tails_when_lease_is_held(client: Any) -> None:
    """/resume：messages 可省略（用 t_run_input）；审批后（持久化 Run 已被唤醒过）一律尾随事件流（事件日志行 + done）：
    租约被其他节点持有时由持有者完成执行；无人持有时补投由 Worker 执行，本接口同样只尾随（结果格式确定）。"""
    from runtime.loop import runner

    alice, bob = {"X-AI-Subject": "human:alice-rt"}, {"X-AI-Subject": "human:bob-rt"}
    run_id, approval = _suspend(client, alice)
    assert client.post(f"/v1/runs/{run_id}/approve", json=approval, headers=bob).status_code == 200
    epoch = await acquire_lease(run_id, "other-node", 30)
    assert epoch is not None

    def resume() -> list[dict[str, Any]]:
        with client.stream("POST", f"/v1/runs/{run_id}/resume", headers=alice) as r:
            assert r.status_code == 200
            return _frames(r)

    task = asyncio.create_task(asyncio.to_thread(resume))
    await asyncio.sleep(0.5)
    assert not task.done()  # 尾随中：等持有者执行
    with fence_scope(epoch, run_id):
        [ev async for ev in runner.drive_durable(run_id)]
    assert await release_lease(run_id, "other-node", epoch)
    frames = await asyncio.wait_for(task, 10)
    assert frames[-1]["event"] == "done" and frames[-1]["data"]["stop_reason"] == "end_turn"
    tailed = [f["event"] for f in frames[:-1]]
    assert "tool.executed" in tailed and all("." in t for t in tailed)  # 事件日志行，不是统一事件
    # 从批准后迁回 working 的那条起尾随（整段续跑）
    assert frames[0]["event"] == "run.state_changed" and frames[0]["data"]["payload"] == {
        "from": "auth-required",
        "to": "working",
        "reason": "approved",
    }

    # 无人持有租约（本用例关闭了审批后的自动入队）：/resume 补投，由进程内 Worker 执行，本接口仍是尾随（日志行 + done）
    run2, approval2 = _suspend(client, alice)
    assert client.post(f"/v1/runs/{run2}/approve", json=approval2, headers=bob).status_code == 200
    with client.stream("POST", f"/v1/runs/{run2}/resume", json={}, headers=alice) as r:
        frames = _frames(r)
    assert any(f["event"] == "tool.result" for f in frames) and frames[-1]["data"]["stop_reason"] == "end_turn"
    assert all("." in f["event"] for f in frames[:-1])
    assert (await _db(lambda s: s.get(db.Run, run2))).lease_owner is None
    # 已终态再 /resume：同一段日志 + done，不是 409
    with client.stream("POST", f"/v1/runs/{run2}/resume", json={}, headers=alice) as r:
        assert r.status_code == 200
        again = _frames(r)
    assert [f["event"] for f in again] == [f["event"] for f in frames]


def _resume_frames(client: Any, run_id: str, who: dict[str, str]) -> list[dict[str, Any]]:
    with client.stream("POST", f"/v1/runs/{run_id}/resume", json={}, headers=who) as r:
        assert r.status_code == 200
        return _frames(r)


@pytest.mark.auto_resume
def test_resume_result_is_deterministic_regardless_of_worker_timing(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """审批通过即入队、Worker 自动续跑（D108）；/resume 无论先于 Worker 执行还是在 Worker 完成之后调用，结果都一样：
    自批准迁回 working 起的事件日志行 + done(end_turn)，不再出现"统一事件 / 日志行 / 409"三种结果。"""
    alice, bob = {"X-AI-Subject": "human:alice-rt"}, {"X-AI-Subject": "human:bob-rt"}
    monkeypatch.setenv("MOCK_TURN_DELAY_S", "0.5")  # 让 Worker 的续跑轮次慢一点：/resume 必定先于 Worker 完成

    # 时序一：/resume 先于 Worker 完成
    run_a, approval_a = _suspend(client, alice)
    assert client.post(f"/v1/runs/{run_a}/approve", json=approval_a, headers=bob).status_code == 200
    early = _resume_frames(client, run_a, alice)
    monkeypatch.delenv("MOCK_TURN_DELAY_S")

    # 时序二：Worker 已完成后才 /resume
    run_b, approval_b = _suspend(client, alice)
    assert client.post(f"/v1/runs/{run_b}/approve", json=approval_b, headers=bob).status_code == 200
    _wait(client, run_b, alice, "completed")
    late = _resume_frames(client, run_b, alice)

    for frames in (early, late):
        assert frames[-1]["event"] == "done" and frames[-1]["data"]["stop_reason"] == "end_turn"
        assert frames[0]["event"] == "run.state_changed" and frames[0]["data"]["payload"]["reason"] == "approved"
        assert all("." in f["event"] for f in frames[:-1])  # 只有事件日志行
    assert [f["event"] for f in early] == [f["event"] for f in late]


# ---------------- 取消与凭据 ----------------


async def test_cancel_forwards_across_nodes_and_deletes_credential(client: Any, idp: IdP) -> None:
    """取消：CAS 迁 canceled、删凭据、提交后经 runtime:cancel 广播（fakeredis 上的另一"节点"收到）；已终态 409。"""
    from runtime.worker import control
    from runtime.worker import queue as queue_mod

    alice = _user(idp, "alice-x")
    run_id, _ = _suspend(client, alice)
    assert await _credential_row(run_id) is not None
    server = fakeredis.FakeServer()
    got: list[str] = []
    received = asyncio.Event()

    async def remote_interrupt(rid: str) -> bool:
        got.append(rid)
        received.set()
        return True

    listener = control.CancelListener(fakeredis.FakeAsyncRedis(server=server, decode_responses=True), remote_interrupt)
    task = asyncio.create_task(listener.run())
    saved = (queue_mod._queue, queue_mod._redis)
    queue_mod.configure(saved[0], fakeredis.FakeAsyncRedis(server=server, decode_responses=True))
    try:
        await asyncio.wait_for(listener.ready.wait(), 2)
        r = await asyncio.to_thread(client.post, f"/v1/runs/{run_id}/cancel", headers=alice)
        assert r.status_code == 200 and r.json()["state"] == "canceled"
        await asyncio.wait_for(received.wait(), 2)
    finally:
        queue_mod.configure(*saved)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert got == [run_id]
    assert await _credential_row(run_id) is None
    assert client.post(f"/v1/runs/{run_id}/cancel", headers=alice).status_code == 200  # 已 canceled：幂等
    rid = _create(client, "demo", "hi", alice)
    _wait(client, rid, alice, "completed")
    assert client.post(f"/v1/runs/{rid}/cancel", headers=alice).status_code == 409  # 其他终态：非法迁移


async def test_cancel_retries_once_on_seq_conflict(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from runtime.state import eventlog

    run_id, _ = _suspend(client, DEV)
    real = eventlog.EventLog.transition
    calls = {"n": 0}

    async def flaky(self: Any, rid: str, dst: RunState, reason: str = "") -> RunState:
        calls["n"] += 1
        if calls["n"] == 1:
            raise SeqConflict("concurrent writer")
        return await real(self, rid, dst, reason)

    monkeypatch.setattr(eventlog.EventLog, "transition", flaky)
    r = await asyncio.to_thread(client.post, f"/v1/runs/{run_id}/cancel", headers=DEV)
    assert r.status_code == 200 and calls["n"] == 2
    run2, _ = _suspend(client, DEV)

    async def always(self: Any, rid: str, dst: RunState, reason: str = "") -> RunState:
        raise SeqConflict("concurrent writer")

    monkeypatch.setattr(eventlog.EventLog, "transition", always)
    assert (await asyncio.to_thread(client.post, f"/v1/runs/{run2}/cancel", headers=DEV)).status_code == 409


async def test_reject_deletes_credential(client: Any, idp: IdP) -> None:
    alice, bob = _user(idp, "alice-rj"), _user(idp, "bob-rj")
    run_id, approval = _suspend(client, alice)
    assert await _credential_row(run_id) is not None
    assert client.post(f"/v1/runs/{run_id}/reject", json=approval, headers=bob).status_code == 200
    assert await _credential_row(run_id) is None


# ---------------- 发布闸门 ----------------


@pytest.mark.parametrize(
    "config",
    [
        {"budget": {"max_rounds": 3}},  # 缺双预算主控
        {"budget": {"max_total_tokens": -1}},
        {"budget": {"max_wall_s": 10, "bogus": 1}},
        {"budget": "100"},
        {"user_input": "yes"},
    ],
)
def test_publish_rejects_invalid_budget_or_user_input(client: Any, config: dict[str, Any]) -> None:
    name = f"s7pub-{uuid.uuid4().hex[:8]}"
    assert client.post("/admin/agents", json={"agent_id": name, "version": "1", "config": config}).status_code == 201
    r = client.post(f"/admin/agents/{name}/versions/1/publish", json={"eval_passed": True})
    assert r.status_code == 409 and ("budget" in r.json()["detail"] or "user_input" in r.json()["detail"])


def test_publish_accepts_valid_budget_and_user_input(client: Any) -> None:
    assert _publish(client, {"budget": {"max_total_tokens": 1000, "max_wall_s": 30}, "user_input": True})


def test_isolated_query_run_owner_only_even_for_admin(client: Any, idp: IdP) -> None:
    alice, bob, admin = _user(idp, "query-alice"), _user(idp, "query-bob"), _admin(idp)
    body = {"agent_id": "f4-data-query", "version": "1", "config": {"tools": ["echo"]}}
    assert client.post("/admin/agents", json=body, headers=admin).status_code == 201
    assert client.post("/admin/agents/f4-data-query/versions/1/publish", json={"eval_passed": True}, headers=admin).status_code == 200
    rid = _create(client, "f4-data-query", "synthetic", alice)
    _wait(client, rid, alice, "completed")
    for headers in (bob, admin):
        for suffix in ("", "/events", "/stream"):
            assert client.get(f"/v1/runs/{rid}{suffix}", headers=headers).status_code == 403
        result = client.get("/v1/runs", params={"agent_id": "f4-data-query"}, headers=headers)
        assert result.status_code == 200
        assert result.json() == {"items": [], "next_cursor": None}
    assert client.get(f"/v1/runs/{rid}/events", headers=alice).status_code == 200

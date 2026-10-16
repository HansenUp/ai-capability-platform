"""Messages/Run API 的集成测试：覆盖普通对话流式响应、只读工具执行与大结果外置、
高风险写操作的挂起/审批/续跑闭环、结果未知窗口转人工对账、非法状态迁移拒绝，
以及评测未通过 / harness 能力等级不足时阻断发布（业务规则拒绝统一 409）、
Run API 对不存在的 Run / 非法参数的错误码与响应视图。"""

import json
import time

import pytest


def _events(resp):
    """把 SSE 响应体解析为事件字典列表（按 `data:` 前缀行提取 JSON）。"""
    out = []
    for line in resp.iter_lines():
        if line.startswith("data:"):
            out.append(json.loads(line[5:].strip()))
    return out


def test_plain_chat_streams_and_completes(client):
    """普通对话：应先产出 text 事件，最后以 done 事件收尾，Run 最终状态为 completed，
    且事件日志的前四条与末尾一条符合预期顺序。"""
    with client.stream("POST", "/v1/messages", json={"agent_id": "demo", "messages": [{"role": "user", "content": "你好"}]}) as r:
        assert r.status_code == 200
        run_id = r.headers["x-run-id"]
        evs = _events(r)
    types = [e["type"] for e in evs]
    assert types[0] == "text" and types[-1] == "done"
    run = client.get(f"/v1/runs/{run_id}").json()
    assert run["state"] == "completed"
    log = client.get(f"/v1/runs/{run_id}/events").json()
    assert [e["type"] for e in log][:4] == ["run.created", "run.state_changed", "context.assembled", "model.request"]
    assert log[-1]["type"] == "run.state_changed" and log[-1]["payload"]["to"] == "completed"


def test_read_tool_executes_and_large_output_offloaded(client):
    """只读工具（ticket.search）应直接执行成功，且由于测试环境把 TOOL_RESULT_OFFLOAD_TOKENS
    调得很小，其结果应被外置到工作区，返回 artifact:// 引用。"""
    body = {"agent_id": "demo", "messages": [{"role": "user", "content": '!tool ticket.search {"q":"all"}'}]}
    with client.stream("POST", "/v1/messages", json=body) as r:
        evs = _events(r)
    tr = next(e for e in evs if e["type"] == "tool_result")
    assert tr["status"] == "ok" and tr["output_ref"] and tr["output_ref"].startswith("artifact://")
    assert evs[-1]["stop_reason"] == "end_turn"


def test_high_risk_write_suspends_then_resumes_after_approval(client):
    """高风险写操作（ticket.close_batch）应先挂起等待审批（card 事件 + awaiting + auth-required），
    审批通过后通过 /resume 续跑，最终应看到 tool.approved / tool.executed 事件，且带有唯一幂等键。"""
    msgs = [{"role": "user", "content": '!tool ticket.close_batch {"ids":["T-1","T-2"]}'}]
    with client.stream("POST", "/v1/messages", json={"agent_id": "demo", "messages": msgs}) as r:
        run_id = r.headers["x-run-id"]
        evs = _events(r)
    card = next(e for e in evs if e["type"] == "card")
    assert card["card_type"] == "impact_preview" and card["payload"]["preview"]["impact_preview"]["tickets"] == 2
    assert evs[-1]["stop_reason"] == "awaiting"
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "auth-required"

    ok = client.post(
        f"/v1/runs/{run_id}/approve",
        json={"call_id": card["payload"]["call_id"], "params_hash": card["payload"]["params_hash"]},
    )
    assert ok.status_code == 200 and ok.json()["state"] == "working"

    with client.stream("POST", f"/v1/runs/{run_id}/resume", json={"messages": msgs}) as r:
        evs2 = _events(r)
    assert evs2[-1]["stop_reason"] in ("end_turn", "tool_use")
    log = client.get(f"/v1/runs/{run_id}/events").json()
    types = [e["type"] for e in log]
    assert "tool.approved" in types and "tool.executed" in types
    idem = [e["idempotency_key"] for e in log if e["type"] == "tool.executed"]
    assert len(idem) == 1 and idem[0]


def test_illegal_transition_rejected(client):
    """已完成（completed）的 Run 不允许再迁移到 canceled；取消接口应返回 409。"""
    with client.stream("POST", "/v1/messages", json={"agent_id": "demo", "messages": [{"role": "user", "content": "hi"}]}) as r:
        run_id = r.headers["x-run-id"]
        _events(r)
    assert client.post(f"/v1/runs/{run_id}/cancel").status_code == 409  # completed → canceled is illegal（非法迁移）


def test_publish_blocked_when_eval_fails(client):
    """当评测未通过（eval_passed=False）时，发布接口应被评测闸门阻断，按业务规则拒绝返回 409。"""
    client.post("/admin/agents", json={"agent_id": "bad", "version": "1"})
    r = client.post("/admin/agents/bad/versions/1/publish", json={"eval_passed": False})
    assert r.status_code == 409 and isinstance(r.json()["detail"], str)


def _wait_for(client, run_id, predicate, timeout=5.0):
    """轮询 Run 事件日志直到 predicate(日志) 成立（后台驱动在另一线程的事件循环中执行）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        log = client.get(f"/v1/runs/{run_id}/events").json()
        if predicate(log):
            return log
        time.sleep(0.02)
    pytest.fail(f"timeout waiting for run {run_id}; log={client.get(f'/v1/runs/{run_id}/events').json()}")


def test_unknown_tool_result_window_requires_reconciliation(client, monkeypatch):
    """结果未知窗口：工具已写下 TOOL_EXECUTING 但执行过程中进程崩溃（此处以抛异常模拟），
    续跑时不得重新执行，而应把 Run 迁到 input-required 并推一张对账卡片。"""
    from runtime.loop import toolbroker

    called = []

    async def _crash(tool, tool_input, dry_run=False):
        called.append(tool.name)
        raise RuntimeError("simulated crash after write-ahead log")  # 模拟"执行中崩溃，结果未知"

    monkeypatch.setattr(toolbroker, "execute_tool", _crash)
    body = {"agent_id": "demo", "input": [{"role": "user", "content": '!tool ticket.search {"q":"all"}'}]}
    run_id = client.post("/v1/runs", json=body).json()["run_id"]
    log = _wait_for(client, run_id, lambda log: called and any(e["type"] == "tool.executing" for e in log))
    executing = next(e for e in log if e["type"] == "tool.executing")
    assert executing["payload"]["params_hash"] and not any(e["type"] in ("tool.executed", "tool.result") for e in log)
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "working"

    with client.stream("POST", f"/v1/runs/{run_id}/resume", json={"messages": body["input"]}) as r:
        evs = _events(r)
    card = next(e for e in evs if e["type"] == "card")
    assert card["card_type"] == "reconciliation_required"
    assert card["payload"]["call_id"] == executing["payload"]["call_id"] and card["payload"]["tool"] == "ticket.search"
    assert evs[-1]["stop_reason"] == "awaiting"
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "input-required"

    log = client.get(f"/v1/runs/{run_id}/events").json()
    assert called == ["ticket.search"]  # 结果未知的调用没有被重放执行第二次
    assert not any(e["type"] in ("tool.executed", "tool.result") for e in log)
    assert [e["payload"]["code"] for e in log if e["type"] == "run.error"] == ["UNKNOWN_TOOL_RESULT"]


def test_publish_blocked_when_harness_tier_too_low(client):
    """能力等级闸门：声明了高风险写工具的 Agent，配 L0 档 harness 时发布应被 409 拒绝。

    mock harness 由平台持有循环，属 L2，因此这里注册一个 harness 自持循环且只有 advisory 拦截的
    适配器来构造 L0 场景，验证闸门确实按能力等级而非按 harness 名称判断。
    """
    from ai_contract import HarnessCapabilities, capability_tier
    from runtime.harness import registry

    class _WeakHarness:
        """只声明能力的最小适配器：harness 自持循环 + advisory 拦截 + 不可由事件日志续跑。"""

        def capabilities(self) -> HarnessCapabilities:
            return HarnessCapabilities(name="weak", version="0", transport="stdio", loop_owner="harness", tool_interception="advisory")

    registry.register("weak", _WeakHarness())
    assert capability_tier(_WeakHarness().capabilities()) == "L0"

    client.post(
        "/admin/agents",
        json={"agent_id": "risky", "version": "1", "harness": "weak", "config": {"tools": ["ticket.close_batch"]}},
    )
    r = client.post("/admin/agents/risky/versions/1/publish", json={"eval_passed": True})
    assert r.status_code == 409 and "能力等级" in r.json()["detail"]

    # 同一个 Agent 换成平台持有循环的 mock harness（L2）后可以正常发布
    client.post(
        "/admin/agents",
        json={"agent_id": "risky", "version": "1", "harness": "mock", "config": {"tools": ["ticket.close_batch"]}},
    )
    assert client.post("/admin/agents/risky/versions/1/publish", json={"eval_passed": True}).status_code == 200


# ---------------- Run API 错误码与响应视图 ----------------

H = {"X-AI-Subject": "human:dev"}


def test_get_run_view_fields_unchanged(client):
    """挂上 RunView 后 GET /v1/runs/{id} 的返回字段与原字典完全一致。"""
    with client.stream("POST", "/v1/messages", json={"agent_id": "demo", "messages": [{"role": "user", "content": "hi"}]}) as r:
        run_id = r.headers["x-run-id"]
        _events(r)
    run = client.get(f"/v1/runs/{run_id}").json()
    assert set(run) == {"run_id", "state", "agent_id", "agent_version", "model_alias", "subject", "last_seq", "created_at"}
    assert run["state"] == "completed" and isinstance(run["created_at"], str)
    log = client.get(f"/v1/runs/{run_id}/events").json()
    assert set(log[0]) == {"run_id", "seq", "type", "payload", "idempotency_key", "ts"}
    assert client.get(f"/v1/runs/{run_id}/events", params={"since_seq": run["last_seq"]}).json() == []


def test_unknown_run_returns_404(client):
    """不存在的 Run：查询 / 取消 / 批准 / 拒绝 / 续跑一律 404（不再 500）。"""
    approval = {"call_id": "c", "params_hash": "h"}
    assert client.get("/v1/runs/no-such-run", headers=H).status_code == 404
    assert client.post("/v1/runs/no-such-run/cancel", headers=H).status_code == 404
    assert client.post("/v1/runs/no-such-run/approve", json=approval, headers=H).status_code == 404
    assert client.post("/v1/runs/no-such-run/reject", json=approval, headers=H).status_code == 404
    assert client.post("/v1/runs/no-such-run/resume", json={"messages": []}, headers=H).status_code == 404
    assert client.get("/v1/runs/no-such-run/events", headers=H).json() == []


@pytest.mark.parametrize("since_seq", [-1, 2**53 + 1, 2**63])
def test_events_since_seq_out_of_range_is_422(client, since_seq):
    """since_seq 超出 [0, 2^53] 由 FastAPI 参数校验返回 422（不再因 SQLite 溢出 500）。"""
    r = client.get("/v1/runs/no-such-run/events", params={"since_seq": since_seq}, headers=H)
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)


def test_reject_requires_pending_approval_and_matching_params_hash(client):
    """reject 与 approve 同样的前置校验：非 auth-required 或参数指纹不符 409；校验通过后 Run 置为 failed。"""
    with client.stream("POST", "/v1/messages", json={"agent_id": "demo", "messages": [{"role": "user", "content": "hi"}]}) as r:
        done_run = r.headers["x-run-id"]
        _events(r)
    assert client.post(f"/v1/runs/{done_run}/reject", json={"call_id": "c", "params_hash": "h"}).status_code == 409

    msgs = [{"role": "user", "content": '!tool ticket.close_batch {"ids":["T-7"]}'}]
    with client.stream("POST", "/v1/messages", json={"agent_id": "demo", "messages": msgs}) as r:
        run_id = r.headers["x-run-id"]
        card = next(e for e in _events(r) if e["type"] == "card")
    call_id, phash = card["payload"]["call_id"], card["payload"]["params_hash"]
    assert client.post(f"/v1/runs/{run_id}/reject", json={"call_id": call_id, "params_hash": "0" * 16}).status_code == 409
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "auth-required"

    r = client.post(f"/v1/runs/{run_id}/reject", json={"call_id": call_id, "params_hash": phash, "comment": "no"})
    assert r.status_code == 200 and r.json() == {"run_id": run_id, "state": "failed"}
    rejected = next(e for e in client.get(f"/v1/runs/{run_id}/events").json() if e["type"] == "tool.rejected")
    assert rejected["payload"]["params_hash"] == phash

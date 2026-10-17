"""快照回放评测：evals/snapshots/*.jsonl 是真实 Runtime 流程录制的事件日志（录制方式与何时重录见
evals/record_snapshots.py），每个快照旁有同名 `<name>.expected.json`（sidecar，记录初始用户消息、预期终态与
预期最终助手文本）。全程不调用任何模型。验证：
(1) 日志中的每条事件都能通过 Contract 的模型校验，seq 连续，状态机的每次迁移都合法，终态与 sidecar 一致；
(2) 可重建性（确定性）：每条 model.request 记录的 messages_hash / n_messages 必须等于从它之前的日志重建出的
    消息历史的哈希 / 条数——Runtime 写入的哈希正是发给模型的历史的哈希，任何对工具结果、工具请求或模型回复的
    篡改都会让回放结果与记录不符；
(3) 日志具备真实 Runtime 的形态：context.assembled、写前记录（tool.executing 先于 tool.executed）等；
(4) ticket-summary 只使用白名单工具（非白名单调用以错误结果收场，不进闸口、不执行）；
(5) 无权限访问工单时，工具结果是网关包装的统一 404，不泄露工单内容。
"""

import json
from pathlib import Path

import pytest

from ai_contract import RunEvent, RunEventType, RunState, can_transition
from runtime.run import invariant

SNAP_DIR = Path(__file__).parent / "snapshots"
SNAPS = sorted(SNAP_DIR.glob("*.jsonl"))  # 逐个快照文件生成一条测试用例
IDS = [s.stem for s in SNAPS]
AGENT_TOOLS = {"ticket-summary": {"ticket.search", "ticket.get"}}  # Agent 工具白名单（均为只读）
SUMMARY_FIELDS = ["问题", "根因", "处理过程", "客户反馈", "可复用经验"]  # references/format.md 的模板字段
SUMMARY_SNAPS = ["ticket-summary-structured", "ticket-summary-multi", "ticket-summary-unlisted-tool", "ticket-summary-reasoning"]
NOT_FOUND = {"error": "ticket not found or no permission", "status_code": 404}  # mcp-ticket 404 经网关 / 工具代理后的工具结果


def load(snap: Path) -> tuple[list[RunEvent], dict]:
    """读取快照事件日志及其 sidecar 预期。"""
    log = [RunEvent(**json.loads(line)) for line in snap.read_text(encoding="utf-8").splitlines() if line.strip()]
    expected = json.loads(snap.with_suffix(".expected.json").read_text(encoding="utf-8"))
    return log, expected


def by_call(log: list[RunEvent], kind: RunEventType) -> dict[str, RunEvent]:
    """按 call_id 索引某类工具事件。"""
    return {e.payload["call_id"]: e for e in log if e.type == kind}


def test_snapshot_set_is_complete():
    """覆盖的场景数不少于 8 个（录制脚本中的全部场景）。"""
    assert len(SNAPS) >= 8


@pytest.mark.parametrize("snap", SNAPS, ids=IDS)
def test_snapshot_replays(snap):
    """seq 连续、状态迁移合法、终态与 sidecar 一致；从日志重建出的历史以预期的最终助手文本结尾。"""
    log, expected = load(snap)
    assert [e.seq for e in log] == list(range(1, len(log) + 1))  # seq 必须从 1 开始连续递增
    state = RunState.SUBMITTED
    for e in log:
        if e.type == RunEventType.RUN_STATE_CHANGED:
            assert RunState(e.payload["from"]) == state  # 记录的源状态必须与回放状态一致
            assert can_transition(state, RunState(e.payload["to"]))
            state = RunState(e.payload["to"])
    assert state.terminal  # 回放结束后必须落在终止态
    assert state == RunState(expected["final_state"])
    msgs = invariant.rebuild_messages(log, expected["initial_messages"])
    if expected["final_text"] is None:  # 失败/取消的 Run 没有最终助手文本：历史不得以助手文本结尾
        assert not (msgs[-1]["role"] == "assistant" and "content" in msgs[-1])
    else:
        assert msgs[-1] == {"role": "assistant", "content": expected["final_text"]}


@pytest.mark.parametrize("snap", SNAPS, ids=IDS)
def test_snapshot_model_requests_are_reconstructable(snap):
    """每条 model.request 的 messages_hash 必须等于"从它之前的日志重建出的历史"的哈希（与 runner 发请求前
    invariant.check 的口径一致：check(log 至今, initial_messages, history) 返回 message_hash(history)），
    n_messages 等于重建历史的条数。"""
    log, expected = load(snap)
    requests = [i for i, e in enumerate(log) if e.type == RunEventType.MODEL_REQUEST]
    assert requests, "快照中没有 model.request"
    for i in requests:
        rebuilt = invariant.rebuild_messages(log[:i], expected["initial_messages"])
        assert log[i].payload["messages_hash"] == invariant.message_hash(rebuilt), f"seq={log[i].seq} 的上下文无法从日志重建"
        assert log[i].payload["n_messages"] == len(rebuilt)


@pytest.mark.parametrize("snap", SNAPS, ids=IDS)
def test_snapshot_has_runtime_shape(snap):
    """快照必须是真实 Runtime 写出的形态：首个 model.request 之前有 context.assembled；闸口放行的调用先写
    tool.executing（写前记录）再写 tool.executed，且都有结果。"""
    log, _ = load(snap)
    types = [e.type for e in log]
    assert RunEventType.CONTEXT_ASSEMBLED in types
    assert types.index(RunEventType.CONTEXT_ASSEMBLED) < types.index(RunEventType.MODEL_REQUEST)
    seq = {(e.type, e.payload.get("call_id")): e.seq for e in log}
    for cid, decision in by_call(log, RunEventType.TOOL_DECISION).items():
        if decision.payload["decision"] != "allow":
            continue
        executing = seq[(RunEventType.TOOL_EXECUTING, cid)]
        assert decision.seq < executing < seq[(RunEventType.TOOL_EXECUTED, cid)] < seq[(RunEventType.TOOL_RESULT, cid)]


@pytest.mark.parametrize("snap", SNAPS, ids=IDS)
def test_snapshot_tool_calls_follow_whitelist(snap):
    """工具调用必须在 Agent 白名单内；非白名单调用须以错误结果收场，且没有闸口判定与执行事件。"""
    log, _ = load(snap)
    whitelist = AGENT_TOOLS[log[0].payload["agent_id"]]
    results = by_call(log, RunEventType.TOOL_RESULT)
    touched = {
        e.payload["call_id"] for e in log if e.type in (RunEventType.TOOL_DECISION, RunEventType.TOOL_EXECUTING, RunEventType.TOOL_EXECUTED)
    }
    assembled = next(e for e in log if e.type == RunEventType.CONTEXT_ASSEMBLED).payload["tools"]
    for e in log:
        if e.type != RunEventType.TOOL_REQUESTED:
            continue
        cid = e.payload["call_id"]
        assert cid in results  # 每个工具请求都有对应结果
        if e.payload["tool"] not in whitelist:
            assert results[cid].payload["status"] in {"error", "denied"}
            assert cid not in touched
            assert e.payload["risk_level"] != "read"  # 白名单外的调用不得被当作只读通过
            assert e.payload["tool"] not in assembled


def test_unlisted_tool_snapshot_is_never_executed():
    """模型请求白名单外的 ticket.close_batch：结果为 unknown tool 错误，不进闸口、不执行，随后只读调用照常完成。"""
    log, _ = load(SNAP_DIR / "ticket-summary-unlisted-tool.jsonl")
    calls = [e.payload for e in log if e.type == RunEventType.TOOL_REQUESTED and e.payload["tool"] == "ticket.close_batch"]
    assert len(calls) == 1
    cid = calls[0]["call_id"]
    assert by_call(log, RunEventType.TOOL_RESULT)[cid].payload == {"call_id": cid, "status": "error", "output": {"error": "unknown tool"}}
    for kind in (RunEventType.TOOL_DECISION, RunEventType.TOOL_EXECUTING, RunEventType.TOOL_EXECUTED):
        assert cid not in by_call(log, kind)
        assert all(e.payload.get("tool") != "ticket.close_batch" for e in log if e.type == kind)
    assert any(e.payload["status"] == "ok" for e in log if e.type == RunEventType.TOOL_RESULT)


@pytest.mark.parametrize("name", SUMMARY_SNAPS)
def test_summary_snapshots_follow_format(name):
    """总结类快照的最终文本必须包含 references/format.md 约定的五个字段。"""
    _, expected = load(SNAP_DIR / f"{name}.jsonl")
    assert all(f"{f}：" in expected["final_text"] for f in SUMMARY_FIELDS)


def test_reasoning_snapshot_replays_reasoning():
    """带思维链的工具调用：reasoning 随 tool_use 消息重建，且 model.response 的 thinking 本身不进入历史。"""
    log, expected = load(SNAP_DIR / "ticket-summary-reasoning.jsonl")
    assert any(e.type == RunEventType.MODEL_RESPONSE and e.payload.get("thinking") for e in log)
    msgs = invariant.rebuild_messages(log, expected["initial_messages"])
    with_reasoning = [m for m in msgs if m.get("reasoning")]
    assert len(with_reasoning) == 1 and "tool_use" in with_reasoning[0]
    assert all("thinking" not in m for m in msgs)


def test_cross_user_snapshot_does_not_leak():
    """无权限访问他人工单（T-1007 存在但不属于调用者）：工具结果是统一的 404 错误、只有这两项内容，
    没有任何成功的工具结果，助手回复表明无法访问且不含工单内容。"""
    log, expected = load(SNAP_DIR / "ticket-summary-forbidden.jsonl")
    results = [e.payload for e in log if e.type == RunEventType.TOOL_RESULT]
    assert len(results) == 1
    assert results[0]["status"] == "error"
    assert results[0]["output"] == NOT_FOUND  # 精确相等：除统一错误外不带任何工单字段
    assert results[0]["output_ref"] is None  # 也没有外置到工作区的内容
    assert "无法访问" in expected["final_text"]
    assert "EC-8" not in json.dumps([e.payload for e in log], ensure_ascii=False)  # T-1007 标题中的型号


def test_model_error_snapshot_fails_with_upstream_error():
    """模型网关失败：harness 的错误事件进日志，Run 以 failed 收尾。"""
    log, _ = load(SNAP_DIR / "ticket-summary-model-error.jsonl")
    errors = [e.payload for e in log if e.type == RunEventType.RUN_ERROR]
    assert errors and errors[-1]["code"] == "LLM_ERROR" and errors[-1]["retryable"] is True
    assert log[-1].payload["to"] == "failed"

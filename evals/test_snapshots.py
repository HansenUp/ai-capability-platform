"""快照回放评测：已提交的 .jsonl 文件既是回放输入也是预期输出，每个快照旁有同名 `<name>.expected.json`
（sidecar，记录初始用户消息、预期终态与预期最终助手文本）。验证：
(1) 日志中的每条事件都能通过 Contract 的模型校验；
(2) 状态机的每次状态迁移都是合法的（can_transition），终态与 sidecar 一致；
(3) 从日志重建出的、模型可见的上下文（context）是确定性的（不变量），且全程不调用任何模型；
(4) ticket-summary 场景只使用白名单工具（非白名单调用必须以 error/denied 结果收场，且不得执行）。
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


def load(snap: Path) -> tuple[list[RunEvent], dict]:
    """读取快照事件日志及其 sidecar 预期。"""
    log = [RunEvent(**json.loads(line)) for line in snap.read_text(encoding="utf-8").splitlines() if line.strip()]
    expected = json.loads(snap.with_suffix(".expected.json").read_text(encoding="utf-8"))
    return log, expected


@pytest.mark.parametrize("snap", SNAPS, ids=IDS)
def test_snapshot_replays(snap):
    """回放单个快照文件并校验事件序号连续、状态迁移合法、终态可达，以及上下文重建的确定性与不变量。"""
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
    init = expected["initial_messages"]
    # 重建两次，确认从同一份日志出发得到的上下文完全一致（确定性）
    a = invariant.rebuild_messages(log, init)
    b = invariant.rebuild_messages(log, init)
    assert a == b
    if expected["final_text"] is None:  # 失败/取消的 Run 没有最终助手文本：历史不得以助手文本结尾
        assert not (a[-1]["role"] == "assistant" and "content" in a[-1])
    else:
        assert a[-1] == {"role": "assistant", "content": expected["final_text"]}
    invariant.check(log, init, a)


@pytest.mark.parametrize("snap", SNAPS, ids=IDS)
def test_snapshot_model_request_sizes(snap):
    """每次 model.request 记录的 n_messages 必须等于当时从日志重建出的历史条数。"""
    log, expected = load(snap)
    for i, e in enumerate(log):
        if e.type == RunEventType.MODEL_REQUEST:
            assert e.payload["n_messages"] == len(invariant.rebuild_messages(log[:i], expected["initial_messages"]))


@pytest.mark.parametrize("snap", SNAPS, ids=IDS)
def test_snapshot_tool_calls_follow_whitelist(snap):
    """工具调用必须在 Agent 白名单内；非白名单调用须以 error/denied 收场，且没有执行事件。"""
    log, _ = load(snap)
    whitelist = AGENT_TOOLS[log[0].payload["agent_id"]]
    results = {e.payload["call_id"]: e.payload["status"] for e in log if e.type == RunEventType.TOOL_RESULT}
    executed = {e.payload["call_id"] for e in log if e.type == RunEventType.TOOL_EXECUTED}
    for e in log:
        if e.type != RunEventType.TOOL_REQUESTED:
            continue
        cid = e.payload["call_id"]
        assert cid in results  # 每个工具请求都有对应结果
        if e.payload["tool"] not in whitelist:
            assert results[cid] in {"error", "denied"}
            assert cid not in executed
            assert e.payload["risk_level"] != "read"  # 白名单外的调用不得被当作只读通过


@pytest.mark.parametrize("name", SUMMARY_SNAPS)
def test_summary_snapshots_follow_format(name):
    """总结类快照的最终文本必须包含 references/format.md 约定的五个字段。"""
    _, expected = load(SNAP_DIR / f"{name}.jsonl")
    assert all(f"{f}：" in expected["final_text"] for f in SUMMARY_FIELDS)


def test_reasoning_snapshot_replays_reasoning():
    """带思维链的工具调用：reasoning 随 tool_use 消息重建，且 model.response 的 thinking 本身不进入历史。"""
    log, expected = load(SNAP_DIR / "ticket-summary-reasoning.jsonl")
    msgs = invariant.rebuild_messages(log, expected["initial_messages"])
    with_reasoning = [m for m in msgs if m.get("reasoning")]
    assert len(with_reasoning) == 1 and "tool_use" in with_reasoning[0]
    assert all("thinking" not in m for m in msgs)


def test_cross_user_snapshot_does_not_leak():
    """无权限访问他人工单：工具结果为统一的未找到或无权限，且助手回复表明无法访问、不含任何工单内容。"""
    log, expected = load(SNAP_DIR / "ticket-summary-forbidden.jsonl")
    result = next(e for e in log if e.type == RunEventType.TOOL_RESULT)
    assert result.payload["output"]["error"] == "NOT_FOUND_OR_FORBIDDEN"
    assert "无法访问" in expected["final_text"]

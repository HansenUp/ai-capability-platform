"""快照回放评测：已提交的 .jsonl 文件既是回放输入也是预期输出。验证：
(1) 日志中的每条事件都能通过 Contract 的模型校验；
(2) 状态机的每次状态迁移都是合法的（can_transition）；
(3) 从日志重建出的、模型可见的上下文（context）是确定性的（不变量），且全程不调用任何模型。
"""

import json
from pathlib import Path

import pytest

from ai_contract import RunEvent, RunEventType, RunState, can_transition
from runtime.run import invariant

SNAPS = sorted((Path(__file__).parent / "snapshots").glob("*.jsonl"))  # 逐个快照文件生成一条测试用例


@pytest.mark.parametrize("snap", SNAPS, ids=[s.stem for s in SNAPS])
def test_snapshot_replays(snap):
    """回放单个快照文件并校验事件序号连续、状态迁移合法、终态可达，以及上下文重建的确定性与不变量。"""
    log = [RunEvent(**json.loads(line)) for line in snap.read_text().splitlines() if line.strip()]
    assert [e.seq for e in log] == list(range(1, len(log) + 1))  # seq 必须从 1 开始连续递增
    state = RunState.SUBMITTED
    for e in log:
        if e.type == RunEventType.RUN_STATE_CHANGED:
            assert can_transition(state, RunState(e.payload["to"]))
            state = RunState(e.payload["to"])
    assert state.terminal  # 回放结束后必须落在终止态
    init = [{"role": "user", "content": "总结所有工单"}]
    # 重建两次，确认从同一份日志出发得到的上下文完全一致（确定性）
    a = invariant.rebuild_messages(log, init)
    b = invariant.rebuild_messages(log, init)
    assert a == b and a[-1] == {"role": "assistant", "content": "共 1 张工单。"}
    invariant.check(log, init, a)

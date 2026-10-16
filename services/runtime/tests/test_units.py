"""单元测试：覆盖上下文预算模块的稳定前缀哈希与截断行为、可重建性不变量对上下文被篡改场景的
检测能力，以及闸口审批与"具体参数 + 有效期"的绑定关系。"""

from datetime import UTC, datetime, timedelta

import pytest

from ai_contract import RunEvent, RunEventType
from runtime.assemble.assembler import LOCAL_TOOLS
from runtime.context import budget
from runtime.guard import gate
from runtime.run import invariant

HIGH_RISK_TOOL = LOCAL_TOOLS["ticket.close_batch"]  # write_high，闸口默认判定为需要审批


def _ctx(approval: dict | None) -> gate.GateContext:
    """构造一个只带单条审批记录（call_id = c1）的闸口上下文。"""
    return gate.GateContext(subject="human:u", agent_id="a", approved_call_ids={"c1": approval} if approval else {})


def test_stable_prefix_hash_unchanged_when_history_changes():
    """校验只要固定的前三个区块（system/skills/tools）内容不变，history 区块内容变化
    不应影响 stable_prefix_hash（提示缓存前缀稳定性的核心保证）。"""
    a = budget.assemble({"system": "S", "skills": "K", "tools": "T", "history": "h1"})
    b = budget.assemble({"system": "S", "skills": "K", "tools": "T", "history": "h2 much longer"})
    assert a.stable_prefix_hash == b.stable_prefix_hash
    assert a.usage["history"] != b.usage["history"]


def test_budget_truncates_oversized_block():
    """校验超出预算的区块会被截断，并记录在 truncated 列表中。"""
    plan = budget.assemble({"system": "x" * 100_000}, {"system": 10, "skills": 1, "tools": 1, "memory": 1, "retrieval": 1, "history": 1})
    assert "system" in plan.truncated and plan.usage["system"] <= 10


def test_invariant_detects_divergence():
    """校验 rebuild_messages 能正确从事件日志重建历史，且 check() 能检测出"即将发送的历史"
    与"日志重建的历史"不一致的情况（如被注入了额外消息）并抛出 InvariantViolation。"""
    log = [
        RunEvent(run_id="r", seq=1, type=RunEventType.RUN_CREATED),
        RunEvent(run_id="r", seq=2, type=RunEventType.MODEL_RESPONSE, payload={"text": "hello"}),
    ]
    init = [{"role": "user", "content": "hi"}]
    good = invariant.rebuild_messages(log, init)
    assert good[-1] == {"role": "assistant", "content": "hello"}
    invariant.check(log, init, good)
    with pytest.raises(invariant.InvariantViolation):
        invariant.check(log, init, good + [{"role": "user", "content": "injected"}])


async def test_approval_invalidated_when_params_change():
    """审批附着于具体参数：用原参数批准后，换一组参数再调用同一个 call_id，闸口必须重新要求审批。"""
    params = {"ids": ["T-1"]}
    ctx = _ctx({"params_hash": gate.params_hash(params), "expires_at": (datetime.now(UTC) + timedelta(seconds=900)).isoformat()})
    assert (await gate.evaluate(ctx, HIGH_RISK_TOOL, params, "c1")).decision == "allow"
    assert (await gate.evaluate(ctx, HIGH_RISK_TOOL, {"ids": ["T-1", "T-999"]}, "c1")).decision == "require_approval"


async def test_approval_invalidated_after_expiry():
    """审批有有效期：过期后即便参数完全一致，闸口也必须重新要求审批。"""
    params = {"ids": ["T-1"]}
    ctx = _ctx({"params_hash": gate.params_hash(params), "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()})
    assert (await gate.evaluate(ctx, HIGH_RISK_TOOL, params, "c1")).decision == "require_approval"

"""ai_contract 包的单元测试：覆盖事件判别联合类型的往返序列化、Run 状态机的合法迁移、
A2A 状态命名，以及 RunEvent/ToolSpec/SkillDir/HarnessCapabilities 等模型的基本行为。
"""

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from ai_contract import (
    CardEvent,
    CardType,
    DoneEvent,
    Event,
    EventType,
    HarnessCapabilities,
    InputRequiredEvent,
    RunEvent,
    RunEventType,
    RunState,
    SkillDir,
    TextEvent,
    ThinkingEvent,
    ToolSpec,
    ToolUseEvent,
    can_transition,
    capability_tier,
)

EVENTS = TypeAdapter(list[Event])  # 用于按 "type" 判别字段解析/序列化事件列表


def test_event_discriminator_roundtrip():
    """校验事件的判别联合类型能正确解析为具体子类型，并能无损地序列化回等价的 JSON。"""
    raw = [
        {"type": "text", "delta": "hi"},
        {"type": "tool_use", "call_id": "c1", "tool_name": "crm.search", "input": {"q": "acme"}},
        {"type": "done", "run_id": "r1", "stop_reason": "end_turn"},
    ]
    parsed = EVENTS.validate_python(raw)
    assert isinstance(parsed[0], TextEvent)
    assert isinstance(parsed[1], ToolUseEvent)
    assert isinstance(parsed[2], DoneEvent)
    assert EVENTS.dump_python(parsed, mode="json") == [
        {"type": "text", "delta": "hi"},
        {"type": "tool_use", "call_id": "c1", "tool_name": "crm.search", "input": {"q": "acme"}},
        {"type": "done", "run_id": "r1", "trace_id": None, "stop_reason": "end_turn"},
    ]


def test_unknown_event_rejected():
    """未知的事件 type 应当被判别联合类型拒绝，抛出校验错误。"""
    with pytest.raises(ValidationError):
        EVENTS.validate_python([{"type": "bogus"}])


@pytest.mark.parametrize(
    "src,dst,ok",
    [
        (RunState.SUBMITTED, RunState.WORKING, True),
        (RunState.WORKING, RunState.AUTH_REQUIRED, True),
        (RunState.AUTH_REQUIRED, RunState.WORKING, True),
        (RunState.COMPLETED, RunState.WORKING, False),
        (RunState.SUBMITTED, RunState.COMPLETED, False),
    ],
)
def test_run_transitions(src, dst, ok):
    """校验 Run 状态机的合法迁移表：终态不可再迁移，非相邻状态不能直接跳转。"""
    assert can_transition(src, dst) is ok


def test_a2a_state_names():
    """校验 RunState 的取值与 A2A task 状态命名保持一致，并校验 terminal 属性。"""
    assert {s.value for s in RunState} == {"submitted", "working", "auth-required", "input-required", "completed", "failed", "canceled"}
    assert RunState.FAILED.terminal and not RunState.WORKING.terminal


def test_run_event_and_tool_spec():
    """校验 RunEvent/ToolSpec/SkillDir/HarnessCapabilities 的默认值与字段行为。"""
    ev = RunEvent(run_id="r1", seq=1, type=RunEventType.RUN_CREATED, payload={"agent_id": "a"})
    assert ev.model_dump()["type"] == "run.created"
    t = ToolSpec(name="crm.close", description="close deal", risk_level="write_high", requires_idempotency_key=True)
    assert t.sandbox_level == "read-only"
    s = SkillDir(name="ticket-summary", version="1.0.0", path=Path("skills/ticket-summary"))
    assert s.signature is None
    HarnessCapabilities(name="mock", version="0", transport="in-process")


def test_capability_tier_three_levels():
    """校验 capability_tier 的判定：平台持有循环时由平台闸口与事件日志保证，直接 L2；
    harness 持有循环时，enforcing + 可由事件日志续跑才是 L2，仅 enforcing 是 L1，其余降到 L0。"""
    base = {"name": "h", "version": "0", "transport": "stdio"}
    # 未声明 loop_owner 时默认按 harness 持有循环处理（失败关闭），落到 L0
    assert capability_tier(HarnessCapabilities(**base)) == "L0"
    # 平台持有循环：工具由平台工具代理执行，闸口必然生效，恢复由事件日志重放，直接 L2
    assert capability_tier(HarnessCapabilities(**base, loop_owner="platform")) == "L2"
    assert capability_tier(HarnessCapabilities(**base, loop_owner="platform", tool_interception="none")) == "L2"
    # advisory 拦截可被 harness 绕过，即便能由事件日志续跑也只能算 L0
    assert capability_tier(HarnessCapabilities(**base, tool_interception="advisory", can_resume_from_events=True)) == "L0"
    assert capability_tier(HarnessCapabilities(**base, tool_interception="enforcing")) == "L1"
    assert capability_tier(HarnessCapabilities(**base, tool_interception="enforcing", can_resume_from_events=True)) == "L2"


# ---------------- Contract V1 冻结（P0-2.2）----------------


def test_contract_version_exported():
    """CONTRACT_VERSION 存在、是语义化版本号，并在 __all__ 中导出。"""
    import ai_contract

    assert ai_contract.CONTRACT_VERSION == "1.0.0"
    assert "CONTRACT_VERSION" in ai_contract.__all__
    for name in ("InputRequiredEvent", "CardType", "ThinkingVisibility"):
        assert name in ai_contract.__all__


def test_input_required_event_roundtrip():
    """input_required 事件能经判别联合解析为 InputRequiredEvent；协议字段名为 schema，往返无损。"""
    raw = {
        "type": "input_required",
        "prompt": "请选择要关闭的工单",
        "schema": {"type": "object", "properties": {"ids": {"type": "array"}}},
        "call_id": "c1",
    }
    (ev,) = EVENTS.validate_python([raw])
    assert isinstance(ev, InputRequiredEvent)
    assert ev.type == EventType.INPUT_REQUIRED and ev.input_schema == raw["schema"]
    assert EVENTS.dump_python([ev], mode="json") == [raw]
    assert json.loads(ev.model_dump_json()) == raw
    # schema / call_id 可省略；Python 侧也可以用 input_schema 构造
    minimal = InputRequiredEvent(prompt="补充说明")
    assert minimal.model_dump(mode="json") == {"type": "input_required", "prompt": "补充说明", "schema": None, "call_id": None}
    assert InputRequiredEvent(prompt="x", input_schema={"type": "string"}).model_dump()["schema"] == {"type": "string"}
    with pytest.raises(ValidationError):
        EVENTS.validate_python([{"type": "input_required"}])  # prompt 必填
    with pytest.raises(ValidationError):
        EVENTS.validate_python([{"type": "input_required", "prompt": "x", "bogus": 1}])  # 禁止未声明字段


def test_input_run_event_types():
    """用户补充输入在事件日志中有对应的 RunEventType，命名沿用 <域>.<动作> 风格。"""
    assert RunEventType.INPUT_REQUESTED == "input.requested"
    assert RunEventType.INPUT_PROVIDED == "input.provided"
    ev = RunEvent(run_id="r1", seq=2, type=RunEventType.INPUT_PROVIDED, payload={"input": {"ids": ["T-1"]}})
    assert ev.model_dump(mode="json")["type"] == "input.provided"


def test_thinking_visibility_default_and_values():
    """ThinkingEvent.visibility 默认 internal（失败关闭），只接受 internal/summary/full。"""
    (ev,) = EVENTS.validate_python([{"type": "thinking", "delta": "..."}])
    assert isinstance(ev, ThinkingEvent) and ev.visibility == "internal"
    for v in ("internal", "summary", "full"):
        assert EVENTS.dump_python(EVENTS.validate_python([{"type": "thinking", "delta": "d", "visibility": v}]), mode="json") == [
            {"type": "thinking", "delta": "d", "visibility": v}
        ]
    with pytest.raises(ValidationError):
        ThinkingEvent(delta="d", visibility="public")


def test_card_type_enum():
    """CardType 枚举取值固定为四种，CardEvent.card_type 只接受枚举值并序列化为字符串。"""
    assert {c.value for c in CardType} == {"impact_preview", "form", "choice", "reconciliation_required"}
    (card,) = EVENTS.validate_python([{"type": "card", "card_type": "reconciliation_required", "payload": {"call_id": "c1"}}])
    assert isinstance(card, CardEvent) and card.card_type is CardType.RECONCILIATION_REQUIRED
    assert json.loads(CardEvent(card_type=CardType.FORM).model_dump_json()) == {"type": "card", "card_type": "form", "payload": {}}
    with pytest.raises(ValidationError):
        CardEvent(card_type="table")


def test_event_union_covers_all_event_types():
    """Event 联合类型覆盖 EventType 的全部取值：每个 type 都能解析到对应子类型（discriminator 完整）。"""
    samples = {
        "text": {"delta": "a"},
        "thinking": {"delta": "b"},
        "tool_use": {"call_id": "c", "tool_name": "t"},
        "tool_result": {"call_id": "c"},
        "card": {"card_type": "choice"},
        "input_required": {"prompt": "p"},
        "usage": {},
        "error": {"code": "E", "message": "m"},
        "done": {"run_id": "r"},
    }
    assert set(samples) == {t.value for t in EventType}
    parsed = EVENTS.validate_python([{"type": t, **body} for t, body in samples.items()])
    assert [p.type for p in parsed] == list(samples)

"""ai_contract 包的单元测试：覆盖事件判别联合类型的往返序列化、Run 状态机的合法迁移、
A2A 状态命名，以及 RunEvent/ToolSpec/SkillDir/HarnessCapabilities 等模型的基本行为。
"""

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from ai_contract import (
    HOOK_OVERRIDABLE,
    CardEvent,
    CardType,
    DoneEvent,
    Event,
    EventType,
    HarnessCapabilities,
    HookPoint,
    HookSet,
    InputRequiredEvent,
    RunEvent,
    RunEventType,
    RunState,
    SkillDir,
    TextEvent,
    ThinkingEvent,
    ToolSpec,
    ToolUseEvent,
    TurnInput,
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

    assert ai_contract.CONTRACT_VERSION == "1.3.0"
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


def test_v13_capabilities_discovered_and_error_detail():
    """V1.3：发现事件类型 capabilities.discovered；统一错误体 ErrorDetail{detail, code} 两字段必填。"""
    import ai_contract

    assert RunEventType.CAPABILITIES_DISCOVERED == "capabilities.discovered"
    ev = RunEvent(run_id="r1", seq=3, type=RunEventType.CAPABILITIES_DISCOVERED, payload={"tools": [], "tools_hash": "x"})
    assert ev.model_dump(mode="json")["type"] == "capabilities.discovered"
    assert "ErrorDetail" in ai_contract.__all__
    err = ai_contract.ErrorDetail(detail="run is not awaiting approval", code="CONFLICT")
    assert err.model_dump() == {"detail": "run is not awaiting approval", "code": "CONFLICT"}
    with pytest.raises(ValidationError):
        ai_contract.ErrorDetail(detail="x")  # type: ignore[call-arg]


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


# ---------------- Contract V1.1（S5：Hooks 由平台统一执行）----------------


def test_v11_exports():
    """V1.1 新增的 HookPoint、HOOK_OVERRIDABLE 从包入口导出。"""
    import ai_contract

    for name in ("HookPoint", "HOOK_OVERRIDABLE"):
        assert name in ai_contract.__all__


def test_hook_points_and_overridable_whitelist():
    """四个拦截点取值固定；每个点都有可覆盖键白名单，取值与方案 §1.1 一致（pre_model 不可覆盖任何键）。"""
    assert [p.value for p in HookPoint] == ["pre_model", "post_model", "pre_tool", "post_tool"]
    assert set(HOOK_OVERRIDABLE) == set(HookPoint)
    assert HOOK_OVERRIDABLE[HookPoint.PRE_MODEL] == frozenset()
    assert HOOK_OVERRIDABLE[HookPoint.POST_MODEL] == {"thinking_summary"}
    assert HOOK_OVERRIDABLE[HookPoint.PRE_TOOL] == {"input"}
    assert HOOK_OVERRIDABLE[HookPoint.POST_TOOL] == {"output", "data_class"}
    assert all(isinstance(v, frozenset) for v in HOOK_OVERRIDABLE.values())


async def test_hookset_post_model_optional():
    """HookSet 新增可选的 post_model；未设置时为 None，原有构造方式不受影响。"""
    assert HookSet().post_model is None
    seen: list[dict] = []

    async def post_model(p):
        seen.append(p)
        return {"thinking_summary": "s"}

    hs = HookSet(post_model=post_model)
    assert hs.post_model is not None
    assert await hs.post_model({"text": "t"}) == {"thinking_summary": "s"}
    assert seen == [{"text": "t"}]


def test_internal_hook_points_default_and_validation():
    """internal_hook_points 默认空（全部由平台执行），只接受 HookPoint 取值，序列化为字符串；extra=forbid 不变。"""
    base = {"name": "h", "version": "0", "transport": "stdio"}
    assert HarnessCapabilities(**base).internal_hook_points == []
    caps = HarnessCapabilities(**base, internal_hook_points=["pre_model", HookPoint.PRE_TOOL])
    assert caps.internal_hook_points == [HookPoint.PRE_MODEL, HookPoint.PRE_TOOL]
    assert caps.model_dump(mode="json")["internal_hook_points"] == ["pre_model", "pre_tool"]
    with pytest.raises(ValidationError):
        HarnessCapabilities(**base, internal_hook_points=["on_subagent"])
    with pytest.raises(ValidationError):
        HarnessCapabilities(**base, bogus=True)
    # 新字段不参与能力分级
    assert capability_tier(caps) == capability_tier(HarnessCapabilities(**base))


def test_turn_input_tool_statuses_optional_and_validated():
    """TurnInput.tool_statuses（V1.1 新增）默认空字典，只接受工具结果状态取值；extra=forbid 不变。"""
    assert TurnInput(messages=[]).tool_statuses == {}
    turn = TurnInput(messages=[], tool_statuses={"c1": "denied", "c2": "error", "c3": "ok", "c4": "pending_approval"})
    assert turn.model_dump(mode="json")["tool_statuses"]["c1"] == "denied"
    with pytest.raises(ValidationError):
        TurnInput(messages=[], tool_statuses={"c1": "rejected"})
    with pytest.raises(ValidationError):
        TurnInput(messages=[], bogus=True)


def test_image_ref_load_and_validation(tmp_path: Path) -> None:
    """image_ref（V1.2 新增）：按引用从工作区读取图片并返回 base64；格式、路径穿越、缺失、指纹或类型不符一律 ImageRefError。"""
    import base64
    import hashlib

    import ai_contract
    from ai_contract import IMAGE_REF_TYPE, ImageRefError, image_ref_path, is_image_ref, load_image_ref, sniff_media_type

    for name in ("IMAGE_REF_TYPE", "IMAGE_MEDIA_TYPES", "ImageRefError", "load_image_ref", "sniff_media_type"):
        assert name in ai_contract.__all__
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    aid = "ab" * 16
    (tmp_path / f"{aid}.png").write_bytes(png)
    part = {
        "type": IMAGE_REF_TYPE,
        "ref": f"artifact://{aid}",
        "media_type": "image/png",
        "size": len(png),
        "sha256": hashlib.sha256(png).hexdigest(),
    }
    assert is_image_ref(part) and not is_image_ref({"type": "text"})
    assert image_ref_path(str(tmp_path), part) == str(tmp_path / f"{aid}.png")
    assert load_image_ref(str(tmp_path), part) == ("image/png", base64.b64encode(png).decode())
    assert sniff_media_type(b"\xff\xd8\xff\xe0") == "image/jpeg"
    assert sniff_media_type(b"GIF89a") == "image/gif"
    assert sniff_media_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff_media_type(b"%PDF-1.7") is None
    bad = [
        part | {"ref": "artifact://../../etc/passwd"},
        part | {"media_type": "image/svg+xml"},
        part | {"sha256": "x"},
        part | {"sha256": "0" * 64},  # 内容与记录的指纹不符
        part | {"ref": "artifact://" + "cd" * 16},  # 工件不存在
        part | {"media_type": "image/jpeg"},  # 文件不存在（扩展名随类型）
    ]
    for p in bad:
        with pytest.raises(ImageRefError):
            load_image_ref(str(tmp_path), p)
    with pytest.raises(ImageRefError):
        load_image_ref(None, part)
    (tmp_path / f"{aid}.png").write_bytes(b"not an image")
    with pytest.raises(ImageRefError):  # 指纹与内容一致但不是 png
        load_image_ref(str(tmp_path), part | {"sha256": hashlib.sha256(b"not an image").hexdigest()})

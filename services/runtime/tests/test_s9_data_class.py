"""S9 OPT-4 数据分级六通道（契约 ④，D152）在 Runtime 侧的落地。

- 矩阵：开发矩阵可加载、未签署；开发态兼容旧配置 CONFIDENTIAL_MODEL_ALIASES，生产不兼容；
- 模型通道：runner 按 Matrix.allows("model", level, alias) 判定（internal 污点同样受控）；
- 回退通道：等级 ≥ internal 的请求带 disable_fallbacks；config.yaml 的回退链不降级（CI 校验）；摘要别名为非推理且无回退；
- 观测通道：internal 经 redact_pii 后记录、confidential 只记元数据；网关请求 metadata 打码；
- 记忆 / 重排通道：无实现，架构测试保证新增实现必须经 allows 守卫；
- toolbroker.taint_of 传播 internal。
"""

from __future__ import annotations

import dataclasses
import json
import re
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from test_harness_litellm import ALIAS, Gateway, text_turn, tool_call_turn

from ai_contract import RunEventType
from ai_contract.data_class import CHANNELS, Level, Matrix
from runtime import config
from runtime.llm import set_transport_for_tests
from runtime.loop import data_class as dc
from runtime.loop import runner
from runtime.observe import tracing

ROOT = Path(__file__).resolve().parents[3]
RUNTIME_SRC = ROOT / "services" / "runtime" / "src" / "runtime"
GATEWAY_CONFIG = ROOT / "services" / "model-gateway" / "config.yaml"


def test_dev_matrix_loads_unsigned_and_covers_all_channels() -> None:
    m = Matrix.load(config.DEV_DATA_CLASS_MATRIX)
    assert not m.signed and set(m.rules) == set(CHANNELS)
    assert config.settings.data_class_matrix_file == str(config.DEV_DATA_CLASS_MATRIX)
    # 进程内嵌入器（hash / fastembed）数据不出进程：internal 与 confidential 均准许；外部嵌入只准 internal
    assert m.allows("embedding", "confidential", "fastembed") and not m.allows("embedding", "confidential", "embed-default")
    assert not m.allows("memory", "internal", "any-store") and not m.allows("fallback", "internal", "default-chat")


def test_legacy_confidential_aliases_only_in_dev() -> None:
    dev = dataclasses.replace(config.settings, confidential_model_aliases=("legacy-private",), runtime_env="dev")
    prod = dataclasses.replace(dev, runtime_env="prod")
    assert dc.model_allowed("legacy-private", Level.CONFIDENTIAL, dev)
    assert not dc.model_allowed("legacy-private", Level.CONFIDENTIAL, prod)  # 生产以矩阵为准（并由基线拒绝旧配置）
    assert dc.model_allowed("anything", Level.INTERNAL, dev) and not dc.model_allowed("anything", Level.CONFIDENTIAL, dev)


def test_levels_observe_fallback_memory_and_request_extra() -> None:
    assert dc.run_level(set()) is Level.PUBLIC and dc.run_level({"internal"}) is Level.INTERNAL
    assert dc.run_level({"internal", "confidential"}) is Level.CONFIDENTIAL
    assert [dc.observe_mode(lv) for lv in Level] == ["full", "redacted", "metadata"]
    assert dc.fallbacks_allowed("default-chat", Level.PUBLIC) and not dc.fallbacks_allowed("default-chat", Level.INTERNAL)
    assert not dc.memory_allowed(Level.PUBLIC, "vector-memory")  # 开发矩阵不准许任何跨会话记忆
    assert runner.request_extra(Level.PUBLIC, "default-chat") == {}  # public：请求体不变
    assert runner.request_extra(Level.CONFIDENTIAL, "x") == {
        "disable_fallbacks": True,
        "metadata": {"data_class": "confidential", "mask_input": True, "mask_output": True},
    }


def test_taint_of_propagates_internal() -> None:
    from runtime.loop.toolbroker import taint_of

    assert taint_of("ok", "internal") == {"internal"} and taint_of("error", "confidential") == {"confidential"}
    assert taint_of("denied", "internal") == set() and taint_of("ok", "public") == set()


class _Span:
    def __init__(self) -> None:
        self.attrs: dict[str, Any] = {}

    def set_attribute(self, k: str, v: Any) -> None:
        self.attrs[k] = v


def test_trace_content_by_level() -> None:
    prompt, completion = "我的手机 13812345678", "已联系 a.b@corp.cn"
    full, red, meta = _Span(), _Span(), _Span()
    tracing.record_model_content(full, prompt, completion, mode="full")  # type: ignore[arg-type]
    tracing.record_model_content(red, prompt, completion, mode="redacted")  # type: ignore[arg-type]
    tracing.record_model_content(meta, prompt, completion, mode="metadata")  # type: ignore[arg-type]
    assert full.attrs["gen_ai.prompt"] == prompt
    assert "13812345678" not in red.attrs["gen_ai.prompt"] and "[REDACTED:" in red.attrs["gen_ai.prompt"]
    assert "a.b@corp.cn" not in red.attrs["gen_ai.completion"] and red.attrs["ai.content_pii_redacted"] is True
    assert meta.attrs == {"ai.content_redacted": True}


def test_gateway_fallback_chains_do_not_downgrade_and_summary_alias() -> None:
    """CI 校验：config.yaml 每条回退链的目标，在源别名准许回退的每个等级上同样被模型通道准许（回退不降级）；
    等级 ≥ internal 一律不回退（Runtime 带 disable_fallbacks）；摘要别名为非推理模型、无回退链。"""
    cfg = yaml.safe_load(GATEWAY_CONFIG.read_text(encoding="utf-8"))
    aliases = {m["model_name"]: m["litellm_params"] for m in cfg["model_list"]}
    m = Matrix.load(config.DEV_DATA_CLASS_MATRIX)
    chains = {src: dsts for entry in cfg["router_settings"].get("fallbacks", []) for src, dsts in entry.items()}
    assert chains, "no fallback chain configured"
    for src, dsts in chains.items():
        assert src in aliases and all(d in aliases for d in dsts)
        for level in Level:
            if level >= Level.INTERNAL:
                assert not dc.fallbacks_allowed(src, level), (src, level)
            if not (m.allows("fallback", level, src) and m.allows("model", level, src)):
                continue
            for dst in dsts:
                assert m.allows("model", level, dst), f"fallback {src} → {dst} downgrades {level.value} data"
    summary = config.settings.thinking_summary_model_alias
    assert summary in aliases and summary not in chains
    params = aliases[summary]
    assert params["model"].startswith("deepseek/") and params["extra_body"]["thinking"]["type"] == "disabled"


def test_runner_blocks_model_by_matrix_for_internal(client: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """internal 污点同样受模型通道约束：矩阵只准许 internal 发给别名 private-x 时，读过 internal 工具结果后不再调用 default-chat。"""
    matrix = {"channels": {"model": {"public": ["*"], "internal": ["private-x"], "confidential": []}}}
    path = tmp_path / "m.json"
    path.write_text(json.dumps(matrix), encoding="utf-8")
    monkeypatch.setattr(runner, "settings", dataclasses.replace(runner.settings, data_class_matrix_file=str(path)))
    with client.stream("POST", "/v1/messages", json={"agent_id": "demo", "messages": [{"role": "user", "content": "!tool echo {}"}]}) as r:
        run_id = r.headers["x-run-id"]
        evs = [json.loads(line[5:]) for line in r.iter_lines() if line.startswith("data:")]
    err = next(e for e in evs if e["type"] == "error")
    assert err["code"] == "DATA_CLASS_MODEL_BLOCKED" and "internal data" in err["message"]
    log = client.get(f"/v1/runs/{run_id}/events").json()
    assert [e["type"] for e in log].count("model.request") == 1 and client.get(f"/v1/runs/{run_id}").json()["state"] == "failed"


def test_requests_after_internal_result_disable_fallbacks_and_mask(client: Any) -> None:
    gw = Gateway(tool_call_turn("echo", '{"text": "hi"}', call_id="dc1"), text_turn("好了"))
    set_transport_for_tests(httpx.MockTransport(gw), max_retries=0, backoff_s=0)
    try:
        name = f"s9-dc-{uuid.uuid4().hex[:6]}"
        body = {"agent_id": name, "version": "1", "harness": "litellm", "model_alias": ALIAS, "config": {"tools": ["echo"]}}
        assert client.post("/admin/agents", json=body).status_code == 201
        assert client.post(f"/admin/agents/{name}/versions/1/publish", json={"eval_passed": True}).status_code == 200
        with client.stream("POST", "/v1/messages", json={"agent_id": name, "messages": [{"role": "user", "content": "回显"}]}) as r:
            run_id = r.headers["x-run-id"]
            list(r.iter_lines())
    finally:
        set_transport_for_tests(None)
    first, second = gw.requests
    assert "disable_fallbacks" not in first and "metadata" not in first  # 尚无污点（public）：请求体不变
    assert second["disable_fallbacks"] is True and second["metadata"]["data_class"] == "internal"
    assert second["metadata"]["mask_input"] is True and second["metadata"]["mask_output"] is True
    log = client.get(f"/v1/runs/{run_id}/events").json()
    assert next(e for e in log if e["type"] == RunEventType.TOOL_RESULT.value)["payload"]["data_class"] == "internal"


def _sources() -> dict[str, str]:
    return {str(p.relative_to(RUNTIME_SRC)): p.read_text(encoding="utf-8") for p in RUNTIME_SRC.rglob("*.py")}


def test_architecture_memory_and_rerank_channels_have_no_unguarded_call_sites() -> None:
    """记忆通道：平台尚无跨会话记忆实现——runtime 不得出现记忆模块 / 记忆库依赖；将来新增实现必须调用
    data_class.memory_allowed（本测试随之更新白名单）。重排通道：runtime 无调用点（重排只在能力网关检索内部，按矩阵登记）。"""
    srcs = _sources()
    assert not [p for p in srcs if re.search(r"(^|/)memory(/|\.py$)", p)]
    banned = re.compile(r"^\s*(import|from)\s+(mem0|langmem|letta|zep_python|zep_cloud|memgpt)\b", re.M)
    assert not [p for p, s in srcs.items() if banned.search(s)]
    guarded = {p for p, s in srcs.items() if "memory_allowed(" in s}
    assert guarded == {"loop/data_class.py"}, guarded  # 只有守卫自身；新增记忆写入点须在此登记并调用守卫
    assert not [p for p, s in srcs.items() if re.search(r"rerank-default|/rerank\b|\.rerank\(", s)]


def test_compaction_summary_and_artifacts_inherit_level() -> None:
    """摘要与附件继承等级：压缩事件带 data_class（= Run 等级）；外置工件在机密 Run 中按 confidential 继承（D88）。"""
    import inspect

    from runtime.context import compact
    from runtime.loop import toolbroker

    assert "data_class" in compact.Compaction.__dataclass_fields__
    assert '"artifact_data_class"' in inspect.getsource(toolbroker._finish)

"""S6 WP-D Runtime 集成：goose / dsh 进入 registry（AcpStdioHarness + 进程池，开会话前即报告 harness 持有循环）、
进程池已满映射为可重试的 POOL_EXHAUSTED（非流式 503；新 Run failed，续跑保持 working 可再次续跑）、
上下文预算按每张图片约 1.5k token 估算、门禁对缺二进制的 harness 记 unavailable。"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest

from ai_contract import SessionHandle, SessionSpec, capability_tier
from ai_harness_probe.gate import run_gate
from runtime.api import admin
from runtime.config import settings
from runtime.context import budget
from runtime.harness import registry
from runtime.harness.mock import MockHarness


@pytest.mark.parametrize("name", ["goose", "dsh"])
def test_registry_loads_acp_harness_with_pool(name: str):
    """开会话前的能力声明即为 harness 持有循环（不是 StdioHarness 默认的平台持有循环，否则 registry 定错等级）；
    每个 harness 一个进程池，桥进程拿到平台模型网关地址与密钥、二进制路径；注册时不启动任何进程。"""
    from ai_harness_stdio import WorkerPool
    from ai_harness_stdio.acp.bridge import AcpStdioHarness, bridge_command

    h = registry.get(name)
    assert isinstance(h, AcpStdioHarness)
    caps = h.capabilities()
    assert (caps.name, caps.loop_owner, caps.tool_interception, caps.can_resume_from_events) == (name, "harness", "enforcing", True)
    assert capability_tier(caps) == "L2"
    pool = h.pool
    assert isinstance(pool, WorkerPool) and pool.cmd == h.cmd == bridge_command(name)
    env = dict(pool.config.env or {})
    assert env["ACP_LLM_BASE_URL"] == settings.model_gateway_url and "ACP_LLM_API_KEY" not in env
    # 密钥不进环境变量：池为每个 worker 写 0600 文件，桥只拿到路径（ACP_LLM_API_KEY_FILE），读完即删；也不进 repr
    assert pool.config.secret_env == {"ACP_LLM_API_KEY": settings.model_gateway_api_key}
    assert settings.model_gateway_api_key not in repr(pool.config)
    assert env["GOOSE_BIN"] if name == "goose" else env["DSH_BIN"]
    assert pool.config.min_idle == settings.harness_pool_min_idle and pool.config.max_workers == settings.harness_pool_max_workers
    assert pool.stats()["total"] == 0  # 懒启动：第一次 acquire 才预热
    assert registry.get(name) is h


def test_acp_binary_and_pool_settings_from_env(monkeypatch: pytest.MonkeyPatch):
    from runtime.config import Settings

    monkeypatch.delenv("GOOSE_BIN", raising=False)
    monkeypatch.delenv("DSH_BIN", raising=False)
    s = Settings()
    assert s.goose_bin == "goose"
    assert s.dsh_bin == os.path.expanduser("~/.local/share/ai-platform/dsh/node_modules/.bin/dsh")
    monkeypatch.setenv("GOOSE_BIN", "/opt/x/goose")
    monkeypatch.setenv("DSH_BIN", "~/dsh")
    monkeypatch.setenv("HARNESS_POOL_MIN_IDLE", "0")
    monkeypatch.setenv("HARNESS_POOL_MAX_WORKERS", "3")
    monkeypatch.setenv("HARNESS_POOL_ACQUIRE_TIMEOUT_S", "2.5")
    s = Settings()
    assert (s.goose_bin, s.dsh_bin) == ("/opt/x/goose", os.path.expanduser("~/dsh"))
    assert (s.harness_pool_min_idle, s.harness_pool_max_workers, s.harness_pool_acquire_timeout_s) == (0, 3, 2.5)


def test_goose_high_risk_write_still_needs_allowlist(client, monkeypatch: pytest.MonkeyPatch):
    """goose 即使 L2，声明高风险写仍须在 HARNESS_LOOP_HIGH_RISK_ALLOWLIST 中（D79）。"""
    from ai_harness_stdio.goose.profile import GooseProfile

    monkeypatch.setattr(GooseProfile, "agent_version", lambda self: "1.54.0")  # CI 无 goose 二进制时版本闸门不先拦下
    agent_id = f"s6-goose-{uuid.uuid4().hex[:6]}"
    monkeypatch.setattr(admin, "settings", dataclasses.replace(admin.settings, publish_check_model_alias=False))
    body = {"agent_id": agent_id, "version": "1", "harness": "goose", "config": {"tools": ["ticket.close_batch"]}}
    assert client.post("/admin/agents", json=body).status_code == 201
    r = client.post(f"/admin/agents/{agent_id}/versions/1/publish", json={"eval_passed": True})
    assert r.status_code == 409 and "HARNESS_LOOP_HIGH_RISK_ALLOWLIST" in r.json()["detail"]
    allowed = dataclasses.replace(admin.settings, harness_loop_high_risk_allowlist=(agent_id,))
    monkeypatch.setattr(admin, "settings", allowed)
    assert client.post(f"/admin/agents/{agent_id}/versions/1/publish", json={"eval_passed": True}).status_code == 200


# ---------------- harness 版本（S6 评审）----------------


class VersionedHarness(MockHarness):
    """mock + harness 持有循环的能力声明：发布时冻结 capabilities().version，开会话后 session_capabilities() 报告实际版本
    （同 StdioHarness 的 opened 帧）。"""

    def __init__(self, version: str) -> None:
        super().__init__()
        self.version = version
        self.session_version = version

    def capabilities(self) -> Any:
        update = {"version": self.version, "loop_owner": "harness", "tool_interception": "enforcing", "can_resume_from_events": True}
        return super().capabilities().model_copy(update=update)

    def session_capabilities(self, session: SessionHandle) -> Any:
        return self.capabilities().model_copy(update={"version": self.session_version})


def _versioned_agent(client: Any, version: str) -> tuple[str, VersionedHarness]:
    name = f"s6-ver-{uuid.uuid4().hex[:6]}"
    harness = VersionedHarness(version)
    registry.register(name, harness)
    body = {"agent_id": name, "version": "1", "harness": name, "system_prompt": "x", "config": {"tools": ["echo"]}}
    assert client.post("/admin/agents", json=body).status_code == 201
    return name, harness


def test_publish_rejects_harness_loop_adapter_without_version(client):
    """harness 持有循环的适配器取不到版本：发布 409（不能冻结空的 harness_version）。"""
    name, _ = _versioned_agent(client, "")
    r = client.post(f"/admin/agents/{name}/versions/1/publish", json={"eval_passed": True})
    assert r.status_code == 409 and "版本" in r.json()["detail"]


def test_harness_version_mismatch_fails_run_closed(client):
    """开会话时实际版本与快照 harness_version 不一致：HARNESS_VERSION_MISMATCH、Run failed、不调模型；版本一致即正常。"""
    name, harness = _versioned_agent(client, "1.2.3")
    assert client.post(f"/admin/agents/{name}/versions/1/publish", json={"eval_passed": True}).status_code == 200
    harness.session_version = "1.2.4"  # 发布后二进制被升级
    with client.stream("POST", "/v1/messages", json={"agent_id": name, "messages": [{"role": "user", "content": "hi"}]}) as r:
        run_id = r.headers["x-run-id"]
        evs = _sse(r)
    assert [e["type"] for e in evs] == ["error", "done"] and evs[0]["code"] == "HARNESS_VERSION_MISMATCH"
    assert "'1.2.4'" in evs[0]["message"] and "'1.2.3'" in evs[0]["message"]
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "failed"
    log = client.get(f"/v1/runs/{run_id}/events").json()
    assert [e["payload"]["code"] for e in log if e["type"] == "run.error"] == ["HARNESS_VERSION_MISMATCH"]
    assert not [e for e in log if e["type"] in ("model.request", "context.assembled")]
    assert harness.active_turns() == []
    harness.session_version = "1.2.3"
    ok = client.post("/v1/messages", json={"agent_id": name, "messages": [{"role": "user", "content": "hi"}], "stream": False})
    assert ok.status_code == 200 and ok.json()["content"]


# ---------------- POOL_EXHAUSTED ----------------


class _Exhausted(RuntimeError):
    code = "POOL_EXHAUSTED"  # 与 ai_harness_stdio.PoolExhausted 同口径：runner 按 code 识别


class PoolLimitedHarness(MockHarness):
    """mock harness + 可切换的"池已满"：full 为 True 时 open_session（及经它的 resume）抛 POOL_EXHAUSTED。"""

    def __init__(self) -> None:
        super().__init__()
        self.full = False
        self.opened = 0

    async def open_session(self, spec: SessionSpec) -> SessionHandle:
        if self.full:
            raise _Exhausted("stdio pool exhausted: 1 workers busy for 0.1s")
        self.opened += 1
        return await super().open_session(spec)

    async def resume(self, spec: SessionSpec, events: list[Any]) -> SessionHandle:
        if self.full:
            raise _Exhausted("stdio pool exhausted: 1 workers busy for 0.1s")
        return await super().resume(spec, events)


def _sse(resp: Any) -> list[dict[str, Any]]:
    return [json.loads(line[5:].strip()) for line in resp.iter_lines() if line.startswith("data:")]


@pytest.fixture
def pool_agent(client) -> tuple[str, PoolLimitedHarness]:
    name = f"s6-pool-{uuid.uuid4().hex[:6]}"
    harness = PoolLimitedHarness()
    registry.register(name, harness)
    body = {"agent_id": name, "version": "1", "harness": name, "system_prompt": "x", "config": {"tools": ["echo", "ticket.close_batch"]}}
    assert client.post("/admin/agents", json=body).status_code == 201
    assert client.post(f"/admin/agents/{name}/versions/1/publish", json={"eval_passed": True}).status_code == 200
    return name, harness


def test_pool_exhausted_non_stream_is_503_and_run_failed(client, pool_agent):
    name, harness = pool_agent
    harness.full = True
    r = client.post("/v1/messages", json={"agent_id": name, "messages": [{"role": "user", "content": "hi"}], "stream": False})
    assert r.status_code == 503 and r.json()["detail"].startswith("POOL_EXHAUSTED") and r.headers["retry-after"] == "1"
    # 重发即可：池有空位后同样的请求成功
    harness.full = False
    ok = client.post("/v1/messages", json={"agent_id": name, "messages": [{"role": "user", "content": "hi"}], "stream": False})
    assert ok.status_code == 200 and ok.json()["content"]


def test_pool_exhausted_stream_emits_retryable_error(client, pool_agent):
    name, harness = pool_agent
    harness.full = True
    with client.stream("POST", "/v1/messages", json={"agent_id": name, "messages": [{"role": "user", "content": "hi"}]}) as r:
        assert r.status_code == 200
        run_id = r.headers["x-run-id"]
        evs = _sse(r)
    assert [e["type"] for e in evs] == ["error", "done"]
    assert evs[0]["code"] == "POOL_EXHAUSTED" and evs[0]["retryable"] is True and evs[1]["stop_reason"] == "error"
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "failed"
    log = client.get(f"/v1/runs/{run_id}/events").json()
    errors = [e["payload"] for e in log if e["type"] == "run.error"]
    assert errors == [{"code": "POOL_EXHAUSTED", "message": evs[0]["message"], "retryable": True}]
    assert not [e for e in log if e["type"] in ("model.request", "context.assembled")]  # 没打开会话、没调模型
    assert harness.opened == 0


def test_pool_exhausted_on_resume_keeps_run_resumable(client, pool_agent):
    """审批后续跑时池已满：Run 保持 working（不判失败），发起人稍后再续跑即完成；工具只执行一次。"""
    name, harness = pool_agent
    msgs = [{"role": "user", "content": '!tool ticket.close_batch {"ids":["T-9"]}'}]
    with client.stream("POST", "/v1/messages", json={"agent_id": name, "messages": msgs}) as r:
        run_id = r.headers["x-run-id"]
        card = next(e for e in _sse(r) if e["type"] == "card")
    approval = {"call_id": card["payload"]["call_id"], "params_hash": card["payload"]["params_hash"]}
    assert client.post(f"/v1/runs/{run_id}/approve", json=approval).status_code == 200

    harness.full = True
    with client.stream("POST", f"/v1/runs/{run_id}/resume", json={"messages": msgs}) as r:
        evs = _sse(r)
    assert evs[0]["code"] == "POOL_EXHAUSTED" and evs[0]["retryable"] is True and evs[-1]["stop_reason"] == "error"
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "working"

    harness.full = False
    with client.stream("POST", f"/v1/runs/{run_id}/resume", json={"messages": msgs}) as r:
        evs = _sse(r)
    assert evs[-1]["stop_reason"] == "end_turn"
    log = client.get(f"/v1/runs/{run_id}/events").json()
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "completed"
    assert len([e for e in log if e["type"] == "tool.executed"]) == 1


# ---------------- 上下文预算：图片引用 ----------------


def _image(n: int) -> dict[str, Any]:
    return {"type": "image_ref", "ref": f"artifact://{n:032x}", "media_type": "image/png", "size": 10, "sha256": "0" * 64}


def test_image_refs_counted_at_1500_tokens_each():
    history: list[dict[str, Any]] = [
        {"role": "user", "content": "看图"},
        {"role": "user", "content": [{"type": "text", "text": "图片附件"}, _image(1), _image(2)]},
        {"role": "assistant", "content": "好的"},
    ]
    assert budget.count_image_refs(history) == 2
    text = json.dumps(history, ensure_ascii=False)
    plan = budget.assemble({"history": text}, history_images=2)
    assert plan.usage["history"] == budget.estimate_tokens(text) + 2 * budget.IMAGE_REF_TOKENS == budget.estimate_tokens(text) + 3000
    assert plan.truncated == []
    # 无图片时与此前完全一致（快照不漂移）
    assert budget.assemble({"history": text}).usage["history"] == budget.estimate_tokens(text)
    # 图片把 history 推过预算：记截断
    tight = budget.assemble({"history": text}, {"history": 2_000}, history_images=2)
    assert "history" in tight.truncated


def test_context_assembled_reports_image_tokens(client, monkeypatch: pytest.MonkeyPatch):
    """经 runner：带一张图片附件的 Run，CONTEXT_ASSEMBLED.usage.history = 初始消息文本估算 + 1500。"""
    from test_s6_multimodal import ALIAS, H, _artifact, _chat, _own_run, _vision_agent

    from runtime.api import messages

    monkeypatch.setattr(messages, "settings", dataclasses.replace(messages.settings, vision_model_aliases=(ALIAS,)))
    png = b"\x89PNG\r\n\x1a\n" + os.urandom(32)
    ref = _artifact(_own_run(client), png)
    agent, _ = _vision_agent(client)
    r = _chat(client, agent, [ref])
    assert r.status_code == 200, r.text
    events = client.get(f"/v1/runs/{r.json()['run_id']}/events", headers=H).json()
    assembled = next(e["payload"] for e in events if e["type"] == "context.assembled")
    part = {"type": "image_ref", "ref": ref, "media_type": "image/png", "size": len(png), "sha256": hashlib.sha256(png).hexdigest()}
    initial = [{"role": "user", "content": "看图"}, {"role": "user", "content": [{"type": "text", "text": f"图片附件：{ref}"}, part]}]
    text = json.dumps(initial, ensure_ascii=False)
    assert assembled["usage"]["history"] == budget.estimate_tokens(text) + budget.IMAGE_REF_TOKENS


# ---------------- 门禁：unavailable ----------------


def _missing_factory(script: Any) -> Any:
    raise FileNotFoundError("goose binary not found (set GOOSE_BIN or install block-goose-cli)")


@asynccontextmanager
async def _broken_factory(script: Any) -> Any:
    raise RuntimeError("boom")
    yield  # pragma: no cover


KNOWN_FOR_TEST = {"missing": f"{__name__}:_missing_factory", "broken": f"{__name__}:_broken_factory"}


async def test_gate_missing_binary_is_unavailable_and_fails_closed_by_default():
    result = await run_gate(KNOWN_FOR_TEST, harnesses=["missing"])
    rec = result["harnesses"]["missing"]
    assert rec["unavailable"] is True and rec["passed"] is False and "unavailable" in rec["error"]
    assert result["passed"] is False  # 默认失败关闭（make probe-real）


async def test_gate_allow_unavailable_tolerates_only_unavailable():
    ok = await run_gate(KNOWN_FOR_TEST, harnesses=["missing"], allow_unavailable=["missing"])
    assert ok["passed"] is True and ok["harnesses"]["missing"]["unavailable"] is True
    # 其他异常不是"不可用"：即便点名也照样失败
    bad = await run_gate(KNOWN_FOR_TEST, harnesses=["broken"], allow_unavailable=["broken"])
    assert bad["passed"] is False and not bad["harnesses"]["broken"].get("unavailable")

"""S2.3 统一审计、S2.4 密钥注入、S2.6 Prompt Cache 命中统计。"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import select
from test_runner_stop_reasons import _run_with

from ai_contract import DoneEvent, TextEvent, UsageEvent
from runtime import config, secret_store
from runtime.api.audit import result_of
from runtime.state.db import Audit, SessionLocal

H = {"X-AI-Subject": "human:alice", "X-AI-Tenant": "default"}


@pytest.fixture(scope="module")
def exporter(client: Any) -> InMemorySpanExporter:
    from runtime.observe import tracing

    exp = InMemorySpanExporter()
    tracing.add_span_processor(SimpleSpanProcessor(exp))
    return exp


async def _audit_rows(**where: str) -> list[Audit]:
    async with SessionLocal() as s:
        q = select(Audit).order_by(Audit.id)
        for k, v in where.items():
            q = q.where(getattr(Audit, k) == v)
        return list((await s.execute(q)).scalars())


# ---------------- S2.3 统一审计 ----------------


async def test_every_api_request_is_audited(client: Any, exporter: InMemorySpanExporter) -> None:
    """一次对话请求在 t_audit 中能回答：谁、何时、以何身份、调了什么、结果；并镜像为 audit span（与 Run 同一 Langfuse 会话）。"""
    body = {"agent_id": "demo", "messages": [{"role": "user", "content": "hi"}], "stream": False}
    run_id = client.post("/v1/messages", json=body, headers=H).json()["run_id"]
    [row] = await _audit_rows(object=f"run:{run_id}", action="POST /v1/messages")
    assert (row.actor, row.auth_method, row.tenant_id, row.result) == ("human:alice", "dev", "default", "ok")
    assert row.ts is not None and row.detail["status_code"] == 200 and row.detail["duration_ms"] >= 0
    assert row.trace_id  # Run 的执行 Trace，便于从审计跳到 Langfuse
    span = next(s for s in exporter.get_finished_spans() if s.attributes.get("ai.audit.object") == f"run:{run_id}")
    assert span.name == "audit POST /v1/messages"
    assert span.attributes["langfuse.session.id"] == run_id and span.attributes["langfuse.user.id"] == "human:alice"


async def test_failed_and_route_templated_requests_are_audited(client: Any) -> None:
    """失败请求同样入审计；action 用路由模板（低基数），对象由路径参数推导。"""
    assert client.get("/v1/runs/run-missing", headers=H).status_code == 404
    rows = await _audit_rows(object="run:run-missing")
    assert rows and rows[-1].action == "GET /v1/runs/{run_id}" and rows[-1].result == "rejected"


async def test_healthz_is_not_audited(client: Any) -> None:
    before = len(await _audit_rows())
    client.get("/healthz")
    assert len(await _audit_rows()) == before


async def test_tool_decision_is_audited(client: Any) -> None:
    """工具闸口判定落 tool.decision 审计：发起主体、执行 Agent、工具、结论与策略决策号。"""
    body = {"agent_id": "demo", "messages": [{"role": "user", "content": "!tool echo"}], "stream": False}
    run_id = client.post("/v1/messages", json=body, headers=H).json()["run_id"]
    rows = [r for r in await _audit_rows(action="tool.decision") if r.detail.get("run_id") == run_id]
    assert len(rows) == 1
    row = rows[0]
    assert (row.actor, row.act, row.object, row.result) == ("human:alice", "agent:demo", "tool:echo", "allow")
    assert "policy_decision_id" in row.detail and row.detail["params_hash"]


async def test_admin_writes_are_audited_with_identity(client: Any) -> None:
    r = client.post("/admin/agents", json={"agent_id": "audit-probe", "version": "1"}, headers=H)
    assert r.status_code == 201
    [row] = await _audit_rows(action="agent.create", object="agent:audit-probe@1")
    assert (row.actor, row.auth_method, row.tenant_id) == ("human:alice", "dev", "default")


def test_result_mapping() -> None:
    assert [result_of(c) for c in (200, 202, 401, 403, 404, 409, 500)] == ["ok", "ok", "denied", "denied", "rejected", "rejected", "error"]


# ---------------- S2.4 密钥注入 ----------------


@pytest.fixture
def clean_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("VAULT_ADDR", "VAULT_TOKEN", "VAULT_TOKEN_FILE", "MODEL_GATEWAY_API_KEY", "MODEL_GATEWAY_API_KEY_FILE"):
        monkeypatch.delenv(k, raising=False)
    secret_store.vault_data.cache_clear()
    yield
    secret_store.vault_data.cache_clear()


def test_secret_resolution_order(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clean_vault: None) -> None:
    """Vault > <NAME>_FILE > 环境变量 > 默认值。"""
    assert secret_store.get("MODEL_GATEWAY_API_KEY", "dflt") == "dflt"
    assert secret_store.source("MODEL_GATEWAY_API_KEY") == "default"
    monkeypatch.setenv("MODEL_GATEWAY_API_KEY", "from-env")
    assert (secret_store.get("MODEL_GATEWAY_API_KEY"), secret_store.source("MODEL_GATEWAY_API_KEY")) == ("from-env", "env")
    f = tmp_path / "key"
    f.write_text("from-file\n")
    monkeypatch.setenv("MODEL_GATEWAY_API_KEY_FILE", str(f))
    assert (secret_store.get("MODEL_GATEWAY_API_KEY"), secret_store.source("MODEL_GATEWAY_API_KEY")) == ("from-file", "file")

    seen: dict[str, Any] = {}

    def fake_get(url: str, headers: dict[str, str], timeout: float) -> httpx.Response:
        seen.update(url=url, token=headers["X-Vault-Token"])
        return httpx.Response(200, json={"data": {"data": {"MODEL_GATEWAY_API_KEY": "from-vault", "UNRELATED": "x"}}})

    monkeypatch.setattr(secret_store.httpx, "get", fake_get)
    monkeypatch.setenv("VAULT_ADDR", "http://vault.test:8200")
    monkeypatch.setenv("VAULT_TOKEN", "t-1")
    secret_store.vault_data.cache_clear()  # Vault 只在进程启动时读一次：模拟重新启动
    assert (secret_store.get("MODEL_GATEWAY_API_KEY"), secret_store.source("MODEL_GATEWAY_API_KEY")) == ("from-vault", "vault")
    assert seen == {"url": "http://vault.test:8200/v1/secret/data/ai-platform/runtime", "token": "t-1"}
    assert "UNRELATED" not in secret_store.vault_data()  # 只取登记的密钥项


def test_vault_failure_fails_fast(monkeypatch: pytest.MonkeyPatch, clean_vault: None) -> None:
    monkeypatch.setenv("VAULT_ADDR", "http://vault.test:8200")
    with pytest.raises(secret_store.SecretError, match="VAULT_TOKEN"):
        secret_store.get("MODEL_GATEWAY_API_KEY")
    monkeypatch.setenv("VAULT_TOKEN", "t-1")
    monkeypatch.setattr(secret_store.httpx, "get", lambda *a, **k: httpx.Response(403))
    with pytest.raises(secret_store.SecretError, match="HTTP 403"):
        secret_store.get("MODEL_GATEWAY_API_KEY")


def test_production_baseline(monkeypatch: pytest.MonkeyPatch, clean_vault: None) -> None:
    """RUNTIME_ENV=prod：缺鉴权配置、开发主密钥、明文环境变量密钥都拒绝启动；dev 不检查。"""
    monkeypatch.setenv("MODEL_GATEWAY_API_KEY", "sk-real")
    prod = dataclasses.replace(config.settings, runtime_env="prod", model_gateway_api_key="sk-real")
    problems = config.production_problems(prod)
    assert any("AUTH_REQUIRED" in p for p in problems) and any("JWT_AUDIENCE" in p for p in problems)
    assert any("MODEL_GATEWAY_API_KEY 来自明文环境变量" in p for p in problems)
    with pytest.raises(RuntimeError, match="安全基线"):
        config.validate_for_production(prod)
    config.validate_for_production(dataclasses.replace(prod, runtime_env="dev"))  # dev 不抛

    monkeypatch.delenv("MODEL_GATEWAY_API_KEY")
    monkeypatch.setenv("MODEL_GATEWAY_API_KEY_FILE", "/vault/secrets/model-gateway-api-key")
    ok = dataclasses.replace(
        prod,
        auth_required=True,
        jwks_url="https://idp/certs",
        jwt_issuer="https://idp",
        jwt_audience="ai-runtime",
        database_url="sqlite+aiosqlite:///./runtime.db",
    )
    monkeypatch.setattr(secret_store, "source", lambda name: "file")  # 其余密钥项同样经文件注入
    assert any("SQLite" in p for p in config.production_problems(ok))  # 数据库仍是默认 SQLite
    ok = dataclasses.replace(ok, database_url="mysql+asyncmy://u:p@db:3306/ai", tool_dispatch="gateway", credential_enc_key="ck-real")
    # S8：调用能力网关须带服务令牌——缺令牌端点与服务客户端密钥均拒绝启动
    assert [p for p in config.production_problems(ok) if "RUNTIME_GATEWAY" in p] == [
        "缺少 RUNTIME_GATEWAY_TOKEN_URL（调用能力网关须带 ai-runtime-svc 服务令牌）",
        "RUNTIME_GATEWAY_CLIENT_SECRET 未配置（服务客户端 ai-runtime-svc 的密钥）",
    ]
    assert "POLICY_SERVICE_TOKEN 未配置（调用策略服务的服务间令牌）" in config.production_problems(ok)
    ok = dataclasses.replace(ok, gateway_token_url="https://idp/token", gateway_client_secret="svc-real")  # noqa: S106
    ok = dataclasses.replace(ok, policy_service_token="pst-real")  # noqa: S106
    assert config.production_problems(ok) == ["HOOK_DIGEST_KEY 未配置或仍为开发默认值"]  # hook 指纹密钥仍是开发默认值
    ok = dataclasses.replace(ok, hook_digest_key="hk-real")
    assert config.production_problems(ok) == []
    assert "hk-real" not in repr(ok)
    assert "u:p" not in repr(ok) and "sk-real" not in repr(ok) and "ck-real" not in repr(ok)  # 密钥项不进 repr（日志、异常栈）
    assert "svc-real" not in repr(ok) and "pst-real" not in repr(ok)
    assert config.production_problems(dataclasses.replace(ok, credential_enc_key=config.DEV_CREDENTIAL_ENC_KEY)) == [
        "CREDENTIAL_ENC_KEY 未配置或仍为开发默认值"
    ]
    assert config.production_problems(dataclasses.replace(ok, model_gateway_api_key=config.DEV_GATEWAY_KEY))


# ---------------- S2.6 Prompt Cache 命中统计 ----------------


def test_prompt_cache_stats_in_event_log_and_trace(client: Any, exporter: InMemorySpanExporter) -> None:
    """MODEL_RESPONSE 事件与模型 span 都带稳定前缀哈希与命中统计；hit_ratio = cache_read / input。"""
    turns = [[TextEvent(delta="ok"), UsageEvent(input_tokens=200, output_tokens=5, cache_read_tokens=150), DoneEvent(run_id="x")]]
    _, run_id, _ = _run_with(client, "cache-probe", turns)
    log = client.get(f"/v1/runs/{run_id}/events").json()
    prefix = next(e for e in log if e["type"] == "context.assembled")["payload"]["stable_prefix_hash"]
    cache = next(e for e in log if e["type"] == "model.response")["payload"]["prompt_cache"]
    assert cache == {"stable_prefix_hash": prefix, "input_tokens": 200, "cache_read_tokens": 150, "hit_ratio": 0.75}
    span = next(s for s in exporter.get_finished_spans() if s.name.startswith("chat ") and s.parent and _run_of(exporter, s) == run_id)
    assert span.attributes["ai.context.stable_prefix_hash"] == prefix
    assert span.attributes["ai.prompt_cache.hit_ratio"] == 0.75
    assert json.loads(span.attributes["langfuse.observation.usage_details"]) == {
        "input": 50,
        "input_cached_tokens": 150,
        "output": 5,
        "total": 205,
    }


def _run_of(exporter: InMemorySpanExporter, span: Any) -> str | None:
    """子 span 所属 Run：找到同一 Trace 的根 span 取 ai.run_id。"""
    for s in exporter.get_finished_spans():
        if s.context.trace_id == span.context.trace_id and s.parent is None and "ai.run_id" in (s.attributes or {}):
            return str(s.attributes["ai.run_id"])
    return None

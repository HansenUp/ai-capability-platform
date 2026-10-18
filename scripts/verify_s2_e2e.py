#!/usr/bin/env python3
"""S2 本机出口验证：身份（Keycloak JWT）、统一审计、Vault 密钥注入、Prompt Cache 命中统计。

前置：compose 已启动 keycloak、vault（含 vault-init）、litellm 与 observe profile（OTel Collector、Langfuse）；已 `make cedar-build`
（闸口经真实策略栈判定：本脚本以子进程启动 Cedar 引擎 + 策略服务）。
Runtime 以 AUTH_REQUIRED=true 运行在本进程内（临时 SQLite），网关 Key 只从 Vault 读取（不设环境变量）。

出口标准与对应检查：
1. 未带令牌请求被 401：/v1/messages、/admin/agents 未带令牌 401；应用令牌调管理接口 403。
2. 任意一次请求可在审计表中回答"谁、何时、以何身份、调了什么"：逐条打印本次验证产生的 t_audit 行，
   并断言 401 / 403 / 对话请求 / 工具判定各有一行且身份字段齐全。
3. Prompt Cache 命中率在 Langfuse 可统计：同一 Agent（长系统提示词）多次对话（间隔重试，等厂商建好前缀缓存），应有一次命中；
   在 Langfuse 中按 Trace 取模型轮次（GENERATION）的 usageDetails，计算命中率并与事件日志对账。

用法：
    .venv/bin/python scripts/verify_s2_e2e.py
退出码 0 表示全部通过。
"""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit  # noqa: E402

KEYCLOAK = os.environ.get("KEYCLOAK_URL", "http://localhost:8180")
REALM = f"{KEYCLOAK}/realms/ai-platform"
_tmp = tempfile.mkdtemp(prefix="s2-e2e-")
os.environ.update(
    {
        "DATABASE_URL": f"sqlite+aiosqlite:///{_tmp}/runtime.db",
        "WORKSPACE_DIR": f"{_tmp}/ws",
        "HARNESS": "litellm",
        "POLICY_URL": devkit.start_policy_stack(Path(_tmp)),  # 真实策略栈（Cedar）；echo 为只读工具，同租户放行
        "CAPABILITY_GATEWAY_URL": "http://127.0.0.1:1",
        "AUTH_REQUIRED": "true",
        "JWKS_URL": f"{REALM}/protocol/openid-connect/certs",
        "JWT_ISSUER": REALM,
        "JWT_AUDIENCE": "ai-runtime",
        "VAULT_ADDR": os.environ.get("VAULT_ADDR", "http://localhost:8200"),
        "VAULT_TOKEN": os.environ.get("VAULT_TOKEN", "dev-root"),  # compose 中 Vault dev 服务器的开发根令牌
    }
)
os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
for _k in ("MODEL_GATEWAY_API_KEY", "MODEL_GATEWAY_API_KEY_FILE", "CLAUDE_SDK_AUTH_TOKEN"):
    os.environ.pop(_k, None)  # 网关 Key 只能来自 Vault

from fastapi.testclient import TestClient  # noqa: E402  —— 必须在设置环境变量之后导入
from sqlalchemy import select  # noqa: E402

from runtime import secret_store  # noqa: E402
from runtime.main import app  # noqa: E402
from runtime.observe import tracing  # noqa: E402
from runtime.state.db import Audit, SessionLocal  # noqa: E402

ALIAS = os.environ.get("S2_MODEL_ALIAS", "deepseek-v4-pro-2026-08")
LANGFUSE = os.environ.get("LANGFUSE_HOST", "http://localhost:3000")
_pk = os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-dev")
_sk = os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-dev")
LF_AUTH = {"Authorization": "Basic " + base64.b64encode(f"{_pk}:{_sk}".encode()).decode()}
# 足够长的固定系统提示词：厂商的前缀缓存有最小长度（DeepSeek 以 64 token 为单位、Anthropic 至少 1024 token）
SYSTEM = "你是售后工单助手，回答务必简短。" + "".join(
    f"规则{i}：处理工单时先确认工单号、客户等级与故障现象，再给出下一步建议。" for i in range(80)
)

CACHE_RETRIES = 4  # 首次对话之后最多再试几次
CACHE_WAIT_S = 10.0  # 每次重试前等待（秒）

failures: list[str] = []


def check(ok: bool, what: str) -> None:
    print(f"  [{'通过' if ok else '失败'}] {what}")
    if not ok:
        failures.append(what)


def token(client_id: str) -> str:
    """Client Credentials 取短期令牌（本机 realm 中的开发用客户端密钥，见 services/identity/realm-export.json）。"""
    r = httpx.post(
        f"{REALM}/protocol/openid-connect/token",
        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": f"{client_id}-dev-secret"},
        timeout=10,
    )
    r.raise_for_status()
    return str(r.json()["access_token"])


def bearer(t: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {t}"}


def chat(client: TestClient, headers: dict[str, str], agent_id: str, text: str) -> dict[str, Any]:
    r = client.post(
        "/v1/messages", json={"agent_id": agent_id, "messages": [{"role": "user", "content": text}], "stream": False}, headers=headers
    )
    assert r.status_code == 200, r.text
    return dict(r.json())


def langfuse_generations(trace_id: str, expected: int, timeout_s: float = 90) -> list[dict[str, Any]]:
    """轮询 Langfuse，直到该 Trace 的模型轮次（GENERATION）全部到齐并带上用量明细（摄取是异步的，逐条到达）。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = httpx.get(f"{LANGFUSE}/api/public/traces/{trace_id}", headers=LF_AUTH, timeout=10)
        if r.status_code == 200:
            gens = [o for o in r.json().get("observations", []) if o.get("type") == "GENERATION" and o.get("usageDetails")]
            if len(gens) >= expected:
                return gens
        time.sleep(3)
    return []


def langfuse_session_traces(session_id: str, timeout_s: float = 90) -> list[str]:
    """该会话（run_id）下的 Trace 名称：应同时有执行 Trace 与审计镜像。"""
    deadline = time.time() + timeout_s
    names: list[str] = []
    while time.time() < deadline:
        r = httpx.get(f"{LANGFUSE}/api/public/traces", params={"sessionId": session_id}, headers=LF_AUTH, timeout=10)
        if r.status_code == 200:
            names = [t.get("name") or "" for t in r.json().get("data", [])]
            if any(n.startswith("audit ") for n in names) and any(n.startswith("run ") for n in names):
                return names
        time.sleep(3)
    return names


async def audit_rows() -> list[Audit]:
    async with SessionLocal() as s:
        return list((await s.execute(select(Audit).order_by(Audit.id))).scalars())


def main() -> int:
    import asyncio

    print("== S2.4 密钥注入")
    check(secret_store.source("MODEL_GATEWAY_API_KEY") == "vault", "网关 Key 来自 Vault（环境变量中没有该密钥）")

    app_tok, admin_tok = token("agent-demo"), token("console-api")
    with TestClient(app) as client:
        print("== S2.1 / S2.2 身份")
        r = client.post("/v1/messages", json={"agent_id": "x", "messages": []})
        check(r.status_code == 401 and r.headers.get("www-authenticate", "").startswith("Bearer"), "未带令牌调 /v1/messages → 401")
        check(client.get("/admin/agents").status_code == 401, "未带令牌调 /admin/agents → 401")
        check(client.get("/admin/agents", headers=bearer("x.y.z")).status_code == 401, "伪造令牌 → 401")
        check(client.get("/admin/agents", headers=bearer(app_tok)).status_code == 403, "应用令牌（无 platform-admin）调管理接口 → 403")

        agent = {"agent_id": "s2-cache", "version": "1", "harness": "litellm", "model_alias": ALIAS, "system_prompt": SYSTEM}
        agent["config"] = {"tools": ["echo"]}
        check(
            client.post("/admin/agents", json=agent, headers=bearer(admin_tok)).status_code == 201,
            "console-api（platform-admin）创建 Agent",
        )
        r = client.post("/admin/agents/s2-cache/versions/1/publish", json={"eval_passed": True}, headers=bearer(admin_tok))
        check(r.status_code == 200, "console-api 发布 Agent")

        print("== S2.6 Prompt Cache（同一前缀多次对话）")
        # 厂商侧前缀缓存是异步建立的（DeepSeek 实测需十余秒）：首次对话后间隔重试，直到某次命中或用完次数
        question = "工单 T-1001 客户反馈无法开机，下一步怎么做？"
        runs: list[dict[str, Any]] = []
        caches: list[dict[str, Any]] = []
        for attempt in range(1 + CACHE_RETRIES):
            if attempt:
                time.sleep(CACHE_WAIT_S)
            # 问题末尾带序号：系统提示词与工具这段前缀不变（命中厂商 Prompt Cache），整条请求又各不相同，
            # 不会被 LiteLLM 的 Redis 响应缓存原样返回（响应缓存回放的是首次请求的用量，看不到厂商侧命中）
            run = chat(client, bearer(app_tok), "s2-cache", f"{question}（第 {attempt + 1} 次）")
            runs.append(run)
            events = client.get(f"/v1/runs/{run['run_id']}/events", headers=bearer(app_tok)).json()
            run["caches"] = [e["payload"]["prompt_cache"] for e in events if e["type"] == "model.response"]
            caches += run["caches"]
            if attempt and caches[-1]["cache_read_tokens"] > 0:
                break
        tool_run = chat(client, bearer(app_tok), "s2-cache", "请调用 echo 工具回显“葡萄”，然后告诉我结果。")
        for c in caches:
            print(
                f"  事件日志：prefix={c['stable_prefix_hash'][:12]} input={c['input_tokens']} "
                f"cached={c['cache_read_tokens']} hit={c['hit_ratio']}"
            )
        check(len({c["stable_prefix_hash"] for c in caches}) == 1, "多次对话稳定前缀哈希一致")
        check(caches[-1]["cache_read_tokens"] > 0, f"第 {len(runs)} 次对话命中 Prompt Cache（cache_read_tokens > 0）")
    tracing.force_flush()

    print("== S2.3 统一审计（本次验证产生的 t_audit）")
    rows = asyncio.run(audit_rows())
    for a in rows:
        print(
            f"  {a.ts:%H:%M:%S} actor={a.actor} act={a.act} auth={a.auth_method} tenant={a.tenant_id} "
            f"action={a.action} object={a.object} result={a.result}"
        )
    check(
        any(a.actor == "anonymous" and a.result == "denied" and a.detail.get("status_code") == 401 for a in rows),
        "401 请求入审计（anonymous / denied）",
    )
    check(
        any(a.actor == "app:agent-demo" and a.detail.get("status_code") == 403 for a in rows), "403 请求入审计（app:agent-demo / denied）"
    )
    chat_rows = [a for a in rows if a.action == "POST /v1/messages" and a.result == "ok"]
    check(
        len(chat_rows) == len(runs) + 1
        and all(
            (a.actor, a.act, a.auth_method, a.tenant_id) == ("app:agent-demo",) * 2 + ("client_credentials", "default") for a in chat_rows
        ),
        "对话请求入审计：谁 / 以何身份 / 租户齐全",
    )
    check(all(a.trace_id for a in chat_rows), "对话请求审计行带 Run 的 trace_id")
    check(any(a.action == "tool.decision" and a.object == "tool:echo" and a.result == "allow" for a in rows), "工具闸口判定入审计")
    check(any(a.action == "agent.publish" and a.actor == "app:console-api" for a in rows), "管理写操作入审计")

    print("== Langfuse：Prompt Cache 命中率与审计镜像")
    trace_ids = {a.object.removeprefix("run:"): a.trace_id for a in chat_rows}
    total_in = total_cached = 0
    for run in runs:
        gens = langfuse_generations(trace_ids[run["run_id"]] or "", len(run["caches"]))
        for g in gens:
            u = g["usageDetails"]
            total_in += int(u.get("input", 0)) + int(u.get("input_cached_tokens", 0))
            total_cached += int(u.get("input_cached_tokens", 0))
        check(bool(gens), f"Langfuse 查到 {run['run_id']} 的模型轮次用量明细")
    ratio = total_cached / total_in if total_in else 0.0
    print(f"  Langfuse 统计：{len(runs)} 次对话输入 {total_in} token，命中缓存 {total_cached} token，命中率 {ratio:.1%}")
    expected = sum(c["cache_read_tokens"] for c in caches)
    check(total_cached == expected and total_cached > 0, "Langfuse 命中 token 与事件日志一致且大于 0")
    names = langfuse_session_traces(tool_run["run_id"])
    check(any(n == "audit POST /v1/messages" for n in names), f"审计镜像与执行 Trace 同会话（{sorted(set(names))}）")

    print("S2 端到端验证：" + ("全部通过" if not failures else f"{len(failures)} 项失败"))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())

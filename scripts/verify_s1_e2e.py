#!/usr/bin/env python3
"""S1 本机出口验证：litellm 与 claude-sdk 两种 Harness 经 /v1/messages 与真实模型对话，并在 Langfuse 查到 Trace。

前置：compose 已启动且含 observe profile（LiteLLM、OTel Collector、Langfuse 在线），`.env` 中已配置模型 Key；
已 `make cedar-build`（闸口经真实策略栈判定：本脚本以子进程启动 Cedar 引擎 + 策略服务）。
每种 Harness 跑两次：一次纯对话，一次要求调用 echo 工具；断言 Run 终态为 completed、每轮都有 usage，
再到 Langfuse 按 trace_id 查询，确认 Trace 含根 span 且 sessionId 等于 run_id。

用法：
    .venv/bin/python scripts/verify_s1_e2e.py                     # 默认模型别名 deepseek-v4-pro-2026-08
    S1_MODEL_ALIAS=claude-sonnet-2026-06 .venv/bin/python scripts/verify_s1_e2e.py

退出码 0 表示全部通过。Runtime 使用临时 SQLite，不影响本机数据库。
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="s1-e2e-")
os.environ.update(
    {
        "DATABASE_URL": f"sqlite+aiosqlite:///{_tmp}/runtime.db",
        "WORKSPACE_DIR": f"{_tmp}/ws",
        "HARNESS": "litellm",
        # 闸口经真实策略栈判定（Cedar；策略不可达即拒绝）；echo 为只读工具，同租户放行
        "POLICY_URL": devkit.start_policy_stack(Path(_tmp)),
        "CAPABILITY_GATEWAY_URL": "http://127.0.0.1:1",
    }
)
os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")

from fastapi.testclient import TestClient  # noqa: E402  —— 必须在设置环境变量之后导入

from runtime.main import app  # noqa: E402
from runtime.observe import tracing  # noqa: E402

ALIAS = os.environ.get("S1_MODEL_ALIAS", "deepseek-v4-pro-2026-08")
LANGFUSE = os.environ.get("LANGFUSE_HOST", "http://localhost:3000")
_pk = os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-dev")
_sk = os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-dev")
AUTH = "Basic " + base64.b64encode(f"{_pk}:{_sk}".encode()).decode()

CASES: list[tuple[str, list[str]]] = [("litellm", []), ("claude-sdk", []), ("litellm", ["echo"]), ("claude-sdk", ["echo"])]


def run_case(client: TestClient, harness: str, tools: list[str]) -> tuple[str, dict[str, Any], bool]:
    """创建并发布一个 Agent，经 /v1/messages 对话一次，返回（agent_id、最终 done 事件、是否通过）。"""
    aid = f"e2e-{harness}-{'tool' if tools else 'chat'}"
    prompt = "请调用 echo 工具回显“葡萄”，然后告诉我结果。" if tools else "用一句话介绍你自己。"
    body = {
        "agent_id": aid,
        "version": "1",
        "harness": harness,
        "model_alias": ALIAS,
        "system_prompt": "你是测试助手。回答尽量简短。" + ("用户要求时必须调用 echo 工具。" if tools else ""),
        "config": {"tools": tools},
    }
    assert client.post("/admin/agents", json=body).status_code == 201
    assert client.post(f"/admin/agents/{aid}/versions/1/publish", json={"eval_passed": True}).status_code == 200
    evs: list[dict[str, Any]] = []
    msg = {"agent_id": aid, "messages": [{"role": "user", "content": prompt}]}
    with client.stream("POST", "/v1/messages", json=msg) as resp:
        assert resp.status_code == 200, resp.text
        evs = [json.loads(line[5:]) for line in resp.iter_lines() if line.startswith("data:")]
    done = evs[-1]
    state = client.get(f"/v1/runs/{done['run_id']}").json()["state"]
    text = "".join(e["delta"] for e in evs if e["type"] == "text")
    usage = [(e["input_tokens"], e["output_tokens"], e["model"]) for e in evs if e["type"] == "usage"]
    used_tool = any(e["type"] == "tool_use" for e in evs)
    ok = state == "completed" and bool(text.strip()) and bool(usage) and (used_tool or not tools)
    print(f"[{aid}] {'通过' if ok else '失败'} state={state} tool_use={used_tool} usage={usage}")
    print(f"[{aid}] text={text[:100]!r}")
    return aid, done, ok


def langfuse_trace(trace_id: str, timeout_s: float = 90) -> dict[str, Any] | None:
    """轮询 Langfuse，直到该 Trace 带 sessionId 且已收到根 span（摄取是异步的）。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = httpx.get(f"{LANGFUSE}/api/public/traces/{trace_id}", headers={"Authorization": AUTH}, timeout=10)
        if r.status_code == 200:
            t: dict[str, Any] = r.json()
            obs = t.get("observations", [])
            if t.get("sessionId") and any(o.get("type") == "SPAN" for o in obs):
                return t
        time.sleep(3)
    return None


def main() -> int:
    ok = True
    with TestClient(app) as client:
        results = [run_case(client, h, t) for h, t in CASES]
        tracing.force_flush()
    for aid, done, case_ok in results:
        ok &= case_ok
        trace = langfuse_trace(done.get("trace_id") or "")
        if trace is None or trace.get("sessionId") != done["run_id"]:
            print(f"[{aid}] Langfuse：未查到完整 Trace（trace_id={done.get('trace_id')}）")
            ok = False
            continue
        names = [f"{o.get('type')}:{o.get('name')}" for o in trace["observations"]]
        print(f"[{aid}] Langfuse：session={trace['sessionId']} observations={names}")
    print("S1 端到端验证：" + ("全部通过" if ok else "存在失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

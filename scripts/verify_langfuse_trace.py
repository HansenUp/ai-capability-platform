#!/usr/bin/env python3
"""本机端到端验证（S1.5）：Runtime 的 Run Trace 经 OTel Collector 到达 Langfuse，且 span 上的事件 seq 与 Run 事件日志对应。

前置：`docker compose -f deploy/compose-dev/docker-compose.yml --profile observe up -d`（otel-collector + Langfuse，
Langfuse 由 LANGFUSE_INIT_* 无头初始化出项目与密钥）。

做法：在进程内以 TestClient 启动 Runtime（mock harness、临时 SQLite），发布一个验证用 Agent，发起一次
`!tool echo {...}` 对话（产生 chat → execute_tool → chat 三个子 span），force_flush 后轮询 Langfuse
`GET /api/public/traces/{trace_id}`（Basic 公钥:私钥，最多 60 s），打印 Trace 名称、observation 数量与各
observation 的 `ai.event.seq.*` 属性，并与 `/v1/runs/{id}/events` 的 seq 逐一核对。

用法：
    .venv/bin/python scripts/verify_langfuse_trace.py
环境变量（均有本机默认值）：OTEL_EXPORTER_OTLP_ENDPOINT（http://localhost:4318）、LANGFUSE_HOST（http://localhost:3000）、
LANGFUSE_PUBLIC_KEY（pk-lf-dev）、LANGFUSE_SECRET_KEY（sk-lf-dev）。
退出码：0 = Langfuse 可查到 Trace 且 seq 与 Run 事件对应；1 = 超时或不一致。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import uuid
from typing import Any

# 必须在导入 runtime 之前设置：Settings 在模块导入时读取环境变量。
_tmp = tempfile.mkdtemp(prefix="verify-langfuse-")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp}/runtime.db"
os.environ["WORKSPACE_DIR"] = f"{_tmp}/ws"
os.environ["HARNESS"] = "mock"
os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
os.environ.setdefault("POLICY_URL", "http://127.0.0.1:1")  # 不可达 → 闸口走基线策略（echo 为只读工具，直接放行）
os.environ.setdefault("CAPABILITY_GATEWAY_URL", "http://127.0.0.1:1")
# 本机若配置了 HTTP(S)_PROXY，OTLP 导出（requests）与下面的 httpx 访问 localhost 时需绕过代理。
os.environ["NO_PROXY"] = ",".join(x for x in (os.environ.get("NO_PROXY", ""), "localhost,127.0.0.1") if x)
os.environ["no_proxy"] = os.environ["NO_PROXY"]

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

LANGFUSE = os.environ.get("LANGFUSE_HOST", "http://localhost:3000").rstrip("/")
AUTH = (os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-dev"), os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-dev"))
TIMEOUT_S = 60
SEQ_PREFIX = "ai.event.seq."


def _sse(resp: Any) -> list[dict[str, Any]]:
    """解析 SSE 响应体中的 data 帧。"""
    return [json.loads(line[5:].strip()) for line in resp.iter_lines() if line.startswith("data:")]


def _find_seq_attrs(node: Any, out: dict[str, int]) -> None:
    """在 observation 的 metadata（Langfuse 把 OTel span 属性放在其中，结构随版本略有差异）里递归找 ai.event.seq.* 键。"""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and k.startswith(SEQ_PREFIX) and not isinstance(v, dict | list):
                out[k] = int(v)
            else:
                _find_seq_attrs(v, out)
    elif isinstance(node, list):
        for v in node:
            _find_seq_attrs(v, out)


def _run_once(agent_id: str) -> tuple[str, str, list[dict[str, Any]]]:
    """启动进程内 Runtime，发布 Agent 并跑一次工具对话；返回 (run_id, trace_id, 事件日志)。"""
    from runtime.main import app
    from runtime.observe import tracing

    with TestClient(app) as c:
        r = c.post(
            "/admin/agents", json={"agent_id": agent_id, "version": "1", "system_prompt": "你是验证助手。", "config": {"tools": ["echo"]}}
        )
        assert r.status_code == 201, r.text
        assert c.post(f"/admin/agents/{agent_id}/versions/1/publish", json={"eval_passed": True}).status_code == 200
        body = {"agent_id": agent_id, "messages": [{"role": "user", "content": '!tool echo {"verify": "langfuse"}'}]}
        with c.stream("POST", "/v1/messages", json=body) as resp:
            run_id = resp.headers["x-run-id"]
            events = _sse(resp)
        done = events[-1]
        assert done["type"] == "done" and done["trace_id"], f"DoneEvent 缺 trace_id: {done}"
        log = c.get(f"/v1/runs/{run_id}/events").json()
        flushed = tracing.force_flush(10_000)
    print(f"run_id={run_id} trace_id={done['trace_id']} stop_reason={done['stop_reason']} force_flush={flushed}")
    return run_id, done["trace_id"], log


def _poll_trace(trace_id: str, expected_observations: int) -> dict[str, Any] | None:
    """轮询 Langfuse 直到 Trace 可查且 observation 数达到预期（ingestion 为异步），超时返回最后一次结果或 None。"""
    deadline = time.monotonic() + TIMEOUT_S
    last: dict[str, Any] | None = None
    with httpx.Client(base_url=LANGFUSE, auth=AUTH, timeout=10, trust_env=False) as h:
        while time.monotonic() < deadline:
            try:
                r = h.get(f"/api/public/traces/{trace_id}")
            except httpx.TransportError as exc:  # Langfuse 重启 / 未就绪：继续等，超时后按失败处理
                print(f"Langfuse 暂不可达（{type(exc).__name__}），重试中…")
                time.sleep(2)
                continue
            if r.status_code == 200:
                last = r.json()
                if len(last.get("observations") or []) >= expected_observations:
                    return last
            elif r.status_code in (401, 403):
                print(f"Langfuse 鉴权失败（{r.status_code}）：检查 LANGFUSE_PUBLIC_KEY/SECRET_KEY 与 Langfuse 项目密钥。{r.text[:200]}")
                return None
            time.sleep(2)
    return last


def main() -> int:
    """执行端到端验证，打印结果并返回退出码。"""
    agent_id = f"verify-{uuid.uuid4().hex[:6]}"
    run_id, trace_id, log = _run_once(agent_id)
    expected = {
        "ai.event.seq.model_request": [e["seq"] for e in log if e["type"] == "model.request"],
        "ai.event.seq.model_response": [e["seq"] for e in log if e["type"] == "model.response"],
        "ai.event.seq.tool_requested": [e["seq"] for e in log if e["type"] == "tool.requested"],
    }
    trace = _poll_trace(trace_id, expected_observations=4)  # 根 span + chat + execute_tool + chat
    if trace is None:
        print(f"FAIL：{TIMEOUT_S}s 内在 Langfuse 查不到 trace {trace_id}")
        return 1
    observations = sorted(trace.get("observations") or [], key=lambda o: o.get("startTime") or "")
    print(f"Langfuse trace: name={trace.get('name')!r} sessionId={trace.get('sessionId')!r} observations={len(observations)}")
    found: dict[str, list[int]] = {k: [] for k in expected}
    for o in observations:
        seqs: dict[str, int] = {}
        _find_seq_attrs(o.get("metadata"), seqs)
        print(f"  - {o.get('type'):<10} {o.get('name')!r:<28} {json.dumps(seqs, sort_keys=True)}")
        for k, v in seqs.items():
            if k in found:
                found[k].append(v)
    print(f"Run 事件日志 seq: {json.dumps(expected, sort_keys=True)}")
    problems = []
    if trace.get("name") != f"run {agent_id}":
        problems.append(f"trace 名称应为 'run {agent_id}'")
    if trace.get("sessionId") != run_id:
        problems.append(f"sessionId 应为 run_id {run_id}")
    if len(observations) < 4:
        problems.append(f"observation 数 {len(observations)} < 4")
    for k, want in expected.items():
        if sorted(found[k]) != sorted(want):
            problems.append(f"{k}: Langfuse={sorted(found[k])} 事件日志={sorted(want)}")
    if problems:
        print("FAIL：" + "；".join(problems))
        return 1
    print("OK：Langfuse 可查到 Trace，且 observation 上的 ai.event.seq.* 与 Run 事件日志一一对应")
    return 0


if __name__ == "__main__":
    sys.exit(main())

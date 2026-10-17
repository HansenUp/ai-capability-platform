"""发布评测闸门（S4.6）。

两种模式（环境变量 CONSOLE_EVAL_GATE）：
- flag（默认，开发 / 测试）：信任请求体里的 eval_passed=true；
- langfuse：不信任请求体，按 eval_run_ref（形如 `langfuse:<数据集>/<运行名>`）到 Langfuse 读取该评测运行的 metadata，
  要求评测对象正是本次发布的 Agent 版本（agent_id、agent_version 一致）且通过率不低于 EVAL_MIN_PASS_RATE（默认 0.8）。
  评测运行由 `scripts/eval_ticket_summary.py` 生成。Langfuse 不可达或运行不存在一律视为未通过（fail closed）。

闸门只在版本对外放量时生效：以 0% 灰度 + 白名单发布属于预发布（只有白名单里的评测账号可见），用于跑评测本身。
"""

from __future__ import annotations

import base64
import math
import os
from urllib.parse import quote

import httpx

MODE = os.environ.get("CONSOLE_EVAL_GATE", "flag")
MIN_PASS_RATE = float(os.environ.get("EVAL_MIN_PASS_RATE", "0.8"))
_transport: httpx.AsyncBaseTransport | None = None  # 测试注入


def set_transport_for_tests(transport: httpx.AsyncBaseTransport | None) -> None:
    global _transport
    _transport = transport


async def check(agent_id: str, version: str, eval_run_ref: str | None, eval_passed: bool | None) -> str | None:
    """返回 None 表示通过；否则返回拒绝原因。"""
    if MODE != "langfuse":
        return None if eval_passed is True else "evaluation must pass (eval_passed=true with eval_run_ref)"
    if not eval_run_ref or not eval_run_ref.startswith("langfuse:") or "/" not in eval_run_ref:
        return "eval_run_ref must reference a Langfuse dataset run (langfuse:<dataset>/<run>)"
    dataset, run = eval_run_ref.removeprefix("langfuse:").split("/", 1)
    host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000").rstrip("/")
    pk, sk = os.environ.get("LANGFUSE_PUBLIC_KEY", ""), os.environ.get("LANGFUSE_SECRET_KEY", "")
    auth = {"Authorization": "Basic " + base64.b64encode(f"{pk}:{sk}".encode()).decode()}
    try:
        async with httpx.AsyncClient(timeout=10, transport=_transport) as c:
            r = await c.get(f"{host}/api/public/datasets/{quote(dataset, safe='')}/runs/{quote(run, safe='')}", headers=auth)
    except httpx.HTTPError:
        return "Langfuse unavailable; cannot verify evaluation"
    if r.status_code != 200:
        return f"evaluation run not found ({r.status_code})"
    meta = r.json().get("metadata") or {}
    if meta.get("agent_id") != agent_id or str(meta.get("agent_version")) != version:
        return f"evaluation run is for {meta.get('agent_id')}@{meta.get('agent_version')}, not {agent_id}@{version}"
    try:
        rate = float(meta.get("pass_rate") or 0)
    except (TypeError, ValueError):
        rate = 0.0
    if not math.isfinite(rate) or rate < MIN_PASS_RATE:  # nan / inf 一律视为未通过
        return f"evaluation pass rate {rate:.0%} is below {MIN_PASS_RATE:.0%}"
    return None

"""发布评测闸门（S4.6；S8 §2 S4 #4 加固）。

两种模式（环境变量 CONSOLE_EVAL_GATE）：
- flag（默认，开发 / 测试）：信任请求体里的 eval_passed=true；
- langfuse：不信任请求体，按 eval_run_ref（形如 `langfuse:<数据集>/<运行名>`）到 Langfuse 核对该评测运行：
  1) 数据集须在 ``EVAL_DATASETS`` 为该 Agent 配置的名单内（JSON：``{"<agent_id>": ["<数据集>", ...], "*": [...]}``，
     ``*`` 为未单独配置的 Agent 的默认名单；Agent 不在名单内一律拒绝）；
  2) 运行 metadata 的 agent_id / agent_version 须正是本次发布的版本；
  3) 不信任 metadata 的 pass_rate：逐条拉取运行项（dataset run items），按每条关联 Trace 上的 ``eval_pass`` 评分重算——
     按数据集条目去重，同一条目的全部运行项都通过（eval_pass 评分存在且全为 1）才算通过；缺 Trace / 缺评分按不通过计；
  4) 去重后的条目数不少于 ``EVAL_MIN_ITEMS``（默认 30），重算的通过率不低于 ``EVAL_MIN_PASS_RATE``（默认 0.8）；
  5) 防复用 Trace：同一 traceId 不得出现在不同数据集条目下（否则整次运行拒绝）；每条 Trace 的 metadata（Runtime 根 span 的
     ``ai.agent_id`` / ``ai.agent_version`` / ``ai.tenant_id``，Langfuse 置于 metadata.attributes）须与本次发布的 Agent、版本、
     租户一致，且 Trace 时间戳须在评测运行时间窗内（不晚于引用它的运行项创建时间 + ``EVAL_TRACE_SKEW_S``，不早于其
     − ``EVAL_TRACE_MAX_AGE_S``，默认 6 小时）；不满足的条目按不通过计。
  评测运行由 `scripts/eval_ticket_summary.py` 生成。Langfuse 不可达、运行不存在或响应不合规一律视为未通过（fail closed）。

``CONSOLE_ENV=prod`` 时启动即校验：必须为 langfuse 模式，且配置了 EVAL_DATASETS 与 Langfuse 密钥（见 production_problems）。

闸门只在版本对外放量时生效：以 0% 灰度 + 白名单发布属于预发布（只有白名单里的评测账号可见），用于跑评测本身。
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

MODE = os.environ.get("CONSOLE_EVAL_GATE", "flag")
MIN_PASS_RATE = float(os.environ.get("EVAL_MIN_PASS_RATE", "0.8"))
MIN_ITEMS = int(os.environ.get("EVAL_MIN_ITEMS", "30"))
SCORE_NAME = "eval_pass"  # 评测脚本写在每条 Trace 上的通过评分（1 通过 / 0 不通过）
PAGE_LIMIT = 100  # 拉取运行项的分页大小
MAX_PAGES = 50  # 运行项分页上限（防止异常响应无限翻页）
FETCH_CONCURRENCY = 8  # 并发拉取 Trace 评分的上限
EVALUATION_TIMEOUT_S = 30.0
TRACE_MAX_AGE_S = float(os.environ.get("EVAL_TRACE_MAX_AGE_S", str(6 * 3600)))  # Trace 早于运行项创建时间的最大秒数
TRACE_SKEW_S = float(os.environ.get("EVAL_TRACE_SKEW_S", "300"))  # Trace 晚于运行项创建时间的容许时钟偏差（秒）
_transport: httpx.AsyncBaseTransport | None = None  # 测试注入


def _parse_datasets(raw: str) -> dict[str, list[str]]:
    """EVAL_DATASETS（JSON 对象：Agent → 数据集名单）；不合规时返回空（等同未配置，langfuse 模式下一律拒绝）。"""
    try:
        value = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    return {str(k): [str(x) for x in v] for k, v in value.items() if isinstance(v, list)}


DATASETS = _parse_datasets(os.environ.get("EVAL_DATASETS", ""))


def set_transport_for_tests(transport: httpx.AsyncBaseTransport | None) -> None:
    global _transport
    _transport = transport


def production_problems() -> list[str]:
    """CONSOLE_ENV=prod 的评测闸门基线：返回不满足的条目（空表示通过）。"""
    problems = configuration_problems()
    if MODE != "langfuse":
        problems.append("CONSOLE_EVAL_GATE 必须为 langfuse（不得信任请求体的 eval_passed）")
    if not DATASETS:
        problems.append("缺少 EVAL_DATASETS（按 Agent 配置的评测数据集名单，JSON 对象）")
    if not os.environ.get("LANGFUSE_PUBLIC_KEY") or not os.environ.get("LANGFUSE_SECRET_KEY"):
        problems.append("缺少 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY")
    return problems


def configuration_problems() -> list[str]:
    problems: list[str] = []
    if MODE not in {"flag", "langfuse"}:
        problems.append("CONSOLE_EVAL_GATE must be flag or langfuse")
    if isinstance(MIN_PASS_RATE, bool) or not math.isfinite(MIN_PASS_RATE) or not 0 <= MIN_PASS_RATE <= 1:
        problems.append("EVAL_MIN_PASS_RATE must be finite and within [0, 1]")
    if isinstance(MIN_ITEMS, bool) or not isinstance(MIN_ITEMS, int) or MIN_ITEMS < 1:
        problems.append("EVAL_MIN_ITEMS must be a positive integer")
    if not math.isfinite(TRACE_MAX_AGE_S) or TRACE_MAX_AGE_S <= 0:
        problems.append("EVAL_TRACE_MAX_AGE_S must be finite and positive")
    if not math.isfinite(TRACE_SKEW_S) or TRACE_SKEW_S < 0:
        problems.append("EVAL_TRACE_SKEW_S must be finite and nonnegative")
    return problems


def validate_for_production() -> None:
    """CONSOLE_ENV=prod 且评测闸门基线不满足时拒绝启动；其他环境不检查。"""
    invalid = configuration_problems()
    if invalid:
        raise RuntimeError("Console 评测闸门配置非法：" + "；".join(invalid))
    if os.environ.get("CONSOLE_ENV", "dev") != "prod":
        return
    problems = production_problems()
    if problems:
        raise RuntimeError("CONSOLE_ENV=prod 评测闸门基线未满足：" + "；".join(problems))


def allowed_datasets(agent_id: str) -> list[str]:
    """该 Agent 允许用于发布闸门的数据集名单（未单独配置时取 "*"）。"""
    return DATASETS.get(agent_id, DATASETS.get("*", []))


def policy_snapshot(agent_id: str) -> dict[str, Any]:
    return {
        "mode": MODE,
        "min_items": MIN_ITEMS,
        "min_pass_rate": MIN_PASS_RATE,
        "trace_max_age_s": TRACE_MAX_AGE_S,
        "trace_skew_s": TRACE_SKEW_S,
        "datasets": list(allowed_datasets(agent_id)),
        "scope": "console_process_environment",
    }


class _Unavailable(Exception):
    """Langfuse 不可达或响应不合规。"""


class _Rejected(Exception):
    """评测运行不可采信（如 Trace 在不同条目间复用）。"""


def _ts(value: Any) -> float | None:
    """ISO 8601 时间戳 → epoch 秒；缺失或不合规返回 None。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.timestamp() if stamp.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def _trace_meta(trace: dict[str, Any], key: str) -> Any:
    """Trace metadata 中的 Runtime 属性：OTLP 接入时位于 metadata.attributes["ai.<key>"]，也接受顶层 <key>。"""
    meta = trace.get("metadata")
    if not isinstance(meta, dict):
        return None
    attrs = meta.get("attributes")
    if isinstance(attrs, dict) and f"ai.{key}" in attrs:
        return attrs[f"ai.{key}"]
    return meta.get(key)


async def _get(c: httpx.AsyncClient, path: str, params: dict[str, Any] | None = None) -> Any:
    try:
        r = await c.get(path, params=params)
    except httpx.HTTPError as e:
        raise _Unavailable("Langfuse unavailable; cannot verify evaluation") from e
    if r.status_code != 200:
        raise _Unavailable(f"Langfuse returned {r.status_code} for {path.split('?')[0]}")
    try:
        return r.json()
    except ValueError as e:
        raise _Unavailable("Langfuse returned a malformed response") from e


async def _run_items(c: httpx.AsyncClient, run: dict[str, Any], run_name: str) -> list[dict[str, Any]]:
    """评测运行的全部运行项：运行详情带 datasetRunItems 时直接用，否则按数据集 id + 运行名分页拉取。"""
    items = run.get("datasetRunItems")
    if isinstance(items, list):
        if len(items) > PAGE_LIMIT * MAX_PAGES or any(not isinstance(item, dict) for item in items):
            raise _Unavailable("Langfuse returned malformed embedded run items")
        return items
    if items is not None:
        raise _Unavailable("Langfuse returned malformed embedded run items")
    if not isinstance(run.get("datasetId"), str) or not run["datasetId"]:
        raise _Unavailable("Langfuse paged evaluation has no datasetId")
    out: list[dict[str, Any]] = []
    declared_pages: int | None = None
    for page in range(1, MAX_PAGES + 1):
        params = {"datasetId": run.get("datasetId"), "runName": run_name, "page": page, "limit": PAGE_LIMIT}
        body = await _get(c, "/api/public/dataset-run-items", params)
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or len(data) > PAGE_LIMIT:
            raise _Unavailable("Langfuse returned a malformed run item page")
        if any(not isinstance(item, dict) for item in data):
            raise _Unavailable("Langfuse returned malformed run items")
        meta = body.get("meta")
        if meta is not None and not isinstance(meta, dict):
            raise _Unavailable("Langfuse returned malformed pagination metadata")
        total_pages = meta.get("totalPages") if isinstance(meta, dict) else None
        if total_pages is not None and (
            isinstance(total_pages, bool) or not isinstance(total_pages, int) or total_pages < page or total_pages > MAX_PAGES
        ):
            raise _Unavailable("Langfuse pagination is invalid or exceeds the verification limit")
        if declared_pages is not None and total_pages != declared_pages:
            raise _Unavailable("Langfuse pagination changed during verification")
        if total_pages is not None:
            declared_pages = total_pages
        if not data:
            if total_pages is not None and page < total_pages:
                raise _Unavailable("Langfuse pagination ended before all pages were verified")
            return out
        out += data
        if total_pages is not None and page == total_pages:
            return out
    raise _Unavailable("Langfuse pagination exceeds the verification limit; evaluation is incomplete")


async def _trace_passed(
    c: httpx.AsyncClient, sem: asyncio.Semaphore, trace_id: str, expect: dict[str, str], window: tuple[float, float] | None
) -> bool:
    """该 Trace 属于本次发布的 Agent / 版本 / 租户、时间戳在评测运行时间窗内，且 eval_pass 评分存在且全为 1。"""
    async with sem:
        trace = await _get(c, f"/api/public/traces/{quote(trace_id, safe='')}")
    if not isinstance(trace, dict):
        return False
    if any(str(_trace_meta(trace, k)) != v for k, v in expect.items()):
        return False
    ts = _ts(trace.get("timestamp"))
    if window is None or ts is None or not window[0] <= ts <= window[1]:
        return False
    raw_scores = trace.get("scores")
    if not isinstance(raw_scores, list):
        return False
    scores = [score for score in raw_scores if isinstance(score, dict) and score.get("name") == SCORE_NAME]
    if not scores:
        return False
    return all(
        isinstance(score.get("value"), (int, float)) and not isinstance(score["value"], bool) and score["value"] == 1 for score in scores
    )


async def recompute(c: httpx.AsyncClient, run: dict[str, Any], run_name: str, expect: dict[str, str] | None = None) -> tuple[int, int]:
    """按运行项与 eval_pass 评分重算，返回 (通过的条目数, 去重后的条目数)。expect 为 Trace metadata 须匹配的属性
    （agent_id / agent_version / tenant_id）。同一 traceId 出现在不同数据集条目下抛 _Rejected。"""
    items = await _run_items(c, run, run_name)
    by_item: dict[str, list[str | None]] = {}
    owner: dict[str, str] = {}  # traceId → 数据集条目
    created: dict[str, list[float | None]] = {}  # traceId → 引用它的运行项创建时间
    run_created = _ts(run.get("createdAt"))
    for it in items:
        key = it.get("datasetItemId")
        if not isinstance(key, str) or not key.strip():
            raise _Unavailable("Langfuse run item has no valid datasetItemId")
        tid = it.get("traceId") if isinstance(it.get("traceId"), str) and it.get("traceId") else None
        by_item.setdefault(key, []).append(tid)
        if tid is not None:
            if owner.setdefault(tid, key) != key:
                raise _Rejected(f"trace {tid} is reused across dataset items ({owner[tid]}, {key})")
            created.setdefault(tid, []).append(_ts(it.get("createdAt")) or run_created)
    sem = asyncio.Semaphore(FETCH_CONCURRENCY)
    traces = sorted(created)

    def window(tid: str) -> tuple[float, float] | None:
        stamps = created[tid]
        if not stamps or any(s is None for s in stamps):
            return None  # 无从核对评测时间窗：按不通过计
        known = [s for s in stamps if s is not None]
        return max(known) - TRACE_MAX_AGE_S, min(known) + TRACE_SKEW_S

    results = await asyncio.gather(*(_trace_passed(c, sem, t, expect or {}, window(t)) for t in traces))
    ok = dict(zip(traces, results, strict=True))
    passed = sum(1 for ts in by_item.values() if all(t is not None and ok.get(t, False) for t in ts))
    return passed, len(by_item)


async def _evaluate(
    agent_id: str, version: str, eval_run_ref: str | None, eval_passed: bool | None, tenant: str | None = None
) -> dict[str, Any]:
    """按发布闸门当前策略重算汇总；不返回 Trace 原文，也不写发布状态。"""

    def result(reason: str | None, status: str = "failed", passed: int | None = None, total: int | None = None) -> dict[str, Any]:
        return {
            "status": "passed" if reason is None else status,
            "reason": reason,
            "passed_items": passed,
            "total_items": total,
            "pass_rate": passed / total if passed is not None and total else None,
            "source": "langfuse_eval_pass" if MODE == "langfuse" else "development_flag",
        }

    invalid = configuration_problems()
    if invalid:
        return result("invalid evaluation gate configuration: " + "; ".join(invalid), "unavailable")
    if MODE == "flag":
        return result(None if eval_passed is True else "evaluation must pass (eval_passed=true with eval_run_ref)", "not_evaluated")
    if not eval_run_ref or not eval_run_ref.startswith("langfuse:") or "/" not in eval_run_ref:
        return result("eval_run_ref must reference a Langfuse dataset run (langfuse:<dataset>/<run>)", "not_evaluated")
    dataset, run_name = eval_run_ref.removeprefix("langfuse:").split("/", 1)
    if dataset not in allowed_datasets(agent_id):
        return result(f"dataset '{dataset}' is not an evaluation dataset configured for agent '{agent_id}' (EVAL_DATASETS)")
    host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000").rstrip("/")
    pk, sk = os.environ.get("LANGFUSE_PUBLIC_KEY", ""), os.environ.get("LANGFUSE_SECRET_KEY", "")
    auth = {"Authorization": "Basic " + base64.b64encode(f"{pk}:{sk}".encode()).decode()}
    async with httpx.AsyncClient(
        base_url=host, timeout=10, transport=_transport, headers=auth, trust_env=False, follow_redirects=False
    ) as c:
        try:
            run = await _get(c, f"/api/public/datasets/{quote(dataset, safe='')}/runs/{quote(run_name, safe='')}")
            if not isinstance(run, dict):
                return result("Langfuse returned a malformed evaluation run", "unavailable")
            meta = run.get("metadata") or {}
            if not isinstance(meta, dict) or meta.get("agent_id") != agent_id or str(meta.get("agent_version")) != version:
                got = f"{meta.get('agent_id')}@{meta.get('agent_version')}" if isinstance(meta, dict) else "?"
                return result(f"evaluation run is for {got}, not {agent_id}@{version}")
            if tenant and meta.get("tenant_id", meta.get("tenant", tenant)) != tenant:
                return result("evaluation run tenant mismatch")
            expect = {"agent_id": agent_id, "agent_version": version} | ({"tenant_id": tenant} if tenant else {})
            passed, total = await recompute(c, run, run_name, expect)
        except _Rejected:
            return result("evaluation run rejected: trace reused across dataset items")
        except _Unavailable as e:
            reason = str(e) if "unavailable" in str(e) else f"evaluation run not verifiable: {e}"
            return result(reason, "unavailable")
    if total < MIN_ITEMS:
        return result(f"evaluation run covers {total} dataset items, fewer than EVAL_MIN_ITEMS={MIN_ITEMS}", passed=passed, total=total)
    rate = passed / total
    if not math.isfinite(rate) or rate < MIN_PASS_RATE:
        reason = f"evaluation pass rate {rate:.0%} ({passed}/{total}, recomputed from {SCORE_NAME} scores) is below {MIN_PASS_RATE:.0%}"
        return result(reason, passed=passed, total=total)
    return result(None, passed=passed, total=total)


async def evaluate(
    agent_id: str, version: str, eval_run_ref: str | None, eval_passed: bool | None, tenant: str | None = None
) -> dict[str, Any]:
    """限定完整重算耗时，超时不返回部分统计。"""
    try:
        async with asyncio.timeout(EVALUATION_TIMEOUT_S):
            return await _evaluate(agent_id, version, eval_run_ref, eval_passed, tenant)
    except TimeoutError:
        return {
            "status": "unavailable",
            "reason": "evaluation verification deadline exceeded",
            "passed_items": None,
            "total_items": None,
            "pass_rate": None,
            "source": "langfuse_eval_pass" if MODE == "langfuse" else "development_flag",
        }


async def check(agent_id: str, version: str, eval_run_ref: str | None, eval_passed: bool | None, tenant: str | None = None) -> str | None:
    """发布路径复用同一重算结果；None 通过，否则为失败关闭原因。"""
    result = await evaluate(agent_id, version, eval_run_ref, eval_passed, tenant)
    return None if result["reason"] is None else str(result["reason"])

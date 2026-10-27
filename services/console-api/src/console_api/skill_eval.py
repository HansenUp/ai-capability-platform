"""Skill 评测回放编排（S9.4，D142；方案 9.4）。

流程（``run(name, version, tenant=...)``）：
1. 经能力网关 ``GET /skills/{n}/versions/{v}`` 取版本信息：状态须为 ``scanned``、租户须与操作者一致；取清单声明的能力。
2. 取包内 ``SKILL.md`` 与 ``evals/cases.jsonl``（可选 ``evals/eval.json`` 的 ``min_pass_rate`` 为包内声明阈值）：版本信息带
   ``skill_md`` / ``eval_cases`` 字段时直接用，否则经 ``GET /skills/{n}/versions/{v}/package`` 下载 tgz 读取（未上架版本
   能否下载由网关决定，联调确认）。用例 ≥ ``SKILL_EVAL_MIN_ITEMS``（默认 10）且数据分级只能是 public。
3. 以 0% 灰度 + 评测白名单发布预发布临时 Agent ``__skilleval-<n>``（版本 ``<v>-e<时间戳>``；工具 = 包声明能力，
   ``cap.`` 前缀按目录约定去掉得到工具名；SKILL.md 以 ``<skill>`` 块内联进系统提示词）。
4. 逐条以评测主体（``SKILL_EVAL_USER``，默认 skilleval，须在白名单内）调 Runtime ``POST /v1/messages``，按用例断言：
   ``must_call``（须调用的工具）/ ``must_not_call``（不得调用的工具）/ ``contains``（回答须包含，``a|b`` 任一即可）。
5. 写 Langfuse：数据集 ``skill-<n>``、条目、每条 Trace 的 ``eval_pass`` 评分与数据集运行项（运行 metadata 记被测 Agent 版本），
   再复用 ``eval_gate.recompute`` 按评分重算（不采信本地结果）。未配置 Langfuse 密钥时（开发态）以本地断言结果计、
   ``run_ref`` 为 ``local:<数据集>/<运行名>``。
6. 阈值 = max(包内声明, ``SKILL_EVAL_MIN_PASS_RATE``=0.8)；结果经网关 ``POST /skills/{n}/versions/{v}/eval-result``
   ``{run_ref, pass_rate, items}`` 回写（网关复核后置 evaluated / eval_failed）。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import tarfile
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from console_api import eval_gate, gateway_client, runtime_client

SCORE_NAME = eval_gate.SCORE_NAME
_langfuse_transport: httpx.AsyncBaseTransport | None = None


def set_langfuse_transport_for_tests(transport: httpx.AsyncBaseTransport | None) -> None:
    global _langfuse_transport
    _langfuse_transport = transport


def min_items() -> int:
    return int(os.environ.get("SKILL_EVAL_MIN_ITEMS", "10"))


def min_pass_rate() -> float:
    return float(os.environ.get("SKILL_EVAL_MIN_PASS_RATE", "0.8"))


def ask_timeout_s() -> float:
    """单条评测对话（含模型与工具调用）的超时；管理接口默认 10 秒不够一次真实模型对话。"""
    return float(os.environ.get("SKILL_EVAL_TIMEOUT_S", "180"))


class SkillEvalError(Exception):
    """评测无法进行（status 为建议的 HTTP 状态码）。"""

    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Case:
    id: str
    input: str
    must_call: list[str] = field(default_factory=list)
    must_not_call: list[str] = field(default_factory=list)
    contains: list[str] = field(default_factory=list)


def parse_cases(raw: list[Any]) -> list[Case]:
    out: list[Case] = []
    seen: set[str] = set()
    for i, c in enumerate(raw):
        if not isinstance(c, dict) or not isinstance(c.get("input"), str) or not c["input"].strip():
            raise SkillEvalError(f"eval case #{i + 1} malformed (needs id and input)", 422)
        cid = str(c.get("id") or f"case-{i + 1}")
        if cid in seen:
            raise SkillEvalError(f"duplicate eval case id {cid!r}", 422)
        seen.add(cid)
        if c.get("data_class", "public") != "public":
            raise SkillEvalError(f"eval case {cid!r} is not public data (data_class={c.get('data_class')!r})")
        lists = {k: c.get(k) or [] for k in ("must_call", "must_not_call", "contains")}
        if not all(isinstance(v, list) and all(isinstance(x, str) for x in v) for v in lists.values()):
            raise SkillEvalError(f"eval case {cid!r}: must_call / must_not_call / contains must be string lists", 422)
        out.append(Case(cid, c["input"], lists["must_call"], lists["must_not_call"], lists["contains"]))
    return out


def judge(case: Case, answer: str, called: list[str]) -> list[str]:
    """断言：返回未满足的条件（空表示通过）。"""
    reasons = [f"未调用：{t}" for t in case.must_call if t not in called]
    reasons += [f"不应调用：{t}" for t in case.must_not_call if t in called]
    reasons += [f"缺少：{m}" for m in case.contains if not any(alt in answer for alt in m.split("|"))]
    return reasons


def tool_names(capabilities: list[str]) -> list[str]:
    """包声明能力 → Agent 工具名（能力目录约定 capability_id = ``cap.<工具名>``）。"""
    return sorted({c.removeprefix("cap.") for c in capabilities if isinstance(c, str) and c})


def _read_package(data: bytes) -> dict[str, bytes]:
    wanted = {"SKILL.md", "evals/cases.jsonl", "evals/eval.json"}
    out: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for m in tar:
                name = m.name.removeprefix("./")
                if name in wanted and m.isreg() and m.size <= 4 * 1024 * 1024:
                    fh = tar.extractfile(m)
                    if fh is not None:
                        out[name] = fh.read()
    except (tarfile.TarError, OSError, EOFError) as e:
        raise SkillEvalError(f"skill package unreadable: {type(e).__name__}", 502) from e
    return out


async def _gateway_json(method: str, path: str, **kw: Any) -> tuple[int, Any]:
    try:
        r = await gateway_client.request(method, path, **kw)
    except gateway_client.GatewayError as e:
        raise SkillEvalError(str(e), 502) from e
    try:
        return r.status_code, (r.json() if r.content else None)
    except ValueError:
        return r.status_code, None


async def _materials(name: str, version: str, info: dict[str, Any]) -> tuple[str, list[Any], float]:
    """(SKILL.md 文本, 原始用例列表, 包内声明阈值)。"""
    skill_md, raw_cases, declared = info.get("skill_md"), info.get("eval_cases"), 0.0
    if not isinstance(skill_md, str) or not isinstance(raw_cases, list):
        try:
            r = await gateway_client.request("GET", f"/skills/{quote(name)}/versions/{quote(version)}/package")
        except gateway_client.GatewayError as e:
            raise SkillEvalError(str(e), 502) from e
        if r.status_code != 200:
            raise SkillEvalError(f"skill package unavailable for evaluation (gateway HTTP {r.status_code})", 502)
        files = _read_package(r.content)
        if "SKILL.md" not in files or "evals/cases.jsonl" not in files:
            raise SkillEvalError("package lacks SKILL.md or evals/cases.jsonl")
        skill_md = files["SKILL.md"].decode("utf-8", errors="replace")
        try:
            raw_cases = [json.loads(line) for line in files["evals/cases.jsonl"].decode("utf-8").splitlines() if line.strip()]
            if "evals/eval.json" in files:
                declared = float(json.loads(files["evals/eval.json"]).get("min_pass_rate", 0.0))
        except (ValueError, UnicodeDecodeError, AttributeError, TypeError) as e:
            raise SkillEvalError(f"evals files malformed: {type(e).__name__}", 422) from e
    elif isinstance(info.get("eval_min_pass_rate"), int | float):
        declared = float(info["eval_min_pass_rate"])
    return skill_md, raw_cases, declared


def _langfuse_configured() -> bool:
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"))


def _langfuse_client() -> httpx.AsyncClient:
    host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000").rstrip("/")
    pk, sk = os.environ.get("LANGFUSE_PUBLIC_KEY", ""), os.environ.get("LANGFUSE_SECRET_KEY", "")
    auth = {"Authorization": "Basic " + base64.b64encode(f"{pk}:{sk}".encode()).decode()}
    return httpx.AsyncClient(base_url=host, timeout=30, transport=_langfuse_transport, headers=auth)


async def _lf_post(c: httpx.AsyncClient, path: str, body: dict[str, Any]) -> None:
    try:
        r = await c.post(path, json=body)
    except httpx.HTTPError as exc:
        raise SkillEvalError("Langfuse unavailable while recording evaluation", 502) from exc
    if r.status_code >= 300:
        raise SkillEvalError(f"Langfuse {path} returned HTTP {r.status_code}", 502)


async def _verified_run(
    client: httpx.AsyncClient,
    dataset: str,
    run_name: str,
    expect: dict[str, str],
    expected_items: dict[str, str],
) -> tuple[int, int]:
    deadline = time.monotonic() + max(0, float(os.environ.get("SKILL_EVAL_INGEST_TIMEOUT_S", "60")))
    path = f"/api/public/datasets/{quote(dataset, safe='')}/runs/{quote(run_name, safe='')}"
    while True:
        try:
            run_obj = await eval_gate._get(client, path)
            if not isinstance(run_obj, dict):
                raise eval_gate._Unavailable("malformed evaluation run")
            metadata = run_obj.get("metadata")
            if not isinstance(metadata, dict) or any(str(metadata.get(key)) != value for key, value in expect.items()):
                raise eval_gate._Rejected("evaluation run identity does not match the evaluated agent, version and tenant")
            items = await eval_gate._run_items(client, run_obj, run_name)
            if len(items) != len(expected_items):
                raise eval_gate._Unavailable("evaluation run items not fully ingested")
            observed = {item.get("datasetItemId"): item.get("traceId") for item in items}
            if observed != expected_items:
                raise eval_gate._Rejected("evaluation run items do not match the recorded case traces")
            for trace_id in expected_items.values():
                trace = await eval_gate._get(client, f"/api/public/traces/{quote(trace_id, safe='')}")
                if not isinstance(trace, dict) or not any(
                    isinstance(score, dict) and score.get("name") == SCORE_NAME for score in (trace.get("scores") or [])
                ):
                    raise eval_gate._Unavailable("evaluation trace scores not fully ingested")
                if any(str(eval_gate._trace_meta(trace, key)) != value for key, value in expect.items()):
                    raise eval_gate._Rejected("evaluation trace identity does not match the evaluated agent, version and tenant")
            return await eval_gate.recompute(client, run_obj, run_name, expect)
        except eval_gate._Rejected as exc:
            raise SkillEvalError(f"evaluation run not verifiable: {exc}", 502) from exc
        except eval_gate._Unavailable as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SkillEvalError(f"evaluation ingestion timed out: {exc}", 502) from exc
            await asyncio.sleep(min(1, remaining))


async def _ask(agent_id: str, version: str, case: Case, tenant: str) -> dict[str, Any]:
    body = {"agent_id": agent_id, "agent_version": version, "messages": [{"role": "user", "content": case.input}], "stream": False}
    user = os.environ.get("SKILL_EVAL_USER", "skilleval")
    try:
        data = await runtime_client.call("POST", "/v1/messages", user=user, tenant=tenant, json=body, timeout_s=ask_timeout_s())
    except runtime_client.RuntimeSyncError as e:
        return {"answer": "", "called": [], "run_id": None, "trace_id": None, "error": str(e)}
    events = [e for e in data.get("events", []) if isinstance(e, dict)]
    called = [str(e.get("tool_name")) for e in events if e.get("type") == "tool_use"]
    done = next((e for e in reversed(events) if e.get("type") == "done"), {})
    result = {"answer": str(data.get("content") or ""), "called": called, "run_id": data.get("run_id"), "trace_id": done.get("trace_id")}
    if any(event.get("type") == "error" for event in events) or done.get("stop_reason") not in ("end_turn", "max_tokens"):
        result["error"] = "Runtime evaluation did not complete successfully"
    return result


async def run(name: str, version: str, *, tenant: str, cases: list[Any] | None = None) -> dict[str, Any]:
    """执行一次评测回放并回写网关，返回汇总（见模块说明）。"""
    if os.environ.get("CONSOLE_ENV", "dev") == "prod" and not _langfuse_configured():
        raise SkillEvalError("production Skill evaluation requires Langfuse", 502)
    status, info = await _gateway_json("GET", f"/skills/{quote(name)}/versions/{quote(version)}")
    if status == 404 or not isinstance(info, dict):
        raise SkillEvalError(f"skill {name}@{version} not found", 404)
    if status != 200:
        raise SkillEvalError(f"gateway returned HTTP {status}", 502)
    if info.get("tenant") not in (None, tenant):
        raise SkillEvalError(f"skill {name}@{version} not found", 404)
    if info.get("status") != "scanned":
        raise SkillEvalError(f"skill version status is {info.get('status')!r}; evaluation requires 'scanned'")
    raw_manifest = info.get("manifest")
    manifest: dict[str, Any] = raw_manifest if isinstance(raw_manifest, dict) else {}
    skill_md, raw_cases, declared = await _materials(name, version, info)
    supplied = parse_cases(cases) if cases is not None else None
    parsed = parse_cases(raw_cases)
    if supplied is not None and supplied != parsed:
        raise SkillEvalError("evaluation cases must match the immutable package cases")
    if len(parsed) < min_items():
        raise SkillEvalError(f"{len(parsed)} eval cases, fewer than SKILL_EVAL_MIN_ITEMS={min_items()}")
    threshold = max(declared, min_pass_rate())

    # 预发布临时 Agent：0% 灰度 + 评测白名单
    agent_id = f"__skilleval-{name}"[:64]
    run_token = uuid.uuid4().hex
    agent_version = f"{version[:10]}-e{run_token[:20]}"
    user = os.environ.get("SKILL_EVAL_USER", "skilleval")
    system_prompt = f'<skill name="{name}" version="{version}">\n{skill_md.strip()}\n</skill>'
    snapshot = {
        "agent_id": agent_id,
        "version": agent_version,
        "tenant_id": tenant,
        "system_prompt": system_prompt,
        "model_alias": os.environ.get("SKILL_EVAL_MODEL_ALIAS", "default-chat"),
        "harness": os.environ.get("SKILL_EVAL_HARNESS") or None,
        "config": {"tools": tool_names(list(manifest.get("capabilities") or [])), "data_class": manifest.get("data_class", "internal")},
    }
    # 白名单须是 Runtime 实际看到的主体（令牌模式为 app:<console 服务客户端>），否则 0% 版本对评测请求不可见
    publish = {"eval_passed": True, "rollout_percent": 0, "rollout_allowlist": [runtime_client.subject(user)]}
    try:
        await runtime_client.call("POST", "/admin/agents", user=user, tenant=tenant, json=snapshot)
        await runtime_client.call(
            "POST",
            f"/admin/agents/{agent_id}/versions/{agent_version}/publish",
            user=user,
            tenant=tenant,
            json=publish,
            params={"tenant_id": tenant},
        )
    except runtime_client.RuntimeSyncError as e:
        raise SkillEvalError(f"cannot publish evaluation agent: {e}", 502) from e

    results: list[dict[str, Any]] = []
    for case in parsed:  # 串行回放（本机资源紧；评测量级 ≤ 数十条）
        got = await _ask(agent_id, agent_version, case, tenant)
        reasons = [f"运行失败：{got['error']}"] if got.get("error") else judge(case, got["answer"], got["called"])
        results.append({"id": case.id, "passed": not reasons, "reasons": reasons} | {k: got[k] for k in ("run_id", "trace_id", "called")})

    dataset = f"{os.environ.get('SKILL_EVAL_DATASET_PREFIX', 'skill-')}{name}"
    run_name = f"{name}@{version}-{run_token}"
    local_passed = sum(r["passed"] for r in results)
    meta = {
        "agent_id": agent_id,
        "agent_version": agent_version,
        "skill": name,
        "skill_version": version,
        "tenant_id": tenant,
        "local_passed": local_passed,
        "total": len(results),
    }
    if _langfuse_configured():
        trace_ids = [result["trace_id"] for result in results]
        if any(not isinstance(trace_id, str) or not trace_id for trace_id in trace_ids) or len(set(trace_ids)) != len(trace_ids):
            raise SkillEvalError("evaluation requires one unique Runtime trace per case", 502)
        async with _langfuse_client() as c:
            await _lf_post(c, "/api/public/datasets", {"name": dataset, "description": f"Skill {name} 评测集（S9.4）"})
            expected_items = {}
            for case, r in zip(parsed, results, strict=True):
                item_id = f"{run_token}:{case.id}"
                expected_items[item_id] = r["trace_id"]
                await _lf_post(c, "/api/public/dataset-items", {"datasetName": dataset, "id": item_id, "input": {"question": case.input}})
                if r["trace_id"]:
                    score = {"traceId": r["trace_id"], "name": SCORE_NAME, "value": 1 if r["passed"] else 0, "dataType": "NUMERIC"}
                    await _lf_post(c, "/api/public/scores", score | {"comment": "；".join(r["reasons"])})
                desc = f"Skill {name}@{version} 评测回放"
                body = {"runName": run_name, "runDescription": desc, "metadata": meta, "datasetItemId": item_id}
                await _lf_post(c, "/api/public/dataset-run-items", body | ({"traceId": r["trace_id"]} if r["trace_id"] else {}))
            expect = {"agent_id": agent_id, "agent_version": agent_version, "tenant_id": tenant}
            passed, total = await _verified_run(c, dataset, run_name, expect, expected_items)
        run_ref = f"langfuse:{dataset}/{run_name}"
    else:
        passed, total = local_passed, len(results)
        run_ref = f"local:{dataset}/{run_name}"
    exact_rate = passed / total if total else 0.0
    pass_rate = round(exact_rate, 4)
    gate_passed = total >= min_items() and exact_rate >= threshold
    items = [{"id": r["id"], "passed": r["passed"], "trace_id": r["trace_id"], "reasons": r["reasons"]} for r in results]
    # 回写未取整的通过率：网关按 items 重算并以 1e-6 容差核对（如 11/12 取整到 0.9167 会被判为不一致）
    result_body: dict[str, Any] = {"run_ref": run_ref, "pass_rate": exact_rate, "items": items, "tenant": tenant}
    status, resp = await _gateway_json("POST", f"/skills/{quote(name)}/versions/{quote(version)}/eval-result", json=result_body)
    if status >= 300:
        raise SkillEvalError(f"gateway rejected eval result: HTTP {status}", 502)
    return {
        "skill": name,
        "version": version,
        "run_ref": run_ref,
        "pass_rate": pass_rate,
        "passed": passed,
        "total": total,
        "threshold": threshold,
        "gate_passed": gate_passed,
        "eval_agent": {"agent_id": agent_id, "version": agent_version},
        "gateway": {"status": status, "body": resp},
        "results": results,
    }

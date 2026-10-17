#!/usr/bin/env python3
"""工单总结 Agent 评测（S4.6）：把 `evals/datasets/ticket-summary.jsonl`（30 条）同步为 Langfuse 数据集 `ticket-summary`，
逐条以数据集指定的开发用户身份（alice / bob，经 Keycloak 登录）调用 Runtime，按规则打分，结果写回 Langfuse：
每条一个数据集运行项（关联该次对话的 Trace）与一个 `eval_pass` 评分；运行的 metadata 记录被评测的 Agent 版本与通过率。

版本固定与核对：每条请求都显式指定 agent_version（不走灰度选版），并按返回的 run_id 查询 Runtime 的 Run 记录，
核对实际执行的版本；不一致（或无法核对）的条目按不通过计，条数记入 metadata 的 version_mismatch，
避免把稳定版的成绩记到新版本上、让 Console 闸门放行未评测的版本。

评分规则：`must_include` 中每一项都要出现在回答里（`a|b` 表示任一即可），`must_not_include` 中任一项都不得出现
（越权场景下不得泄露他人工单内容）。

输出的 eval_run_ref 形如 `langfuse:ticket-summary/<运行名>`，Console 发布 / 放量时据此到 Langfuse 读取通过率（评测闸门）。

前置：被评测的版本已在 Runtime 发布并对 alice、bob 可用（预发布做法：灰度 0% + 白名单这两个用户，见 S3 灰度规则）。
用法：
    .venv/bin/python scripts/eval_ticket_summary.py --runtime http://localhost:8080 --agent ticket-summary --version 1
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit  # noqa: E402

DATASET = "ticket-summary"
DATA_FILE = Path(__file__).resolve().parent.parent / "evals" / "datasets" / "ticket-summary.jsonl"


def load_items() -> list[dict[str, Any]]:
    return [json.loads(line) for line in DATA_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]


def check(answer: str, item: dict[str, Any]) -> tuple[bool, list[str]]:
    """规则打分：返回（是否通过，未满足的条件）。"""
    missing = [m for m in item["must_include"] if not any(alt in answer for alt in m.split("|"))]
    leaked = [m for m in item["must_not_include"] if m in answer]
    return not missing and not leaked, [f"缺少：{m}" for m in missing] + [f"不应出现：{m}" for m in leaked]


def sync_dataset(items: list[dict[str, Any]]) -> None:
    """创建数据集并按 id 幂等写入条目。"""
    devkit.langfuse("POST", "/api/public/datasets", json={"name": DATASET, "description": "工单总结 Agent 评测集（S4.6）"})
    for it in items:
        body = {
            "datasetName": DATASET,
            "id": it["id"],
            "input": {"user": it["user"], "question": it["input"]},
            "expectedOutput": {"must_include": it["must_include"], "must_not_include": it["must_not_include"]},
        }
        r = devkit.langfuse("POST", "/api/public/dataset-items", json=body)
        r.raise_for_status()


def ask(runtime: str, token: str, agent: str, version: str, question: str) -> tuple[str, str | None, str | None]:
    """以用户令牌对指定版本对话一次（评测账号须在该版本的灰度白名单内），返回（回答文本，run_id，trace_id）。"""
    body = {"agent_id": agent, "agent_version": version, "messages": [{"role": "user", "content": question}], "stream": False}
    r = httpx.post(f"{runtime}/v1/messages", json=body, headers={"Authorization": f"Bearer {token}"}, timeout=300)
    if r.status_code != 200:
        return f"[HTTP {r.status_code}] {r.text[:200]}", None, None
    data = r.json()
    done = next((e for e in reversed(data.get("events", [])) if e.get("type") == "done"), {})
    return data.get("content", ""), data.get("run_id"), done.get("trace_id")


def run_version(runtime: str, token: str, run_id: str) -> str | None:
    """按 run_id 查询 Runtime 的 Run 记录，返回实际执行的 Agent 版本；查询失败返回 None。"""
    try:
        r = httpx.get(f"{runtime}/v1/runs/{run_id}", headers={"Authorization": f"Bearer {token}"}, timeout=30)
    except httpx.HTTPError:
        return None  # 网络异常只让该条按"无法核对"计，不中断整批
    return str(r.json().get("agent_version")) if r.status_code == 200 else None


def run_eval(runtime: str, agent: str, version: str, concurrency: int = 5, min_pass_rate: float = 0.8) -> dict[str, Any]:
    """执行评测并写回 Langfuse，返回汇总（含 eval_run_ref）。"""
    items = load_items()
    sync_dataset(items)
    tokens = {u: devkit.user_token(u) for u in sorted({it["user"] for it in items})}

    def one(it: dict[str, Any]) -> dict[str, Any]:
        token = tokens[it["user"]]
        answer, run_id, trace_id = ask(runtime, token, agent, version, it["input"])
        ok, reasons = check(answer, it)
        actual = run_version(runtime, token, run_id) if run_id else None
        mismatch = actual != version  # 未取得 run_id 或查询失败同样视为无法核对，按不通过计
        if mismatch:
            ok = False
            reasons.append(f"版本不符：期望 {version}，实际 {actual or '未知'}")
        return {
            "item": it,
            "answer": answer,
            "run_id": run_id,
            "trace_id": trace_id,
            "actual_version": actual,
            "version_mismatch": mismatch,
            "passed": ok,
            "reasons": reasons,
        }

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(one, items))
    passed = sum(r["passed"] for r in results)
    mismatched = sum(r["version_mismatch"] for r in results)
    run_name = f"{agent}@{version}-{time.strftime('%Y%m%d-%H%M%S')}"
    meta = {
        "agent_id": agent,
        "agent_version": version,
        "passed": passed,
        "total": len(results),
        "pass_rate": round(passed / len(results), 4),
        "version_checked": len(results) - mismatched,  # 经 Run 记录核对、确为被测版本的条数
        "version_mismatch": mismatched,  # 实际版本不符或无法核对的条数（均已按不通过计）
    }
    for r in results:
        if r["trace_id"]:
            devkit.langfuse(
                "POST",
                "/api/public/scores",
                json={
                    "traceId": r["trace_id"],
                    "name": "eval_pass",
                    "value": 1 if r["passed"] else 0,
                    "dataType": "NUMERIC",
                    "comment": "；".join(r["reasons"]),
                },
            )
        body = {"runName": run_name, "runDescription": f"{agent}@{version} 规则评测", "metadata": meta, "datasetItemId": r["item"]["id"]}
        if r["trace_id"]:
            body["traceId"] = r["trace_id"]
        devkit.langfuse("POST", "/api/public/dataset-run-items", json=body).raise_for_status()
    return meta | {
        "run_name": run_name,
        "eval_run_ref": f"langfuse:{DATASET}/{run_name}",
        "results": results,
        "gate_passed": meta["pass_rate"] >= min_pass_rate and mismatched == 0,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--runtime", default="http://localhost:8080")
    p.add_argument("--agent", default="ticket-summary")
    p.add_argument("--version", default="1")
    p.add_argument("--concurrency", type=int, default=5)
    args = p.parse_args()
    summary = run_eval(args.runtime, args.agent, args.version, args.concurrency)
    for r in summary["results"]:
        if not r["passed"]:
            print(
                f"未通过 {r['item']['id']}（{r['item']['user']}）{r['item']['input']}：{'；'.join(r['reasons'])}"
                f"\n    回答：{r['answer'][:160]!r}"
            )
    if summary["version_mismatch"]:
        print(f"版本不符 / 无法核对 {summary['version_mismatch']} 条（期望 {args.agent}@{args.version}），已按不通过计")
    print(f"通过 {summary['passed']}/{summary['total']}（{summary['pass_rate']:.0%}），eval_run_ref={summary['eval_run_ref']}")
    return 0 if summary["gate_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

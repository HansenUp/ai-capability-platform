#!/usr/bin/env python3
"""本机演示数据：经 Console API 创建并发布几个 Agent（Console 再同步到 Runtime）。可重复执行（已发布的版本会跳过）。

- portal-demo@1：门户对话演示（litellm 真实模型，带 echo 工具），租户 default；
- gray-demo@1（全量）与 gray-demo@2（灰度 10%，白名单为开发用户 bob）：灰度与回滚演示，回复开头分别带【v1】【v2】；
- t2-demo@1：租户 t2 的 Agent，用于验证租户隔离（default 租户的用户看不到）。

前置：compose（keycloak、litellm、mysql、vault）与 Runtime、Console API 已启动（见本机检查清单）。
用法：.venv/bin/python scripts/dev_seed.py
"""

from __future__ import annotations

import os
import sys

import httpx

CONSOLE = os.environ.get("CONSOLE_URL", "http://localhost:8081")
MODEL = os.environ.get("SEED_MODEL_ALIAS", "deepseek-v4-pro-2026-08")
BOB = "human:00000000-0000-4000-8000-000000000b0b"  # 本机 realm 中开发用户 bob 的固定 id

TICKET_PROMPT = (
    "你是售后工单总结助手，只能读取工单，不能执行任何写操作（关闭、重开、通知客户等），用户要求写操作时明确说明无法执行。"
    "回答前先用 ticket.search 定位工单、用 ticket.get 获取详情，只依据工具返回的内容作答，不编造。"
    "工具返回未找到或无权限时，直接告诉用户无法访问该工单。"
    "用户要求总结某个工单时，按以下五个小标题输出：问题、根因、处理过程、客户反馈、可复用经验。"
)

AGENTS = [
    ("default", "ticket-summary", "1", TICKET_PROMPT, ["ticket.search", "ticket.get"], 100, []),
    ("default", "portal-demo", "1", "你是门户助手，回答简短。用户要求时调用 echo 工具。", ["echo"], 100, []),
    ("default", "gray-demo", "1", "你是灰度演示助手。每次回答必须以【v1】开头，回答一句话。", [], 100, []),
    ("default", "gray-demo", "2", "你是灰度演示助手。每次回答必须以【v2】开头，回答一句话。", [], 10, [BOB]),
    ("t2", "t2-demo", "1", "你是 t2 租户的助手，回答简短。", [], 100, []),
]


def main() -> int:
    ok = True
    for tenant, agent_id, version, prompt, tools, canary, allowlist in AGENTS:
        h = {"X-Console-User": "seed", "X-Console-Role": "TENANT_ADMIN", "X-Console-Tenant": tenant}
        draft = {"agent_id": agent_id, "version": version, "system_prompt": prompt, "model_alias": MODEL, "harness": "litellm"}
        r = httpx.post(f"{CONSOLE}/agents", json=draft | {"config": {"tools": tools}}, headers=h, timeout=30)
        if r.status_code == 409:
            print(f"跳过 {tenant}/{agent_id}@{version}（已发布）")
            continue
        r.raise_for_status()
        body = {"eval_passed": True, "eval_run_ref": "seed", "canary_percent": canary, "canary_allowlist": allowlist}
        r = httpx.post(f"{CONSOLE}/agents/{agent_id}/versions/{version}/publish", json=body, headers=h, timeout=30)
        detail = "" if r.status_code == 200 else f" {r.text}"
        print(f"发布 {tenant}/{agent_id}@{version}（灰度 {canary}%）→ HTTP {r.status_code}{detail}")
        ok &= r.status_code == 200
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

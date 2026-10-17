#!/usr/bin/env python3
"""S4 本机出口验证（M1 垂直切片：工单总结 Agent）。

前置：compose 已启动 keycloak（26.3，标准 Token Exchange）、litellm、observe profile（Langfuse）。
本脚本以子进程启动整条链路：mcp-ticket(18091) ← 能力网关(18082，换取 OBO 令牌) ← Runtime(18090，强制鉴权、
工具经网关分发、litellm 真实模型) ← Console(18081，评测闸门 langfuse 模式)。然后：

1. Console 以 0% 灰度 + 白名单（alice、bob）预发布 ticket-summary@1；未附评测直接全量发布被闸门拒绝；
2. 30 条评测集逐条以 alice / bob 身份对话、规则打分、写回 Langfuse（数据集运行 + eval_pass 评分）；
3. 以评测运行引用放量到 100%，闸门到 Langfuse 读取通过率后放行；
4. 四类安全测试中的端到端部分：跨用户访问（行级权限经 OBO 令牌落到 MCP Server）、非白名单工具、附件越权，
   以及跨租户不可见；机密数据外发 / 检索 / 模型阻断见 services/runtime/tests/test_s4_security.py；
5. 审计与 Trace：工具判定入审计，Langfuse 中可见 execute_tool 与 eval_pass 评分。

用法：.venv/bin/python scripts/verify_s4_e2e.py     退出码 0 表示全部通过。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit  # noqa: E402
import eval_ticket_summary  # noqa: E402
from verify_s3_e2e import check, failures, service  # noqa: E402

ALIAS = os.environ.get("S4_MODEL_ALIAS", "deepseek-v4-pro-2026-08")
TMP = Path(tempfile.mkdtemp(prefix="s4-e2e-"))
TADMIN = {"X-Console-User": "ops", "X-Console-Role": "TENANT_ADMIN"}
SYSTEM = (
    "你是售后工单总结助手，只能读取工单，不能执行任何写操作（关闭、重开、通知客户等），用户要求写操作时明确说明无法执行。"
    "回答前先用 ticket.search 定位工单、用 ticket.get 获取详情，只依据工具返回的内容作答，不编造。"
    "工具返回未找到或无权限时，直接告诉用户无法访问该工单，不要猜测内容。"
    "用户要求总结某个工单时，按以下五个小标题输出：问题、根因、处理过程、客户反馈、可复用经验。其他问题简短直接回答，并注明工单号。"
)


def env_stack() -> dict[str, dict[str, str]]:
    certs = f"{devkit.REALM}/protocol/openid-connect/certs"
    return {
        "mcp": {
            "MCP_TICKET_JWKS_URL": certs,
            "MCP_TICKET_ISSUER": devkit.REALM,
            "MCP_TICKET_AUDIENCE": "mcp-ticket",
            "MCP_TICKET_DB": str(TMP / "mcp-ticket.db"),
        },
        "gateway": {
            "KEYCLOAK_TOKEN_URL": devkit.TOKEN_URL,
            "GATEWAY_CLIENT_SECRET": "capability-gateway-dev-secret",
            "MCP_TICKET_URL": "http://127.0.0.1:18091",
            "POLICY_URL": "http://127.0.0.1:1",
        },
        "runtime": {
            "DATABASE_URL": f"sqlite+aiosqlite:///{TMP}/runtime.db",
            "WORKSPACE_DIR": str(TMP / "ws"),
            "HARNESS": "litellm",
            "POLICY_URL": "http://127.0.0.1:1",
            "CAPABILITY_GATEWAY_URL": "http://127.0.0.1:18082",
            "TOOL_DISPATCH": "gateway",
            "TOOL_RESULT_OFFLOAD_TOKENS": "300",
            "AUTH_REQUIRED": "true",
            "JWKS_URL": certs,
            "JWT_ISSUER": devkit.REALM,
            "JWT_AUDIENCE": "ai-runtime",
            "OTEL_EXPORTER_OTLP_ENDPOINT": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"),
        },
        "console": {
            "CONSOLE_DATABASE_URL": f"sqlite+aiosqlite:///{TMP}/console.db",
            "RUNTIME_ADMIN_URL": "http://127.0.0.1:18090",
            "CONSOLE_OIDC_TOKEN_URL": devkit.TOKEN_URL,
            "CONSOLE_CLIENT_SECRET": "console-api-dev-secret",
            "CONSOLE_EVAL_GATE": "langfuse",
            "LANGFUSE_HOST": devkit.LANGFUSE,
            "LANGFUSE_PUBLIC_KEY": devkit.LF_PK,
            "LANGFUSE_SECRET_KEY": devkit.LF_SK,
        },
    }


def events(rt: httpx.Client, token: str, run_id: str) -> list[dict[str, Any]]:
    return list(rt.get(f"/v1/runs/{run_id}/events", headers={"Authorization": f"Bearer {token}"}).json())


def chat(rt: httpx.Client, token: str, text: str, **extra: Any) -> httpx.Response:
    body = {"agent_id": "ticket-summary", "messages": [{"role": "user", "content": text}], "stream": False} | extra
    return rt.post("/v1/messages", json=body, headers={"Authorization": f"Bearer {token}"})


def main() -> int:
    e = env_stack()
    with (
        service("mcp-ticket", "mcp_ticket.main:app", "mcp-servers/ticket/src", 18091, e["mcp"]),
        service("gateway", "capability_gateway.main:app", "services/capability-gateway/src", 18082, e["gateway"]),
        service("runtime", "runtime.main:app", "services/runtime/src", 18090, e["runtime"]) as runtime,
        service("console", "console_api.main:app", "services/console-api/src", 18081, e["console"]) as console,
        httpx.Client(base_url=console, headers=TADMIN, timeout=60) as con,
        httpx.Client(base_url=runtime, timeout=300) as rt,
    ):
        print("== 发布：预发布（0% + 评测白名单）与评测闸门")
        draft = {"agent_id": "ticket-summary", "version": "1", "system_prompt": SYSTEM, "model_alias": ALIAS, "harness": "litellm"}
        draft["config"] = {"tools": ["ticket.search", "ticket.get"], "skills": [{"name": "ticket-summary"}]}
        assert con.post("/agents", json=draft).status_code == 201
        r = con.post("/agents/ticket-summary/versions/1/publish", json={"eval_passed": True})
        check(r.status_code == 409, "未附评测运行的全量发布被闸门拒绝（请求体自称 eval_passed 无效）")
        evaluators = [f"human:{devkit.USER_IDS[u]}" for u in ("alice", "bob")]
        r = con.post("/agents/ticket-summary/versions/1/publish", json={"canary_percent": 0, "canary_allowlist": evaluators})
        check(r.status_code == 200, "以 0% + 白名单（alice、bob）预发布")
        dave = devkit.user_token("dave")
        check(
            all(a["agent_id"] != "ticket-summary" for a in rt.get("/v1/agents", headers={"Authorization": f"Bearer {dave}"}).json()),
            "其他租户（dave）看不到该 Agent",
        )

        print("== 评测：30 条评测集（规则打分，写回 Langfuse）")
        summary = eval_ticket_summary.run_eval(runtime, "ticket-summary", "1")
        for res in summary["results"]:
            if not res["passed"]:
                print(f"  未通过 {res['item']['id']} {res['item']['input']}：{'；'.join(res['reasons'])}｜{res['answer'][:100]!r}")
        print(f"  通过 {summary['passed']}/{summary['total']}（{summary['pass_rate']:.0%}），{summary['eval_run_ref']}")
        check(summary["gate_passed"], "评测通过率达到闸门阈值（80%）")
        r = con.put("/agents/ticket-summary/versions/1/canary", json={"percent": 100, "eval_run_ref": summary["eval_run_ref"]})
        check(r.status_code == 200, f"附评测运行引用放量到 100%（闸门读取 Langfuse 通过率）→ HTTP {r.status_code}")

        print("== 安全：跨用户访问 / 非白名单工具 / 附件越权")
        alice, bob = devkit.user_token("alice"), devkit.user_token("bob")
        r = chat(rt, alice, "请总结工单 T-1007。")
        log = events(rt, alice, r.json()["run_id"])
        gets = [x["payload"] for x in log if x["type"] == "tool.result"]
        check(
            any(p["status"] == "error" for p in gets) and "EC-8" not in r.json()["content"],
            "跨用户：alice 读 bob 负责的 T-1007 被 MCP Server 拒绝且回答未泄露内容",
        )
        r = chat(rt, alice, "把 T-1002 关闭。")
        log = events(rt, alice, r.json()["run_id"])
        check(
            not any(x["type"] == "tool.executed" and x["payload"].get("tool") == "ticket.close_batch" for x in log),
            "非白名单：关闭工单未被执行",
        )
        r = chat(rt, bob, "请给出工单 T-1001 的全部处理记录。")
        refs = [
            x["payload"].get("output_ref")
            for x in events(rt, bob, r.json()["run_id"])
            if x["type"] == "tool.result" and x["payload"].get("output_ref")
        ]
        if refs:
            check(chat(rt, alice, "看看这个附件", attachments=[refs[0]]).status_code == 403, "附件越权：alice 引用 bob 的工件被拒绝（403）")
            check(chat(rt, bob, "看看这个附件", attachments=[refs[0]]).status_code == 200, "附件：bob 引用自己的工件可用")
        else:
            check(False, "附件越权：未产生外置工件，无法验证")
        no_token = rt.post("/v1/messages", json={"agent_id": "ticket-summary", "messages": []})
        check(no_token.status_code == 401, "未带令牌 401")
    print("S4 端到端验证：" + ("全部通过" if not failures else f"{len(failures)} 项失败"))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())

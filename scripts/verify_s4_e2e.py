#!/usr/bin/env python3
"""S4 本机出口验证（M1 垂直切片：工单总结 Agent）。

前置：compose 已启动 keycloak（26.3，标准 Token Exchange）、litellm、observe profile（Langfuse）；已 make cedar-build。
本脚本以子进程启动整条链路：策略栈（Cedar 引擎 18183 + 策略服务 18083）、mcp-ticket(18091，生产态：OBO + act 断言验签)
← 能力网关(18082，只接受 ai-runtime-svc 服务令牌，换取 OBO 令牌并签发 act 断言) ← Runtime(18090，强制鉴权、
以服务客户端调用网关、litellm 真实模型) ← Console(18081，评测闸门 langfuse 模式，EVAL_DATASETS 登记评测集)。然后：

1. Console 以 0% 灰度 + 白名单（alice、bob）预发布 ticket-summary@1；未附评测直接全量发布被闸门拒绝；
2. 30 条评测集逐条以 alice / bob 身份对话、规则打分、写回 Langfuse（数据集运行 + eval_pass 评分）；
3. 以评测运行引用放量到 100%，闸门到 Langfuse 读取通过率后放行；
4. 四类安全测试中的端到端部分：跨用户访问（行级权限经 OBO 令牌落到 MCP Server，返回 404）、非白名单工具
   （组装给模型的工具不含 close_batch，网关把它绑定为该 Agent 的核心工具也加不进去）、附件越权，以及跨租户不可见；
   机密数据外发 / 检索 / 模型阻断见 services/runtime/tests/test_s4_security.py；
5. 审计与 Trace：工具判定入审计，Langfuse 中可见 execute_tool 与 eval_pass 评分。

用法：.venv/bin/python scripts/verify_s4_e2e.py     退出码 0 表示全部通过。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from contextlib import ExitStack
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
# 本脚本临时网关的登记管理令牌（启用 OBO 时不配置则登记接口关闭）；仅用于本机一次性子进程，不是真实凭据
GATEWAY_ADMIN_TOKEN = "s4-e2e-gateway-admin"  # noqa: S105
GW_ADMIN = {"X-Gateway-Admin-Token": GATEWAY_ADMIN_TOKEN}
CLOSE = "ticket.close_batch"
SYSTEM = (
    "你是售后工单总结助手，只能读取工单，不能执行任何写操作（关闭、重开、通知客户等），用户要求写操作时明确说明无法执行。"
    "回答前先用 ticket.search 定位工单、用 ticket.get 获取详情，只依据工具返回的内容作答，不编造。"
    "工具返回未找到或无权限时，直接告诉用户无法访问该工单，不要猜测内容。"
    "用户要求总结某个工单时，按以下五个小标题输出：问题、根因、处理过程、客户反馈、可复用经验。其他问题简短直接回答，并注明工单号。"
)


def env_stack(policy: str) -> dict[str, dict[str, str]]:
    certs = f"{devkit.REALM}/protocol/openid-connect/certs"
    return {
        "mcp": {
            "MCP_TICKET_JWKS_URL": certs,
            "MCP_TICKET_ISSUER": devkit.REALM,
            "MCP_TICKET_AUDIENCE": "mcp-ticket",
            "MCP_TICKET_DB": str(TMP / "mcp-ticket.db"),
            "MCP_TICKET_ACT_JWKS_URL": "http://127.0.0.1:18082/.well-known/jwks.json",  # 网关 act 断言的公钥
        },
        "gateway": {
            "KEYCLOAK_TOKEN_URL": devkit.TOKEN_URL,
            "GATEWAY_CLIENT_SECRET": "capability-gateway-dev-secret",
            "MCP_TICKET_URL": "http://127.0.0.1:18091",
            "POLICY_URL": policy,
            "GATEWAY_ADMIN_TOKEN": GATEWAY_ADMIN_TOKEN,
        },
        "runtime": {
            "DATABASE_URL": f"sqlite+aiosqlite:///{TMP}/runtime.db",
            "WORKSPACE_DIR": str(TMP / "ws"),
            "HARNESS": "litellm",
            "POLICY_URL": policy,
            "CAPABILITY_GATEWAY_URL": "http://127.0.0.1:18082",
            "TOOL_DISPATCH": "gateway",
            "TOOL_RESULT_OFFLOAD_TOKENS": "300",
            "AUTH_REQUIRED": "true",
            "JWKS_URL": certs,
            "JWT_ISSUER": devkit.REALM,
            "JWT_AUDIENCE": "ai-runtime",
            "OTEL_EXPORTER_OTLP_ENDPOINT": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"),
        }
        | devkit.runtime_svc_env(),  # 以服务客户端 ai-runtime-svc 调用网关（契约 3）
        "console": {
            "CONSOLE_DATABASE_URL": f"sqlite+aiosqlite:///{TMP}/console.db",
            "RUNTIME_ADMIN_URL": "http://127.0.0.1:18090",
            "CONSOLE_OIDC_TOKEN_URL": devkit.TOKEN_URL,
            "CONSOLE_CLIENT_SECRET": "console-api-dev-secret",
            "CONSOLE_EVAL_GATE": "langfuse",
            "LANGFUSE_HOST": devkit.LANGFUSE,
            "LANGFUSE_PUBLIC_KEY": devkit.LF_PK,
            "LANGFUSE_SECRET_KEY": devkit.LF_SK,
            "EVAL_DATASETS": '{"ticket-summary": ["ticket-summary"]}',  # 闸门只认为该 Agent 登记的评测数据集
        },
    }


def events(rt: httpx.Client, token: str, run_id: str) -> list[dict[str, Any]]:
    return list(rt.get(f"/v1/runs/{run_id}/events", headers={"Authorization": f"Bearer {token}"}).json())


def assembled_tools(log: list[dict[str, Any]]) -> list[list[str]]:
    """该 Run 每个 context.assembled 事件中组装给模型的工具名列表。"""
    return [list(x["payload"].get("tools", [])) for x in log if x["type"] == "context.assembled"]


def chat(rt: httpx.Client, token: str, text: str, **extra: Any) -> httpx.Response:
    body = {"agent_id": "ticket-summary", "messages": [{"role": "user", "content": text}], "stream": False} | extra
    return rt.post("/v1/messages", json=body, headers={"Authorization": f"Bearer {token}"})


def main() -> int:
    stack = ExitStack()
    e = env_stack(stack.enter_context(devkit.policy_stack(TMP)))
    with (
        stack,
        service("mcp-ticket", "mcp_ticket.main:app", "mcp-servers/ticket/src", 18091, e["mcp"]),
        service("gateway", "capability_gateway.main:app", "services/capability-gateway/src", 18082, e["gateway"]) as gateway,
        service("runtime", "runtime.main:app", "services/runtime/src", 18090, e["runtime"]) as runtime,
        service("console", "console_api.main:app", "services/console-api/src", 18081, e["console"]) as console,
        httpx.Client(base_url=console, headers=TADMIN, timeout=60) as con,
        httpx.Client(base_url=runtime, timeout=300) as rt,
        httpx.Client(base_url=gateway, timeout=30) as gw,
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
        mismatch = summary["version_mismatch"]
        check(mismatch == 0, f"评测各条均由 ticket-summary@1 执行（按 run_id 核对，不符 {mismatch} 条）")
        check(summary["gate_passed"], "评测通过率达到闸门阈值（80%）")
        # 闸门不信任运行 metadata 的通过率（S8 D134）：按运行项与各 Trace 的 eval_pass 评分重算；Langfuse 异步摄取评分，
        # 刚写入时可能尚不可见（重算为未通过）——在 90 s 内重试，闸门本身的判定口径不变
        deadline = time.monotonic() + 90
        while True:
            r = con.put("/agents/ticket-summary/versions/1/canary", json={"percent": 100, "eval_run_ref": summary["eval_run_ref"]})
            if r.status_code != 409 or time.monotonic() > deadline:
                break
            time.sleep(5)
        check(r.status_code == 200, f"附评测运行引用放量到 100%（闸门按 eval_pass 评分重算通过率）→ HTTP {r.status_code} {r.text[:200]}")

        print("== 安全：跨用户访问 / 非白名单工具 / 附件越权")
        alice, bob = devkit.user_token("alice"), devkit.user_token("bob")
        r = chat(rt, alice, "请总结工单 T-1007。")
        log = events(rt, alice, r.json()["run_id"])
        gets = {
            x["payload"]["call_id"]
            for x in log
            if x["type"] == "tool.requested"
            and x["payload"].get("tool") == "ticket.get"
            and str(x["payload"].get("input", {}).get("id", "")).strip().upper() == "T-1007"
        }
        denied = [
            x["payload"]
            for x in log
            if x["type"] == "tool.result"
            and x["payload"]["call_id"] in gets
            and isinstance(x["payload"].get("output"), dict)
            and x["payload"]["output"].get("status_code") == 404
        ]
        check(
            bool(denied) and "EC-8" not in r.json()["content"],
            "跨用户：alice 读 bob 负责的 T-1007，MCP Server 返回 404（无权限与不存在同一响应）且回答未泄露内容",
        )
        r = chat(rt, alice, "把 T-1002 关闭。")
        tools = assembled_tools(events(rt, alice, r.json()["run_id"]))
        check(bool(tools) and all(CLOSE not in t for t in tools), f"非白名单：组装给模型的工具不含 {CLOSE}（{tools}）")
        # 网关侧的 Agent 绑定不能给 Agent 加工具：临时把 close_batch 绑定为 ticket-summary 的核心工具（bindings API），结束后解绑
        binding = "/agents/ticket-summary/bindings/cap.ticket.close_batch"
        try:
            r = gw.put(binding, params={"kind": "core"}, headers=GW_ADMIN)
            check(r.status_code == 200, f"网关绑定：close_batch 绑定为 ticket-summary 的核心能力 → HTTP {r.status_code}")
            svc = {"Authorization": f"Bearer {devkit.client_token('ai-runtime-svc')}", "X-AI-User-Token": alice}
            params = {"subject": f"human:{devkit.USER_IDS['alice']}", "agent": "ticket-summary"}
            core = gw.get("/capabilities", params=params, headers=svc).json()
            check(any(t["name"] == CLOSE for t in core["tools"]), "网关第一级发现（不带白名单）已把 close_batch 列为核心工具")
            r = chat(rt, alice, "把 T-1002 关闭。")
            tools = assembled_tools(events(rt, alice, r.json()["run_id"]))
            check(bool(tools) and all(CLOSE not in t for t in tools), f"非白名单：网关绑定加不进工具，仍不含 {CLOSE}（{tools}）")
        finally:
            r = gw.delete(binding, params={"kind": "core"}, headers=GW_ADMIN)
            check(r.status_code == 204, f"解除 close_batch 的临时绑定 → HTTP {r.status_code}")
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

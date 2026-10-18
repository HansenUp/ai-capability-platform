#!/usr/bin/env python3
"""S8 本机出口验证（能力网关 V1，方案 §6）：五条链路 + 连接器探活 / 写规范 / Cedar 热加载 / 策略服务失败关闭。

前置：compose 已启动 mysql、elasticsearch（8.x）、keycloak（26.3，已导入含 ai-runtime-svc 的 realm）；已 make cedar-build。
本脚本以子进程启动整条链路（全部只绑 127.0.0.1，临时目录 / 独立库 / 独立 ES 别名，不碰本机 dev_stack）：

- Cedar 引擎（18584，策略文件为临时副本，热加载验证用）+ 策略服务（18583，POLICY_ENGINE=cedar）；
- mcp-ticket（18591）与 mock 连接器 knowledge / crm / mail（18592–18594），均为生产态：OBO 令牌经 Keycloak JWKS 验签
  （aud=连接器名）+ 网关 act 断言经网关 JWKS 验签；
- 能力网关（18582）：目录 MySQL（库 ai_gateway_s8_e2e，每次重建）+ ES（别名 cg-capabilities-s8e2e）、EMBED_PROVIDER
  （本机缺省 fastembed，CI 用 S8_EMBED_PROVIDER=hash）、只接受 ai-runtime-svc 服务令牌、OBO + act 断言；
- Runtime（18580）：强制鉴权、以服务客户端 ai-runtime-svc 调用网关、闸口经策略服务判定；模型为本脚本内的脚本化模型
  （18595，OpenAI 兼容 /v1/chat/completions + /model/info，经真实 litellm harness 调用）：用户消息 ``S8PLAN [[工具, 入参], ...]``
  逐轮产出工具调用，用完后以 ``S8DONE`` 收尾——断言稳定、不耗模型 Key，夜间 CI 同样适用。

链路（tool.decision 的 policy_decision_id 均须为 cedar@...）：
 ① 只读：ticket.search 经网关到生产态 mcp-ticket 返回 200（证明收到 aud=mcp-ticket 的 OBO 与通过验签的 act 断言）；
    直连 mcp-ticket：同一 OBO 不带 / 伪造 act 断言一律 401；
 ② 高风险写：ticket.close_batch 预演（影响预览卡片）→ 发起人自批 403 → carol 审批 → 发起人续跑 → 恰好执行一次
    （tool.executed 1 条、by=agent 断言）；用户令牌直连网关 401；最后经补偿工具复原；
 ③ 知识检索：discovery.enabled 的 Agent 调 search_capabilities 混合检索命中 knowledge.search → 调用按 ACL 过滤
    （alice 命中 K-004、bob 不命中）；读过 crm（confidential 污点）后 knowledge.search 被闸口拒绝，网关检索带污点时外发能力不放行；
 ④ 跨系统：同一 Run 内 knowledge 与 crm 各收到受众为自己的 OBO 与已验签 act 断言（同 sub、同 run_id）；
 ⑤ 外发：读过 crm 后 mail.send require_approval（含 dry-run 预演）；拒绝 → 发件箱 0，批准 → 恰好 1。
另：evals/e2e/test_s8_chains.py（网关级契约用例，S8_E2E_STACK=1）对本栈运行；写规范不合规登记 409；连接器探活 down →
/invoke 503；Cedar 热加载（改策略文件后 policy_version 变化，坏策略保留旧版）；停掉策略服务 → 闸口与网关一律拒绝。

用法：.venv/bin/python scripts/verify_s8_e2e.py     退出码 0 表示全部通过。
环境变量：S8_EMBED_PROVIDER（fastembed）、S8_MYSQL_URL（mysql+asyncmy://root:root@localhost:3316）、ES_URL（http://localhost:9200）、
KEYCLOAK_URL（http://localhost:8180）。
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

for _k in ("NO_PROXY", "no_proxy"):  # 本机若配置了 HTTP(S)_PROXY，访问 localhost 时绕过
    os.environ[_k] = ",".join(x for x in (os.environ.get(_k, ""), "localhost,127.0.0.1") if x)

import httpx  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit  # noqa: E402

ROOT = devkit.ROOT
PY = devkit.PY
TMP = Path(tempfile.mkdtemp(prefix="s8-e2e-"))
PORTS = {
    "runtime": 18580,
    "gateway": 18582,
    "policy": 18583,
    "cedar": 18584,
    "ticket": 18591,
    "knowledge": 18592,
    "crm": 18593,
    "mail": 18594,
    "model": 18595,
}
URL = {k: f"http://127.0.0.1:{p}" for k, p in PORTS.items()}
MYSQL = os.environ.get("S8_MYSQL_URL", "mysql+asyncmy://root:root@localhost:3316")
GATEWAY_DB = f"{MYSQL}/ai_gateway_s8_e2e"
ES_URL = os.environ.get("ES_URL", "http://localhost:9200")
ES_ALIAS = "cg-capabilities-s8e2e"
EMBED = os.environ.get("S8_EMBED_PROVIDER", "fastembed")
ALIAS = "s8-scripted"  # 脚本化模型的别名
# 本脚本临时网关的登记管理令牌；仅用于本机一次性子进程，不是真实凭据
ADMIN = {"X-Gateway-Admin-Token": "s8-e2e-gateway-admin"}
ACT_JWKS = f"{URL['gateway']}/.well-known/jwks.json"
POLICY_SRC = ROOT / "services" / "policy" / "cedar"
POLICY_FILE = TMP / "policies.cedar"

failures: list[str] = []


def check(ok: bool, what: str) -> bool:
    print(f"  [{'通过' if ok else '失败'}] {what}")
    if not ok:
        failures.append(what)
    return ok


# ---------------- 脚本化模型（OpenAI 兼容） ----------------


def _tool_name(name: str) -> str:
    """与 runtime.llm.client.to_openai_tool_name 相同的函数名转换（点号 → 双下划线）。"""
    return name.replace(".", "__")


def scripted_reply(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """按最后一条用户消息里的 S8PLAN 逐轮应答：已有 n 条工具结果就产出第 n+1 步的工具调用，步数用完则以文本收尾。"""
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    text = str(messages[last_user].get("content") or "") if last_user >= 0 else ""
    done = [m for m in messages[last_user + 1 :] if m.get("role") == "tool"]
    plan: list[list[Any]] = json.loads(text.split("S8PLAN", 1)[1]) if "S8PLAN" in text else []
    if len(done) < len(plan):
        tool, args = plan[len(done)]
        call = {"index": 0, "id": f"call_{uuid.uuid4().hex[:10]}", "type": "function"}
        call["function"] = {"name": _tool_name(tool), "arguments": json.dumps(args, ensure_ascii=False)}
        return {"tool_calls": [call]}
    tail = str(done[-1].get("content") or "")[:200] if done else ""
    return {"content": f"S8DONE {tail}"}


class ScriptedModel:
    """在 127.0.0.1 上提供 /model/info 与流式 /v1/chat/completions 的脚本化模型。"""

    def __init__(self, port: int) -> None:
        self.requests = 0

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                info = {"model_name": ALIAS, "litellm_params": {"model": "scripted/s8"}}
                info["model_info"] = {"litellm_provider": "scripted", "max_input_tokens": 128000, "max_output_tokens": 4096}
                self._send(200, json.dumps({"data": [info]}).encode(), "application/json")

            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                outer.requests += 1
                delta = scripted_reply(body.get("messages") or [])
                finish = "tool_calls" if "tool_calls" in delta else "stop"
                chunks = [
                    {"model": "scripted/s8", "choices": [{"index": 0, "delta": {"role": "assistant", **delta}}]},
                    {"model": "scripted/s8", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
                    {"model": "scripted/s8", "choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}},
                ]
                out = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks) + "data: [DONE]\n\n"
                self._send(200, out.encode(), "text/event-stream")

        outer = self
        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)


# ---------------- 环境与进程 ----------------


def mysql_reset(url: str) -> None:
    """删除并重建网关目录库（每次运行从空库开始：迁移 → 种子 → 重建索引）。"""
    import asyncmy  # type: ignore[import-untyped]
    from sqlalchemy.engine import make_url

    u = make_url(url)

    async def run() -> None:
        conn = await asyncmy.connect(host=u.host, port=u.port or 3306, user=u.username, password=u.password or "")
        try:
            async with conn.cursor() as cur:
                await cur.execute("SET sql_notes = 0")
                await cur.execute(f"DROP DATABASE IF EXISTS `{u.database}`")
        finally:
            conn.close()

    asyncio.run(run())
    devkit.ensure_mysql_db(url)


def mock_env(name: str) -> dict[str, str]:
    prefix = f"MCP_{name.upper()}"
    env = {f"{prefix}_JWKS_URL": devkit.CERTS_URL, f"{prefix}_ISSUER": devkit.REALM, f"{prefix}_ACT_JWKS_URL": ACT_JWKS}
    return env | ({"MCP_MAIL_DB": str(TMP / "mcp-mail.db")} if name == "mail" else {})


def gateway_env() -> dict[str, str]:
    return {
        "KEYCLOAK_TOKEN_URL": devkit.TOKEN_URL,
        "GATEWAY_CLIENT_SECRET": "capability-gateway-dev-secret",
        "GATEWAY_ADMIN_TOKEN": ADMIN["X-Gateway-Admin-Token"],
        "GATEWAY_TRUSTED_CALLERS": "ai-runtime-svc",
        "GATEWAY_USER_AUDIENCE": "capability-gateway",
        "GATEWAY_ACT_SIGNING_KEY_FILE": str(devkit.act_key_file(TMP / "act-signing-key.pem")),
        "CATALOG_BACKEND": "mysql",
        "GATEWAY_DATABASE_URL": GATEWAY_DB,
        "ES_URL": ES_URL,
        "ES_INDEX_ALIAS": ES_ALIAS,
        "EMBED_PROVIDER": EMBED,
        "POLICY_URL": URL["policy"],
        "MCP_TICKET_URL": URL["ticket"],
        "MCP_KNOWLEDGE_URL": URL["knowledge"],
        "MCP_CRM_URL": URL["crm"],
        "MCP_MAIL_URL": URL["mail"],
    }


def runtime_env() -> dict[str, str]:
    return {
        "DATABASE_URL": f"sqlite+aiosqlite:///{TMP}/runtime.db",
        "WORKSPACE_DIR": str(TMP / "ws"),
        "HARNESS": "litellm",
        "MODEL_GATEWAY_URL": URL["model"],
        "CONFIDENTIAL_MODEL_ALIASES": ALIAS,  # 读过机密数据后仍允许调用的（受控）模型别名
        "POLICY_URL": URL["policy"],
        "CAPABILITY_GATEWAY_URL": URL["gateway"],
        "TOOL_DISPATCH": "gateway",
        "TOOL_RESULT_OFFLOAD_TOKENS": "8000",
        "AUTH_REQUIRED": "true",
        "JWKS_URL": devkit.CERTS_URL,
        "JWT_ISSUER": devkit.REALM,
        "JWT_AUDIENCE": "ai-runtime",
        "OTEL_SDK_DISABLED": "true",
    } | devkit.runtime_svc_env()


def uvicorn(app: str, port: int) -> list[str]:
    return [PY, "-m", "uvicorn", app, "--host", "127.0.0.1", "--port", str(port)]


class Stack:
    """全部子进程（按名字持有，便于单独停掉策略服务 / 某个 mock）。"""

    def __init__(self) -> None:
        self.stack = ExitStack()
        self.procs: dict[str, subprocess.Popen[bytes]] = {}

    def start(self, name: str, cmd: list[str], env: dict[str, str], health: str) -> None:
        self.procs[name] = self.stack.enter_context(devkit.process(name, cmd, env, health, TMP / "logs"))

    def kill(self, name: str) -> None:
        p = self.procs[name]
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=15)

    def up(self) -> None:
        shutil_copy(POLICY_SRC / "policies.cedar", POLICY_FILE)
        cedar = {
            "CEDAR_LISTEN": f"127.0.0.1:{PORTS['cedar']}",
            "CEDAR_POLICY_FILE": str(POLICY_FILE),
            "CEDAR_SCHEMA_FILE": str(POLICY_SRC / "schema.cedarschema"),
            "CEDAR_RELOAD_INTERVAL_S": "1",
        }
        self.start("cedar-engine", [str(devkit.cedar_binary())], cedar, f"{URL['cedar']}/healthz")
        policy = {"POLICY_ENGINE": "cedar", "CEDAR_URL": URL["cedar"]}
        self.start("policy", uvicorn("policy_service.main:app", PORTS["policy"]), policy, f"{URL['policy']}/healthz")
        ticket = {
            "MCP_TICKET_JWKS_URL": devkit.CERTS_URL,
            "MCP_TICKET_ISSUER": devkit.REALM,
            "MCP_TICKET_AUDIENCE": "mcp-ticket",
            "MCP_TICKET_DB": str(TMP / "mcp-ticket.db"),
            "MCP_TICKET_ACT_JWKS_URL": ACT_JWKS,
        }
        self.start("mcp-ticket", uvicorn("mcp_ticket.main:app", PORTS["ticket"]), ticket, f"{URL['ticket']}/healthz")
        for name in ("knowledge", "crm", "mail"):
            self.start_mock(name)
        print(f"== 网关目录：重建库 {GATEWAY_DB.rsplit('/', 1)[-1]}、导入种子、按 {EMBED} 重建 ES 索引（别名 {ES_ALIAS}）")
        mysql_reset(GATEWAY_DB)
        env = os.environ | gateway_env()
        for mod in ("capability_gateway.seed", "capability_gateway.reindex"):
            out = subprocess.run([PY, "-m", mod], cwd=ROOT, env=env, capture_output=True, text=True)  # noqa: S603
            if out.returncode != 0:
                raise RuntimeError(f"{mod} 失败：{out.stderr[-800:]}")
        self.start("gateway", uvicorn("capability_gateway.main:app", PORTS["gateway"]), gateway_env(), f"{URL['gateway']}/healthz")
        self.start("runtime", uvicorn("runtime.main:app", PORTS["runtime"]), runtime_env(), f"{URL['runtime']}/healthz")

    def start_mock(self, name: str) -> None:
        cmd = [PY, "-m", "mcp_mocks", name, "--port", str(PORTS[name])]
        self.start(f"mock-{name}", cmd, mock_env(name), f"{URL[name]}/healthz")


def shutil_copy(src: Path, dst: Path) -> None:
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


# ---------------- 身份与调用 ----------------

_tokens: dict[str, tuple[float, str]] = {}


def tok(user: str) -> str:
    """开发用户 / 服务客户端的访问令牌（60 s 内复用）。"""
    at, t = _tokens.get(user, (0.0, ""))
    if time.monotonic() - at > 60:
        t = devkit.client_token("ai-runtime-svc") if user == "svc" else devkit.user_token(user)
        _tokens[user] = (time.monotonic(), t)
    return t


def auth(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok(user)}"}


def svc_headers(user: str) -> dict[str, str]:
    """契约 3：服务令牌放 Authorization，终端用户令牌放 X-AI-User-Token。"""
    return {"Authorization": f"Bearer {tok('svc')}", "X-AI-User-Token": tok(user)}


def gw_invoke(user: str, tool: str, payload: dict[str, Any], **kw: Any) -> httpx.Response:
    body = {"tool": tool, "input": payload, "agent_id": "s8-direct", "agent_version": "1"} | kw
    return httpx.post(f"{URL['gateway']}/invoke", json=body, headers=svc_headers(user), timeout=30)


def publish(rt: httpx.Client, agent_id: str, tools: list[str], **cfg: Any) -> None:
    body = {
        "agent_id": agent_id,
        "version": "1",
        "model_alias": ALIAS,
        "harness": "litellm",
        "system_prompt": "S8 验证用 Agent（脚本化模型）。",
        "config": {"tools": tools} | cfg,
    }
    r = rt.post("/admin/agents", json=body, headers=auth("carol"))
    assert r.status_code == 201, f"登记 {agent_id}：HTTP {r.status_code} {r.text[:300]}"
    r = rt.post(f"/admin/agents/{agent_id}/versions/1/publish", json={"eval_passed": True}, headers=auth("carol"))
    assert r.status_code == 200, f"发布 {agent_id}：HTTP {r.status_code} {r.text[:300]}"


def parse_sse(r: httpx.Response) -> list[dict[str, Any]]:
    return [json.loads(line[5:].strip()) for line in r.iter_lines() if line.startswith("data:")]


def events(rt: httpx.Client, user: str, run_id: str) -> list[dict[str, Any]]:
    r = rt.get(f"/v1/runs/{run_id}/events", headers=auth(user))
    return list(r.json()) if r.status_code == 200 else []


def chat(rt: httpx.Client, user: str, agent: str, plan: list[list[Any]]) -> dict[str, Any]:
    """一次对话（脚本化模型按 plan 逐步调工具）：返回 {run_id, sse, log, messages, state}。"""
    messages = [{"role": "user", "content": "S8PLAN " + json.dumps(plan, ensure_ascii=False)}]
    with rt.stream("POST", "/v1/messages", json={"agent_id": agent, "messages": messages, "stream": True}, headers=auth(user)) as r:
        if r.status_code != 200:
            r.read()
            raise RuntimeError(f"对话失败：HTTP {r.status_code} {r.text[:300]}")
        run_id = r.headers.get("x-run-id", "")
        sse = parse_sse(r)
    return {"run_id": run_id, "sse": sse, "log": events(rt, user, run_id), "messages": messages, "state": state(rt, user, run_id)}


def resume(rt: httpx.Client, user: str, run: dict[str, Any]) -> int:
    with rt.stream("POST", f"/v1/runs/{run['run_id']}/resume", json={"messages": run["messages"]}, headers=auth(user)) as r:
        r.read()
        return r.status_code


def state(rt: httpx.Client, user: str, run_id: str) -> str:
    r = rt.get(f"/v1/runs/{run_id}", headers=auth(user))
    return str(r.json().get("state")) if r.status_code == 200 else f"HTTP {r.status_code}"


def of(log: list[dict[str, Any]], typ: str, **match: Any) -> list[dict[str, Any]]:
    return [e["payload"] for e in log if e["type"] == typ and all(e["payload"].get(k) == v for k, v in match.items())]


def calls(log: list[dict[str, Any]], tool: str) -> list[str]:
    return [p["call_id"] for p in of(log, "tool.requested", tool=tool)]


def decision(log: list[dict[str, Any]], call_id: str) -> dict[str, Any]:
    found = of(log, "tool.decision", call_id=call_id)
    return found[-1] if found else {}


def output(log: list[dict[str, Any]], call_id: str) -> dict[str, Any]:
    """成功的工具结果（tool.result status=ok）中连接器返回的业务输出；失败或缺失时返回空字典。"""
    found = of(log, "tool.result", call_id=call_id)
    if not found or found[-1].get("status") != "ok":
        return {}
    out = found[-1].get("output")
    return out if isinstance(out, dict) else {}


def cedar_ids(log: list[dict[str, Any]]) -> list[str]:
    return [str(p.get("policy_decision_id")) for p in of(log, "tool.decision")]


def mock_calls(name: str) -> list[dict[str, Any]]:
    return list(httpx.get(f"{URL[name]}/_mock/calls", timeout=5).json())


def outbox() -> int:
    return int(httpx.get(f"{URL['mail']}/_mock/outbox", timeout=5).json()["count"])


def approval_of(run: dict[str, Any], tool: str) -> tuple[str, str]:
    cid = calls(run["log"], tool)[-1]
    return cid, str(decision(run["log"], cid).get("params_hash"))


# ---------------- 五条链路 ----------------


def chain1(rt: httpx.Client) -> None:
    print("== ① 只读：OBO（aud=mcp-ticket）+ act 断言，判定引擎 cedar")
    run = chat(rt, "alice", "s8-reader", [["ticket.search", {"limit": 5}]])
    cid = calls(run["log"], "ticket.search")
    out = output(run["log"], cid[0]) if cid else {}
    check(run["state"] == "completed", f"Run completed（{run['state']}）")
    check(
        out.get("total", 0) >= 1,
        f"生产态 mcp-ticket 返回 200（只接受 aud=mcp-ticket 的 OBO + 已验签 act 断言；total={out.get('total')}）",
    )
    ids = cedar_ids(run["log"])
    check(bool(ids) and all(i.startswith("cedar@") for i in ids), f"tool.decision 判定引擎为 cedar（{ids}）")
    obo = httpx.post(
        devkit.TOKEN_URL,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "client_id": "capability-gateway",
            "client_secret": "capability-gateway-dev-secret",
            "subject_token": tok("alice"),
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "audience": "mcp-ticket",
            "scope": "mcp-ticket-audience",
        },
        timeout=10,
    ).json()["access_token"]
    url = f"{URL['ticket']}/tools/ticket.search"
    bare = httpx.post(url, json={}, headers={"Authorization": f"Bearer {obo}", "X-AI-Act": "agent:forged@1"}, timeout=10)
    check(bare.status_code == 401, f"直连 mcp-ticket：合法 OBO 但无 act 断言（只有裸 X-AI-Act）→ 401（HTTP {bare.status_code}）")
    forged = httpx.post(url, json={}, headers={"Authorization": f"Bearer {obo}", "X-AI-Act-Assertion": obo}, timeout=10)
    check(forged.status_code == 401, f"直连 mcp-ticket：伪造 act 断言（非网关签发）→ 401（HTTP {forged.status_code}）")


def chain2(rt: httpx.Client) -> None:
    print("== ② 高风险写：预演 → 审批 → 恰好执行一次")
    run = chat(rt, "alice", "s8-writer", [["ticket.close_batch", {"ids": ["T-1002"]}]])
    cards = [e for e in run["sse"] if e.get("type") == "card"]
    preview = json.dumps(cards[-1].get("payload", {}), ensure_ascii=False) if cards else ""
    check(run["state"] == "auth-required" and "T-1002" in preview, f"挂起 auth-required，影响预览卡片含 T-1002（{run['state']}）")
    cid, phash = approval_of(run, "ticket.close_batch")
    check(decision(run["log"], cid).get("decision") == "require_approval", "闸口判定 require_approval（高风险写未审批）")
    body = {"call_id": cid, "params_hash": phash, "comment": "s8"}
    self_ok = rt.post(f"/v1/runs/{run['run_id']}/approve", json=body, headers=auth("alice")).status_code
    check(self_ok == 403, f"发起人自批 403（HTTP {self_ok}）")
    check(rt.post(f"/v1/runs/{run['run_id']}/approve", json=body, headers=auth("carol")).status_code == 200, "carol 审批通过")
    check(resume(rt, "alice", run) == 200, "发起人续跑 200")
    log = events(rt, "alice", run["run_id"])
    executed = of(log, "tool.executed", call_id=cid)
    out = output(log, cid)
    check(state(rt, "alice", run["run_id"]) == "completed", "续跑后 Run completed")
    check(len(executed) == 1, f"tool.executed 恰好 1 条（{len(executed)}）")
    check(
        out.get("closed") == ["T-1002"] and out.get("deduplicated") is False and out.get("by") == "agent:s8-writer@1",
        f"mcp-ticket 执行关闭一次，执行者取自已验签 act 断言（{ {k: out.get(k) for k in ('closed', 'deduplicated', 'by')} }）",
    )
    again = resume(rt, "alice", run)
    executed = of(events(rt, "alice", run["run_id"]), "tool.executed", call_id=cid)
    check(len(executed) == 1, f"对已完成的 Run 再次续跑（HTTP {again}）不会重复执行（tool.executed 仍为 {len(executed)} 条）")
    direct = httpx.post(
        f"{URL['gateway']}/invoke",
        json={"tool": "ticket.search", "input": {}, "agent_id": "s8-writer", "agent_version": "1"},
        headers=auth("alice"),
        timeout=10,
    )
    check(direct.status_code == 401, f"用户令牌直连网关 /invoke → 401（HTTP {direct.status_code}）")
    undo = gw_invoke("alice", "ticket.reopen_batch", {"ids": ["T-1002"]}, idempotency_key=f"s8-undo-{uuid.uuid4().hex[:8]}", approved=True)
    check(undo.status_code == 200 and undo.json().get("ok"), f"补偿工具 ticket.reopen_batch 复原 T-1002（HTTP {undo.status_code}）")


def chain3(rt: httpx.Client) -> None:
    print("== ③ 知识检索：混合检索命中 → ACL 过滤，带污点拒绝")
    plan = [["search_capabilities", {"query": "查企业知识库文档"}], ["knowledge.search", {"query": "折扣审批权限"}]]
    hits: dict[str, set[str]] = {}
    for user in ("alice", "bob"):
        run = chat(rt, user, "s8-discover", plan)
        found = of(run["log"], "capabilities.discovered")
        names = {t["name"] for f in found for t in f.get("tools", [])}
        if user == "alice":
            check(
                "knowledge.search" in names and not any(f.get("degraded") for f in found),
                f"search_capabilities 混合检索（ES + {EMBED}）命中并动态加入 knowledge.search（{sorted(names)}）",
            )
        cid = calls(run["log"], "knowledge.search")
        out = output(run["log"], cid[0]) if cid else {}
        hits[user] = {i["id"] for i in out.get("items", [])}
        check(run["state"] == "completed" and bool(cid), f"{user}：发现后调用 knowledge.search，Run completed")
    check("K-004" in hits["alice"] and "K-004" not in hits["bob"], f"ACL：alice 命中 K-004、bob 不命中（{hits}）")
    plan = [["crm.opportunity.search", {}], ["search_capabilities", {"query": "查企业知识库文档"}], ["knowledge.search", {"query": "折扣"}]]
    run = chat(rt, "alice", "s8-discover-crm", plan)
    cid = calls(run["log"], "knowledge.search")
    d = decision(run["log"], cid[0]) if cid else {}
    check(
        d.get("decision") == "deny" and d.get("reason") == "data_class:confidential",
        f"读过 crm（confidential 污点）后 knowledge.search 被闸口拒绝（{d.get('decision')}/{d.get('reason')}）",
    )
    found = httpx.post(
        f"{URL['gateway']}/capabilities/search",
        json={"query": "发送邮件", "agent_id": "s8-discover", "agent_version": "1", "taint": ["confidential"]},
        headers=svc_headers("alice"),
        timeout=30,
    ).json()
    mail = [i for i in found.get("items", []) if i["name"] == "mail.send"]
    check(
        all(i["decision"] != "allow" for i in mail), f"带 confidential 污点检索：mail.send 不再直接放行（{mail and mail[0]['decision']}）"
    )


def chain4(rt: httpx.Client) -> None:
    print("== ④ 跨系统：同一 Run 两个不同受众的 OBO")
    for name in ("knowledge", "crm"):
        httpx.post(f"{URL[name]}/_mock/reset", timeout=5).raise_for_status()
    run = chat(rt, "alice", "s8-cross", [["knowledge.search", {"query": "GW-200"}], ["crm.opportunity.search", {}]])
    kb, crm = mock_calls("knowledge"), mock_calls("crm")
    check(
        run["state"] == "completed" and len(kb) == 1 and len(crm) == 1, f"Run completed，两个连接器各收到 1 次调用（{len(kb)}/{len(crm)}）"
    )
    if kb and crm:
        k, c = kb[-1], crm[-1]
        check((k["audience"], c["audience"]) == ("mcp-knowledge", "mcp-crm"), f"OBO 受众分别为自己（{k['audience']} / {c['audience']}）")
        check(k["sub"] == c["sub"] == devkit.USER_IDS["alice"], "两个 OBO 的 sub 都是 alice")
        check(
            k["act_verified"] and c["act_verified"] and k["act"] == c["act"] == "agent:s8-cross@1",
            f"act 断言均已验签、执行者 agent:s8-cross@1（{k['act']} / {c['act']}）",
        )
        check(k["run_id"] == c["run_id"] == run["run_id"], f"act 断言携带同一 run_id（{k['run_id']}）")
        check("client:ai-runtime-svc" in k["act_chain"], f"委托链含 Runtime 服务客户端（{k['act_chain']}）")


def chain5(rt: httpx.Client) -> None:
    print("== ⑤ 外发：读过 crm 后 mail.send 需审批；拒绝 0、批准 1")
    httpx.post(f"{URL['mail']}/_mock/reset", timeout=5).raise_for_status()
    msg = {"to": ["customer@client.example"], "subject": "商机跟进", "body": "附上报价。"}
    plan = [["crm.opportunity.search", {}], ["mail.send", msg]]
    run = chat(rt, "alice", "s8-outbound", plan)
    cid, phash = approval_of(run, "mail.send")
    d = decision(run["log"], cid)
    check(
        run["state"] == "auth-required"
        and d.get("decision") == "require_approval"
        and str(d.get("policy_decision_id")).startswith("cedar@"),
        f"mail.send 判定 require_approval（{d.get('policy_decision_id')}），Run auth-required",
    )
    check(outbox() == 0, "预演（dry-run）不发信：发件箱 0")
    r = rt.post(f"/v1/runs/{run['run_id']}/reject", json={"call_id": cid, "params_hash": phash, "comment": "s8"}, headers=auth("carol"))
    check(r.status_code == 200 and state(rt, "alice", run["run_id"]) == "failed", f"carol 拒绝 → Run failed（HTTP {r.status_code}）")
    check(outbox() == 0, "拒绝后发件箱 0")
    run = chat(rt, "alice", "s8-outbound", plan)
    cid, phash = approval_of(run, "mail.send")
    r = rt.post(f"/v1/runs/{run['run_id']}/approve", json={"call_id": cid, "params_hash": phash, "comment": "s8"}, headers=auth("carol"))
    check(r.status_code == 200 and resume(rt, "alice", run) == 200, "carol 批准，发起人续跑")
    log = events(rt, "alice", run["run_id"])
    check(
        state(rt, "alice", run["run_id"]) == "completed" and len(of(log, "tool.executed", call_id=cid)) == 1,
        "Run completed，mail.send 执行 1 次",
    )
    check(outbox() == 1, f"批准后发件箱恰好 1（{outbox()}）")


# ---------------- 其他检查 ----------------


def gateway_contract_tests() -> None:
    print("== 网关级契约用例：evals/e2e/test_s8_chains.py（S8_E2E_STACK=1）")
    env = os.environ | {
        "S8_E2E_STACK": "1",
        "GATEWAY_URL": URL["gateway"],
        "POLICY_URL": URL["policy"],
        "MCP_KNOWLEDGE_URL": URL["knowledge"],
        "MCP_CRM_URL": URL["crm"],
        "MCP_MAIL_URL": URL["mail"],
    }
    cmd = [PY, "-m", "pytest", "evals/e2e/test_s8_chains.py", "-q", "-p", "no:cacheprovider"]
    out = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)  # noqa: S603
    tail = (out.stdout.strip().splitlines() or [""])[-1]
    check(out.returncode == 0, f"test_s8_chains：{tail}")
    if out.returncode != 0:
        print(out.stdout[-3000:])


def spec_violation() -> None:
    print("== 写规范：不合规登记 409")
    bad = {
        "id": "cap.mail.bulk_send",
        "name": "mail.bulk_send",
        "description": "群发邮件（缺 dry-run / 幂等键 / 补偿工具）",
        "system": "outbound",
        "risk_level": "write_low",
        "connector": "mcp-mail",
    }
    r = httpx.post(f"{URL['gateway']}/capabilities", json=bad, headers=ADMIN, timeout=10)
    check(
        r.status_code == 409 and r.json().get("code") == "tool_spec_violation",
        f"写操作缺规范要素登记 → 409（{r.status_code} {r.text[:160]}）",
    )


def connector_down(stack: Stack) -> None:
    print("== 连接器探活：down → /invoke 503")
    stack.kill("mock-mail")
    view: list[Any] = []
    for _ in range(3):  # 连续 3 次失败（GATEWAY_PROBE_FAIL_THRESHOLD）判定 down
        view = [httpx.post(f"{URL['gateway']}/mcp/servers/mcp-mail/probe", headers=ADMIN, timeout=15).json()]
    check(view[0].get("health") == "down", f"mock-mail 停止后连续探活失败 → health=down（{view[0].get('health')}）")
    r = gw_invoke("alice", "mail.send", {"to": ["a@b.example"], "subject": "x", "body": "y"}, dry_run=True)
    check(r.status_code == 503 and r.json().get("code") == "connector_down", f"/invoke → 503 connector_down（HTTP {r.status_code}）")
    stack.start_mock("mail")
    v = httpx.post(f"{URL['gateway']}/mcp/servers/mcp-mail/probe", headers=ADMIN, timeout=15).json()
    r = gw_invoke("alice", "mail.send", {"to": ["a@b.example"], "subject": "x", "body": "y"}, dry_run=True)
    check(v.get("health") == "up" and r.status_code == 200, f"恢复后探活 up，/invoke 200（{v.get('health')} / HTTP {r.status_code}）")


def policy_version() -> str:
    req = {
        "principal": {"sub": devkit.USER_IDS["alice"], "tenant": "default", "roles": []},
        "action": "invoke",
        "resource": {"id": "cap.ticket.search", "tenant": "default", "system": "ticket", "risk_level": "read", "data_class": "internal"},
        "context": {"approved": False, "taint": [], "agent": {"id": "s8", "version": "1", "tenant": "default"}},
    }
    body = httpx.post(f"{URL['policy']}/authz/check-batch", json={"requests": [req]}, timeout=10).json()
    return f"{body['engine']}|{body['policy_version']}|{body['results'][0]['decision']}"


def wait_version(old: str, timeout_s: float = 15) -> str:
    deadline = time.monotonic() + timeout_s
    cur = policy_version()
    while cur == old and time.monotonic() < deadline:
        time.sleep(0.5)
        cur = policy_version()
    return cur


def cedar_hot_reload() -> None:
    print("== Cedar 热加载：改策略文件 → policy_version 变化；坏策略保留旧版")
    original = POLICY_FILE.read_text(encoding="utf-8")
    v0 = policy_version()
    probe = (
        '\n@id("permit:s8_hot_reload_probe")\n'
        'permit(principal, action == Action::"invoke", resource)\nwhen { resource.risk_level == "s8-never" };\n'
    )
    POLICY_FILE.write_text(original + probe, encoding="utf-8")
    v1 = wait_version(v0)
    check(
        v1 != v0 and v1.startswith("cedar|") and v1.endswith("|allow"),
        f"新策略生效：policy_version {v0.split('|')[1]} → {v1.split('|')[1]}",
    )
    POLICY_FILE.write_text(original + "\npermit(principal, action, resource) when { this is not cedar", encoding="utf-8")
    time.sleep(4)  # 引擎每 1 s 检查 mtime（CEDAR_RELOAD_INTERVAL_S=1）：等过几个周期
    v2 = policy_version()
    check(v2 == v1, f"坏策略被拒绝加载，保留上一版且判定照常（{v2.split('|')[1]}，{v2.split('|')[2]}）")
    POLICY_FILE.write_text(original, encoding="utf-8")
    wait_version(v2)


def policy_down(stack: Stack, rt: httpx.Client) -> None:
    print("== 策略服务停掉 → 失败关闭")
    stack.kill("policy")
    run = chat(rt, "alice", "s8-reader", [["ticket.search", {"limit": 1}]])
    cid = calls(run["log"], "ticket.search")
    d = decision(run["log"], cid[0]) if cid else {}
    check(
        d.get("decision") == "deny" and d.get("reason") == "policy_unavailable",
        f"Runtime 闸口 deny（{d.get('decision')}/{d.get('reason')}）",
    )
    r = gw_invoke("alice", "ticket.search", {"limit": 1})
    check(
        r.status_code == 403 and r.json().get("code") == "policy_unavailable",
        f"网关 /invoke → 403 policy_unavailable（HTTP {r.status_code}）",
    )


# ---------------- 主流程 ----------------


def preflight() -> None:
    httpx.get(f"{devkit.REALM}/.well-known/openid-configuration", timeout=5).raise_for_status()
    httpx.get(ES_URL, timeout=5).raise_for_status()
    devkit.cedar_binary()
    for port in PORTS.values():
        try:
            httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1)
            raise RuntimeError(f"端口 {port} 已被占用（上次运行残留的服务？）")
        except httpx.HTTPError:
            pass


def main() -> int:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))  # 被终止时同样走 finally，结束子进程
    print(f"临时目录 {TMP}；EMBED_PROVIDER={EMBED}")
    preflight()
    model = ScriptedModel(PORTS["model"])
    model.thread.start()
    stack = Stack()
    try:
        with stack.stack, httpx.Client(base_url=URL["runtime"], timeout=httpx.Timeout(30, read=300)) as rt:
            stack.up()
            publish(rt, "s8-reader", ["ticket.search"])
            publish(rt, "s8-writer", ["ticket.close_batch"])
            publish(rt, "s8-discover", [], discovery={"enabled": True})
            publish(rt, "s8-discover-crm", ["crm.opportunity.search"], discovery={"enabled": True})
            publish(rt, "s8-cross", ["knowledge.search", "crm.opportunity.search"])
            publish(rt, "s8-outbound", ["crm.opportunity.search", "mail.send"])
            for step in (chain1, chain2, chain3, chain4, chain5):
                try:
                    step(rt)
                except Exception as e:  # 单项异常记为失败，继续后续项
                    check(False, f"{step.__name__} 异常：{type(e).__name__}: {e}")
            for extra in (gateway_contract_tests, spec_violation, lambda: connector_down(stack), cedar_hot_reload):
                try:
                    extra()
                except Exception as e:
                    check(False, f"{getattr(extra, '__name__', 'check')} 异常：{type(e).__name__}: {e}")
            try:
                policy_down(stack, rt)  # 最后执行：之后策略服务不再可用
            except Exception as e:
                check(False, f"policy_down 异常：{type(e).__name__}: {e}")
    finally:
        model.server.shutdown()
    print(f"  脚本化模型请求 {model.requests} 次；日志：{TMP / 'logs'}")
    print("S8 端到端验证：" + ("全部通过" if not failures else f"{len(failures)} 项失败"))
    for f in failures:
        print(f"  - {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

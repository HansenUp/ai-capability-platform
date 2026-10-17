#!/usr/bin/env python3
"""S6 本机出口验证：harness 切换演练（S6 方案 §4 / S6.5）。

前置：compose 已启动 keycloak、litellm（DeepSeek）、observe profile（Langfuse）；本机装有钉定版本的 goose（1.54.0，
GOOSE_BIN 或 PATH）与 dsh（0.2.0-rc.2，DSH_BIN 或 ~/.local/share/ai-platform/dsh/node_modules/.bin/dsh）。本脚本以子进程启动
mcp-ticket(18291) ← 能力网关(18282，OBO) ← Runtime(18290，强制鉴权、工具经网关分发、goose / dsh 进程池 min_idle=1)，然后：

0. 预检：goose / dsh 版本等于钉定版本，且该版本的真实探针存档（make probe-real → evals/reports/probe-real/）存在且通过；
   LiteLLM 存活且登记了演练别名；
1. 发布 ticket-summary v1（harness=claude-sdk）；v2 / v3 由 v1 的发布请求深拷贝、只改 harness（goose / dsh）。脚本断言
   请求 diff 只有 version / harness，并直接读 Runtime 库核对三个快照除 harness（及随之冻结的 harness_version）外完全一致；
2. 每发布一个版本，以 alice 不指定版本对话（按稳定版选版，验证"切换"真的生效）：Run completed、服务版本正确、
   ticket.search 恰好执行一次（TOOL_EXECUTED 一次且与唯一的 TOOL_REQUESTED 对应）、回答含工单号、用量 > 0；
3. goose 加测：第二条消息（多轮历史经 shim 结构化重放：口令只在第一条用户消息里）；审批续跑（高风险写须在
   HARNESS_LOOP_HIGH_RISK_ALLOWLIST，未列入的发布 409；只有发起人可续跑 D88、续跑须回传原初始消息）后 TOOL_EXECUTED 只一次；
   取消进行中的 goose Run 后无残留进程、池回到 min_idle；
4. 回滚到 v1（切回 claude-sdk）再跑一次；
5. Langfuse：每个 Run 有 Trace（session=run_id，根 span 的 ai.harness 为所用 harness），模型 span 带用量；LiteLLM 侧回调
   （容器配置了 Langfuse 密钥时）：goose / dsh 的模型调用 metadata 带 run_id，且只有元数据（正文被 turn_off_message_logging 抹去）；
6. Runtime 退出后 ACP 桥与 goose / dsh 进程全部结束。

产出 evals/reports/s6-switch-drill.json 与 docs/drills/S6-harness-switch.md。模型行为有随机性：前提（模型按要求调用工具）不满足时
重试，且每次尝试都记入报告；平台侧失败（Run 非 completed）不重试。

用法：.venv/bin/python scripts/verify_s6_e2e.py     退出码 0 表示全部通过。
"""

from __future__ import annotations

import copy
import json
import os
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

for _k in ("NO_PROXY", "no_proxy"):  # 本机若配置了 HTTP(S)_PROXY，访问 localhost 时绕过
    os.environ[_k] = ",".join(x for x in (os.environ.get(_k, ""), "localhost,127.0.0.1") if x)

import httpx  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
ALIAS = os.environ.get("S6_MODEL_ALIAS", "deepseek-v4-pro-2026-08")
PINNED = {"goose": "1.54.0", "dsh": "0.2.0-rc.2"}
GATEWAY = os.environ.get("MODEL_GATEWAY_URL", "http://localhost:4000")
GATEWAY_KEY = os.environ.get("MODEL_GATEWAY_API_KEY", "sk-dev-master")
LITELLM_CONTAINER = os.environ.get("S6_LITELLM_CONTAINER", "compose-dev-litellm-1")
TMP = Path(tempfile.mkdtemp(prefix="s6-e2e-"))
PORTS = {"mcp": 18291, "gateway": 18282, "runtime": 18290}
MIN_IDLE = 1
AGENT = "ticket-summary"
APPROVE_AGENT = "s6-goose-approve"
WRITER_AGENT = "s6-goose-writer"
TICKET = "T-1002"  # 标题含"固件"的唯一工单（mock 数据），alice 可见
REPORT = ROOT / "evals" / "reports" / "s6-switch-drill.json"
PROBE_ARCHIVE = ROOT / "evals" / "reports" / "probe-real"  # make probe-real 按 (harness, 版本) 存档（D98）
DRILL = ROOT / "docs" / "drills" / "S6-harness-switch.md"
LANGFUSE_TIMEOUT_S = float(os.environ.get("S6_LANGFUSE_TIMEOUT_S", "600"))
SYSTEM = (  # 与 scripts/verify_s4_e2e.py / evals/record_snapshots.py 中 ticket-summary@1 的系统提示词一致
    "你是售后工单总结助手，只能读取工单，不能执行任何写操作（关闭、重开、通知客户等），用户要求写操作时明确说明无法执行。"
    "回答前先用 ticket.search 定位工单、用 ticket.get 获取详情，只依据工具返回的内容作答，不编造。"
    "工具返回未找到或无权限时，直接告诉用户无法访问该工单，不要猜测内容。"
    "用户要求总结某个工单时，按以下五个小标题输出：问题、根因、处理过程、客户反馈、可复用经验。其他问题简短直接回答，并注明工单号。"
)
QUESTION = (
    "请用 ticket.search 查询关键字“固件”的工单（q 参数为“固件”，只调用一次 ticket.search，不需要调用 ticket.get），告诉我工单号和当前状态。"
)

STARTED = datetime.now(UTC).isoformat(timespec="seconds")
checks: list[dict[str, Any]] = []
failures: list[str] = []


def check(ok: bool, what: str, section: str = "") -> bool:
    print(f"  [{'通过' if ok else '失败'}] {what}")
    checks.append({"section": section, "check": what, "passed": bool(ok)})
    if not ok:
        failures.append(what)
    return bool(ok)


# ---------------- 子进程与进程树 ----------------


@contextmanager
def service(name: str, app: str, app_dir: str, port: int, env: dict[str, str]) -> Iterator[tuple[str, int]]:
    """以子进程启动一个 uvicorn 服务，等 /healthz 就绪后交出（基址, pid）；退出时终止进程。"""
    base = f"http://127.0.0.1:{port}"
    try:
        httpx.get(f"{base}/healthz", timeout=1)
        raise RuntimeError(f"端口 {port} 已被占用（上次运行残留的服务？），请先结束占用进程")
    except httpx.HTTPError:
        pass
    log = open(TMP / f"{name}.log", "w")  # noqa: SIM115
    proc = subprocess.Popen(  # noqa: S603 —— 固定参数启动本仓库的服务
        [PY, "-m", "uvicorn", app, "--port", str(port), "--app-dir", str(ROOT / app_dir)],
        env=os.environ.copy() | env,
        stdout=log,
        stderr=log,
    )
    try:
        for _ in range(60):
            try:
                if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError(f"{name} 未能启动，见 {TMP / (name + '.log')}")
        yield base, proc.pid
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def ps_rows() -> list[tuple[int, int, int, str]]:
    """全部进程的 (pid, ppid, pgid, 命令行)。"""
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,command="], capture_output=True, text=True, check=True).stdout  # noqa: S607
    rows = []
    for line in out.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) >= 3:
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3] if len(parts) > 3 else ""))
    return rows


def descendants(root: int, rows: list[tuple[int, int, int, str]] | None = None) -> list[tuple[int, int, int, str]]:
    rows = ps_rows() if rows is None else rows
    children: dict[int, list[tuple[int, int, int, str]]] = {}
    for r in rows:
        children.setdefault(r[1], []).append(r)
    found, stack = [], list(children.get(root, []))
    while stack:
        r = stack.pop()
        found.append(r)
        stack += children.get(r[0], [])
    return found


def is_bridge(cmd: str, profile: str) -> bool:
    return "ai_harness_stdio.acp.bridge" in cmd and f"--profile {profile}" in cmd


def is_agent(cmd: str, profile: str) -> bool:
    return cmd.rstrip().endswith("goose acp") if profile == "goose" else "--profile acp" in cmd and "dsh" in cmd


def acp_snapshot(rt_pid: int, profile: str) -> dict[str, Any]:
    """Runtime 名下该 profile 的 ACP 桥（池 worker，各自一个进程组）及组内全部进程。"""
    tree = descendants(rt_pid)
    bridges = [r for r in tree if is_bridge(r[3], profile)]
    pgids = {r[2] for r in bridges}
    return {
        "bridges": [{"pid": r[0], "pgid": r[2]} for r in bridges],
        "group_procs": [{"pid": r[0], "pgid": r[2], "cmd": r[3][:160]} for r in tree if r[2] in pgids],
        "agents": [{"pid": r[0], "pgid": r[2]} for r in tree if is_agent(r[3], profile)],
        "stray_agents": [{"pid": r[0], "pgid": r[2]} for r in tree if is_agent(r[3], profile) and r[2] not in pgids],
    }


def pool_settled(snap: dict[str, Any]) -> bool:
    """池处于 min_idle：恰好 MIN_IDLE 个桥，每个桥的进程组里恰好一个 agent，且没有游离在桥进程组外的 agent。"""
    per_group = {b["pgid"]: 0 for b in snap["bridges"]}
    for a in snap["agents"]:
        if a["pgid"] in per_group:
            per_group[a["pgid"]] += 1
    return len(snap["bridges"]) == MIN_IDLE and all(n == 1 for n in per_group.values()) and not snap["stray_agents"]


def wait_pool(rt_pid: int, profile: str, timeout_s: float = 30) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while True:
        snap = acp_snapshot(rt_pid, profile)
        if pool_settled(snap) or time.monotonic() > deadline:
            return snap
        time.sleep(0.5)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---------------- 身份与 Runtime 调用 ----------------

_tokens: dict[str, tuple[float, str]] = {}


def auth(user: str) -> dict[str, str]:
    at, t = _tokens.get(user, (0.0, ""))
    if time.monotonic() - at > 60:
        t = devkit.user_token(user)
        _tokens[user] = (time.monotonic(), t)
    return {"Authorization": f"Bearer {t}"}


def parse_sse(r: httpx.Response) -> list[dict[str, Any]]:
    return [json.loads(line[5:].strip()) for line in r.iter_lines() if line.startswith("data:")]


def events(rt: httpx.Client, user: str, run_id: str) -> list[dict[str, Any]]:
    r = rt.get(f"/v1/runs/{run_id}/events", headers=auth(user))
    return list(r.json()) if r.status_code == 200 else []


def run_state(rt: httpx.Client, user: str, run_id: str) -> str:
    r = rt.get(f"/v1/runs/{run_id}", headers=auth(user))
    return str(r.json().get("state")) if r.status_code == 200 else f"HTTP {r.status_code}"


def chat(rt: httpx.Client, user: str, agent: str, messages: list[dict[str, Any]], version: str | None = None) -> dict[str, Any]:
    """流式对话：返回 {status, run_id, sse, log, messages, state, seconds}。"""
    body: dict[str, Any] = {"agent_id": agent, "messages": messages, "stream": True}
    if version:
        body["agent_version"] = version
    t0 = time.monotonic()
    with rt.stream("POST", "/v1/messages", json=body, headers=auth(user)) as r:
        if r.status_code != 200:
            r.read()
            return {
                "status": r.status_code,
                "run_id": None,
                "sse": [],
                "log": [],
                "messages": messages,
                "state": None,
                "detail": r.text[:300],
            }
        run_id = r.headers.get("x-run-id", "")
        sse = parse_sse(r)
    return {
        "status": 200,
        "run_id": run_id,
        "sse": sse,
        "log": events(rt, user, run_id),
        "messages": messages,
        "state": run_state(rt, user, run_id),
        "seconds": round(time.monotonic() - t0, 1),
    }


def resume(rt: httpx.Client, user: str, run_id: str, messages: list[dict[str, Any]]) -> tuple[int, list[dict[str, Any]]]:
    with rt.stream("POST", f"/v1/runs/{run_id}/resume", json={"messages": messages}, headers=auth(user)) as r:
        if r.status_code != 200:
            r.read()
            return r.status_code, []
        return 200, parse_sse(r)


def of(log: list[dict[str, Any]], typ: str, **match: Any) -> list[dict[str, Any]]:
    return [e for e in log if e["type"] == typ and all(e["payload"].get(k) == v for k, v in match.items())]


def calls(log: list[dict[str, Any]], tool: str) -> list[dict[str, Any]]:
    return [e["payload"] for e in of(log, "tool.requested", tool=tool)]


def done_of(sse: list[dict[str, Any]]) -> dict[str, Any]:
    return next((e for e in reversed(sse) if e.get("type") == "done"), {})


def answer(sse: list[dict[str, Any]]) -> str:
    return "".join(e.get("delta", "") for e in sse if e.get("type") == "text")


def executed_once(log: list[dict[str, Any]]) -> bool:
    """每个 TOOL_EXECUTED 都对应一个 TOOL_REQUESTED，且每个 call_id 至多执行一次、结果 ok 的调用恰好执行一次。"""
    requested = {e["payload"]["call_id"] for e in of(log, "tool.requested")}
    executed = [e["payload"]["call_id"] for e in of(log, "tool.executed")]
    ok = {e["payload"]["call_id"] for e in of(log, "tool.result", status="ok")}
    return bool(executed) and set(executed) <= requested and len(executed) == len(set(executed)) and ok <= set(executed)


def usage_total(log: list[dict[str, Any]]) -> dict[str, int]:
    tot = {"input_tokens": 0, "output_tokens": 0, "model_responses": 0}
    for p in (e["payload"] for e in of(log, "model.response")):
        u = p.get("usage") or {}
        tot["input_tokens"] += int(u.get("input_tokens") or 0)
        tot["output_tokens"] += int(u.get("output_tokens") or 0)
        tot["model_responses"] += 1
    return tot


def until(desc: str, fn: Callable[[], dict[str, Any]], ok: Callable[[dict[str, Any]], bool], tries: int = 3) -> dict[str, Any]:
    """模型行为有随机性：Run 正常完成但模型没按要求调用工具时重试（平台侧失败不重试）；每次尝试记入 attempts。"""
    attempts: list[dict[str, Any]] = []
    res: dict[str, Any] = {}
    for i in range(tries):
        res = fn()
        attempts.append({"run_id": res.get("run_id"), "state": res.get("state"), "precondition": ok(res)})
        if ok(res) or res.get("state") != "completed":
            break
        print(f"  （{desc}：第 {i + 1} 次模型未按要求调用工具，重试；run={res.get('run_id')}）")
    res["attempts"] = attempts
    return res


def admin(rt: httpx.Client, method: str, path: str, **kw: Any) -> httpx.Response:
    return rt.request(method, f"/admin{path}", headers=auth("carol"), **kw)


def publish(rt: httpx.Client, draft: dict[str, Any]) -> int:
    """以 carol（platform-admin）登记并全量发布一个版本；返回发布的 HTTP 状态码。"""
    r = admin(rt, "POST", "/agents", json=draft)
    if r.status_code != 201:
        print(f"    登记 {draft['agent_id']}@{draft['version']} → HTTP {r.status_code}：{r.text[:300]}")
        return r.status_code
    r = admin(rt, "POST", f"/agents/{draft['agent_id']}/versions/{draft['version']}/publish", json={"eval_passed": True})
    if r.status_code != 200:
        print(f"    发布 {draft['agent_id']}@{draft['version']} → HTTP {r.status_code}：{r.text[:300]}")
    return r.status_code


def diff_keys(a: dict[str, Any], b: dict[str, Any]) -> set[str]:
    return {k for k in set(a) | set(b) if a.get(k) != b.get(k)}


# ---------------- 0. 预检 ----------------


def binary_version(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)  # noqa: S603
    except OSError as e:
        return f"error: {e}"
    return (out.stdout or out.stderr).strip().splitlines()[0].strip() if (out.stdout or out.stderr).strip() else ""


def preflight() -> dict[str, Any]:
    print("== 0. 预检")
    goose = shutil.which(os.environ.get("GOOSE_BIN", "goose")) or ""
    dsh = os.path.expanduser(os.environ.get("DSH_BIN", "~/.local/share/ai-platform/dsh/node_modules/.bin/dsh"))
    versions = {"goose": binary_version([goose, "--version"]) if goose else "missing", "dsh": binary_version([dsh, "--version"])}
    for name, pinned in PINNED.items():
        check(versions[name] == pinned, f"{name} 版本 {versions[name]!r} 等于钉定版本 {pinned}", "preflight")
    for name in PINNED:
        arch = PROBE_ARCHIVE / f"{name}-{versions[name]}.json"
        rec = json.loads(arch.read_text(encoding="utf-8")) if arch.is_file() else {}
        check(
            rec.get("passed") is True and rec.get("version") == versions[name],
            f"{name} {versions[name]} 的真实探针存档 {arch.relative_to(ROOT)} 存在且通过（make probe-real，D98）",
            "preflight",
        )
    live = models = None
    try:
        live = httpx.get(f"{GATEWAY}/health/liveliness", timeout=10).status_code
        r = httpx.get(f"{GATEWAY}/v1/models", headers={"Authorization": f"Bearer {GATEWAY_KEY}"}, timeout=10)
        models = [m["id"] for m in r.json().get("data", [])] if r.status_code == 200 else None
    except httpx.HTTPError as e:
        print(f"    LiteLLM 不可达：{e}")
    check(live == 200 and models is not None and ALIAS in models, f"LiteLLM 存活且登记了别名 {ALIAS}", "preflight")
    litellm_version = binary_version(
        ["docker", "exec", LITELLM_CONTAINER, "python", "-c", "import importlib.metadata as m;print(m.version('litellm'))"]
    )
    inspect = ["docker", "inspect", LITELLM_CONTAINER, "--format", "{{json .Config.Env}}"]
    env = subprocess.run(inspect, capture_output=True, text=True, check=False).stdout  # noqa: S603
    lf_keys = '"LANGFUSE_PUBLIC_KEY=' in env and '"LANGFUSE_SECRET_KEY=' in env
    print(f"  LiteLLM {litellm_version}；Langfuse 回调密钥{'已' if lf_keys else '未'}配置")
    return {"versions": versions, "goose_bin": goose, "dsh_bin": dsh, "litellm_version": litellm_version, "litellm_langfuse_keys": lf_keys}


# ---------------- 环境 ----------------


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
            "MCP_TICKET_URL": f"http://127.0.0.1:{PORTS['mcp']}",
            "POLICY_URL": "http://127.0.0.1:1",
        },
        "runtime": {
            "DATABASE_URL": f"sqlite+aiosqlite:///{TMP}/runtime.db",
            "WORKSPACE_DIR": str(TMP / "ws"),
            "HARNESS": "litellm",
            "POLICY_URL": "http://127.0.0.1:1",
            "CAPABILITY_GATEWAY_URL": f"http://127.0.0.1:{PORTS['gateway']}",
            "TOOL_DISPATCH": "gateway",
            "AUTH_REQUIRED": "true",
            "JWKS_URL": certs,
            "JWT_ISSUER": devkit.REALM,
            "JWT_AUDIENCE": "ai-runtime",
            "HARNESS_LOOP_HIGH_RISK_ALLOWLIST": APPROVE_AGENT,
            "HARNESS_POOL_MIN_IDLE": str(MIN_IDLE),
            "HARNESS_POOL_MAX_WORKERS": "4",
            "MODEL_GATEWAY_URL": GATEWAY,
            "OTEL_EXPORTER_OTLP_ENDPOINT": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"),
        },
    }


# ---------------- 1～4. 切换演练 ----------------


def ticket_step(rt: httpx.Client, label: str, version: str, harness: str) -> dict[str, Any]:
    """不指定版本对话一次（按稳定版选版），核对服务版本与工单检索链路。"""
    print(f"== {label}：v{version}（{harness}）")
    res = until(
        label,
        lambda: chat(rt, "alice", AGENT, [{"role": "user", "content": QUESTION}]),
        lambda r: len(calls(r["log"], "ticket.search")) == 1,
    )
    log, sse = res["log"], res["sse"]
    created = of(log, "run.created")[0]["payload"] if of(log, "run.created") else {}
    searches = calls(log, "ticket.search")
    executed = of(log, "tool.executed", tool="ticket.search")
    results = [e["payload"] for e in of(log, "tool.result") if e["payload"].get("call_id") in {c["call_id"] for c in searches}]
    text = answer(sse)
    usage = usage_total(log)
    sec = label
    check(
        res["status"] == 200 and res["state"] == "completed" and done_of(sse).get("stop_reason") == "end_turn",
        f"{label}：Run completed",
        sec,
    )
    check(
        created.get("agent_version") == version,
        f"{label}：选版落到 v{version}（RUN_CREATED.agent_version={created.get('agent_version')}）",
        sec,
    )
    check(
        len(searches) == 1 and len(executed) == 1 and executed[0]["payload"].get("call_id") == searches[0]["call_id"],
        f"{label}：ticket.search 的 TOOL_EXECUTED 恰好一次（请求 {len(searches)}、执行 {len(executed)}）",
        sec,
    )
    check(bool(results) and all(r.get("status") == "ok" for r in results), f"{label}：ticket.search 结果 ok", sec)
    check(TICKET in text, f"{label}：回答含工单号 {TICKET}（{text[:60]!r}…）", sec)
    check(usage["input_tokens"] > 0 and usage["output_tokens"] > 0, f"{label}：用量 > 0（{usage}）", sec)
    return {
        "label": label,
        "version": version,
        "harness": harness,
        "run_id": res["run_id"],
        "trace_id": done_of(sse).get("trace_id"),
        "state": res["state"],
        "seconds": res.get("seconds"),
        "attempts": res["attempts"],
        "search_requested": len(searches),
        "search_executed": len(executed),
        "answer_excerpt": text[:200],
        "usage": usage,
    }


def second_message(rt: httpx.Client) -> dict[str, Any]:
    """goose：第二条消息——历史里有上一轮问答，口令只出现在第一条用户消息里；回答复述口令说明 shim 用平台历史替换了
    agent 自己的消息（ACP prompt 只带最后一条用户消息）。"""
    print("== 3a. goose 第二条消息（多轮历史经 shim 结构化重放）")
    word = "口令" + secrets.token_hex(3).upper()
    msgs = [
        {"role": "user", "content": f"请记住我的口令：{word}。先不用做别的。"},
        {"role": "assistant", "content": "好的，我记住了。"},
        {"role": "user", "content": QUESTION + "回答最后请原样附上我在第一条消息里告诉你的口令。"},
    ]
    res = until("第二条消息", lambda: chat(rt, "alice", AGENT, msgs, version="2"), lambda r: len(calls(r["log"], "ticket.search")) == 1)
    text, log = answer(res["sse"]), res["log"]
    sec = "goose 第二条消息"
    check(res["state"] == "completed", f"第二条消息：Run completed（{res['state']}）", sec)
    check(
        of(log, "run.created")[0]["payload"].get("agent_version") == "2" if of(log, "run.created") else False,
        "第二条消息：由 v2（goose）执行",
        sec,
    )
    check(TICKET in text, f"第二条消息：回答含工单号 {TICKET}", sec)
    check(word in text, f"第二条消息：回答复述了只在第一条用户消息里的口令 {word}（历史经 shim 重放到模型）", sec)
    check(executed_once(log), "第二条消息：每个工具调用恰好执行一次", sec)
    return {"run_id": res["run_id"], "attempts": res["attempts"], "answer_excerpt": text[:200], "word": word}


def approval(rt: httpx.Client) -> dict[str, Any]:
    print("== 3b. goose 审批续跑（高风险写，D79 白名单 + D88 只有发起人可续跑）")
    system = (
        "你是售后助手。用户要求关闭工单时，直接调用 ticket.close_batch（ids 为工单号列表），不要先询问确认。工具返回后用一句话告知结果。"
    )
    sec = "goose 审批续跑"
    base = {"version": "1", "model_alias": ALIAS, "harness": "goose", "system_prompt": system, "config": {"tools": ["ticket.close_batch"]}}
    st = publish(rt, {"agent_id": "s6-goose-noallow"} | base)
    check(st == 409, f"未在 HARNESS_LOOP_HIGH_RISK_ALLOWLIST 中的 goose Agent 声明高风险写，发布被拒（HTTP {st}）", sec)
    st = publish(rt, {"agent_id": APPROVE_AGENT} | base)
    check(st == 200, f"白名单内的 {APPROVE_AGENT}（goose + ticket.close_batch）发布成功", sec)
    msgs = [{"role": "user", "content": "请关闭工单 T-1003。"}]
    res = until("审批", lambda: chat(rt, "alice", APPROVE_AGENT, msgs), lambda r: any(e.get("type") == "card" for e in r["sse"]), tries=3)
    run_id = res["run_id"]
    card = next((e for e in res["sse"] if e.get("type") == "card"), {})
    payload = card.get("payload", {})
    call_id, phash = payload.get("call_id"), payload.get("params_hash")
    check(
        payload.get("tool") == "ticket.close_batch" and done_of(res["sse"]).get("stop_reason") == "awaiting",
        "挂起：影响预览卡片 + done(awaiting)",
        sec,
    )
    check(run_state(rt, "alice", run_id) == "auth-required", "Run 状态 auth-required", sec)
    body = {"call_id": call_id, "params_hash": phash, "comment": "s6"}
    check(rt.post(f"/v1/runs/{run_id}/approve", json=body, headers=auth("carol")).status_code == 200, "carol 审批通过", sec)
    status, _ = resume(rt, "bob", run_id, msgs)
    check(status == 403, f"第三人 bob 续跑 403（HTTP {status}）", sec)
    status, _ = resume(rt, "carol", run_id, msgs)
    check(status == 403, f"审批人 carol 续跑 403（续跑只允许发起人，HTTP {status}）", sec)
    status, _ = resume(rt, "alice", run_id, [{"role": "user", "content": "请关闭工单 T-1003 和 T-1004。"}])
    check(status == 409, f"发起人篡改初始消息续跑 409（HTTP {status}）", sec)
    status, sse = resume(rt, "alice", run_id, msgs)
    log = events(rt, "alice", run_id)
    executed, executing = of(log, "tool.executed", call_id=call_id), of(log, "tool.executing", call_id=call_id)
    result = next((e["payload"] for e in of(log, "tool.result", call_id=call_id)), {})
    check(status == 200 and done_of(sse).get("stop_reason") == "end_turn", f"alice 续跑：done({done_of(sse).get('stop_reason')})", sec)
    check(run_state(rt, "alice", run_id) == "completed", "续跑后 Run completed", sec)
    check(
        len(executed) == 1 and len(executing) == 1,
        f"TOOL_EXECUTED 只出现一次（{len(executed)}），TOOL_EXECUTING 一次（{len(executing)}）",
        sec,
    )
    check(len(calls(log, "ticket.close_batch")) == 1, "续跑后模型没有重新请求 close_batch（不重新决策）", sec)
    check(result.get("status") == "ok" and "T-1003" in json.dumps(result.get("output"), ensure_ascii=False), "close_batch 结果 ok", sec)
    check(len(of(log, "model.request")) >= 2, "续跑经新 goose 会话（resume 重放）发起了后续模型调用", sec)
    return {
        "run_id": run_id,
        "attempts": res["attempts"],
        "call_id": call_id,
        "tool_executed": len(executed),
        "answer_excerpt": answer(sse)[:200],
    }


def cancel(rt: httpx.Client, rt_pid: int) -> dict[str, Any]:
    print("== 3c. 取消进行中的 goose Run：无残留进程、池回到 min_idle")
    sec = "goose 取消"
    draft = {"agent_id": WRITER_AGENT, "version": "1", "model_alias": ALIAS, "harness": "goose", "system_prompt": "你是写作助手。"}
    check(publish(rt, draft | {"config": {"tools": ["ticket.search"]}}) == 200, f"发布 {WRITER_AGENT}（goose）", sec)
    before = wait_pool(rt_pid, "goose")
    check(pool_settled(before), f"取消前池处于 min_idle（桥 {len(before['bridges'])} 个）", sec)
    out: dict[str, Any] = {"before": before}
    for attempt in range(3):
        body = {
            "agent_id": WRITER_AGENT,
            "input": [{"role": "user", "content": "写一篇约 3000 字的文章，介绍售后工单处理的最佳实践，分十个小节。"}],
        }
        r = rt.post("/v1/runs", json=body, headers=auth("alice"))
        run_id = r.json().get("run_id") if r.status_code == 202 else None
        during: dict[str, Any] = {}
        deadline = time.monotonic() + 90
        while run_id and time.monotonic() < deadline:
            log = events(rt, "alice", run_id)
            during = acp_snapshot(rt_pid, "goose")
            if of(log, "model.request") and run_state(rt, "alice", run_id) == "working" and len(during["bridges"]) > MIN_IDLE:
                break
            time.sleep(0.3)
        else:
            print(f"  （取消：第 {attempt + 1} 次未观察到进行中的 goose 会话，重试）")
            continue
        time.sleep(2)  # 让模型流式输出起来
        during = acp_snapshot(rt_pid, "goose")
        procs = during["group_procs"]
        t0 = time.monotonic()
        check(rt.post(f"/v1/runs/{run_id}/cancel", headers=auth("bob")).status_code == 403, "第三人 bob 取消 403", sec)
        check(rt.post(f"/v1/runs/{run_id}/cancel", headers=auth("alice")).status_code == 200, "发起人 alice 取消 200", sec)
        after = wait_pool(rt_pid, "goose", 20)
        idle = {b["pgid"] for b in after["bridges"]}
        leftover = [p for p in procs if alive(p["pid"]) and p["pgid"] not in idle]
        state = run_state(rt, "alice", run_id)
        check(state == "canceled", f"Run 状态 canceled（{state}）", sec)
        check(
            not leftover,
            f"取消后 {time.monotonic() - t0:.1f}s 内被租用 worker 的进程组全部回收（取消时组内 {len(procs)} 个进程，残留 {leftover}）",
            sec,
        )
        check(pool_settled(after), f"池回到 min_idle={MIN_IDLE}（桥 {len(after['bridges'])} 个，每个一个 goose agent，无游离 agent）", sec)
        out |= {"run_id": run_id, "during": during, "after": after, "leftover": leftover, "state": state}
        return out
    check(False, "取消：三次尝试均未观察到进行中的 goose 会话", sec)
    return out


def snapshot_rows() -> list[dict[str, Any]]:
    con = sqlite3.connect(TMP / "runtime.db")
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT * FROM t_agent_version WHERE agent_id = ? ORDER BY id", (AGENT,)).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


# ---------------- 5. Langfuse ----------------


def find_attrs(node: Any, prefix: str, out: dict[str, str] | None = None) -> dict[str, str]:
    out = {} if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and k.startswith(prefix) and not isinstance(v, dict):
                out[k] = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            else:
                find_attrs(v, prefix, out)
    elif isinstance(node, list):
        for v in node:
            find_attrs(v, prefix, out)
    return out


def lf_traces(run_ids: list[str], timeout_s: float) -> dict[str, list[dict[str, Any]]]:
    """各 Run（Langfuse Session = run_id）的 Run Trace 详情（每次 drive 一条）；ingestion 异步，统一轮询到全部就绪或超时。"""
    got: dict[str, list[dict[str, Any]]] = {}
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for run_id in [r for r in run_ids if r not in got]:
            try:  # Langfuse 忙（摄取积压）时单次请求可能超时：下一轮再查，直到总超时
                r = devkit.langfuse("GET", "/api/public/traces", params={"sessionId": run_id, "limit": 20})
                # 同一 Session 里还有审计 span 的 Trace（audit POST /v1/messages 等）：只取 Run 驱动的 Trace（根 span `run <agent>`）
                data = r.json().get("data", []) if r.status_code == 200 else []
                ids = [t["id"] for t in data if str(t.get("name", "")).startswith("run ")]
                details = [d.json() for d in (devkit.langfuse("GET", f"/api/public/traces/{i}") for i in ids) if d.status_code == 200]
            except httpx.HTTPError:
                continue
            if details and all(any(str(o.get("name", "")).startswith("chat ") for o in d.get("observations", [])) for d in details):
                got[run_id] = details
        if len(got) == len(run_ids):
            break
        time.sleep(5)
    return got


def litellm_generations(run_ids: list[str], timeout_s: float) -> dict[str, list[dict[str, Any]]]:
    """LiteLLM 回调写入的 generation（metadata.requester_metadata.run_id = run_id）。"""
    want, got = set(run_ids), {}
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and set(got) != want:
        page, found = 1, {}
        while page <= 10:
            params = {"type": "GENERATION", "name": "litellm-acompletion", "limit": 100, "page": page, "fromStartTime": STARTED}
            try:
                r = devkit.langfuse("GET", "/api/public/observations", params=params)
            except httpx.HTTPError:
                break
            data = r.json().get("data", []) if r.status_code == 200 else []
            for o in data:
                rid = ((o.get("metadata") or {}).get("requester_metadata") or {}).get("run_id")
                if rid in want:
                    found.setdefault(rid, []).append(o)
            if len(data) < 100:
                break
            page += 1
        got = found
        if set(got) != want:
            time.sleep(5)
    return got


def langfuse_suite(steps: list[dict[str, Any]], extra_runs: list[str], lf_keys: bool) -> dict[str, Any]:
    print("== 5. Langfuse")
    sec = "Langfuse"
    runs = [s["run_id"] for s in steps if s.get("run_id")] + [r for r in extra_runs if r]
    traces = lf_traces(runs, LANGFUSE_TIMEOUT_S)
    out: dict[str, Any] = {"runs": {}}
    for s in steps:
        trs = traces.get(s["run_id"], [])
        harnesses = {find_attrs(t, "ai.harness").get("ai.harness") for t in trs}
        run_ids = {find_attrs(t, "ai.run_id").get("ai.run_id") for t in trs}
        chats = [o for t in trs for o in t.get("observations", []) if str(o.get("name", "")).startswith("chat ")]
        used = [
            o
            for o in chats
            if int((o.get("usageDetails") or {}).get("input", 0) or 0) > 0
            or int(find_attrs(o, "gen_ai.usage.input_tokens").get("gen_ai.usage.input_tokens", "0") or 0) > 0
        ]
        check(bool(trs) and run_ids == {s["run_id"]}, f"{s['label']}：Langfuse 有该 Run 的 Trace（{len(trs)} 条，ai.run_id 一致）", sec)
        check(harnesses == {s["harness"]}, f"{s['label']}：Trace 的 ai.harness = {s['harness']}（{harnesses}）", sec)
        check(bool(chats) and len(used) == len(chats), f"{s['label']}：模型 span {len(chats)} 个，均带用量", sec)
        out["runs"][s["run_id"]] = {"traces": [t.get("id") for t in trs], "chat_spans": len(chats), "harness": sorted(map(str, harnesses))}
    missing = [r for r in extra_runs if r and r not in traces]
    check(not missing, f"goose 加测的 Run 在 Langfuse 中均有 Trace（缺 {missing}）", sec)
    acp = [s for s in steps if s["harness"] in ("goose", "dsh")]
    if not lf_keys:
        print("  （LiteLLM 容器未配置 Langfuse 密钥：跳过网关侧回调检查，见演练记录）")
        out["litellm_callback"] = "skipped: LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY 未注入 LiteLLM 容器"
        return out
    gens = litellm_generations([s["run_id"] for s in acp], LANGFUSE_TIMEOUT_S / 2)
    for s in acp:
        g = gens.get(s["run_id"], [])
        plain = [o for o in g if json.dumps([o.get("input"), o.get("output")], ensure_ascii=False).count("redacted-by-litellm") == 0]
        leaked = [o for o in g if TICKET in json.dumps([o.get("input"), o.get("output")], ensure_ascii=False)]
        check(
            len(g) >= s["usage"]["model_responses"],
            f"{s['label']}：LiteLLM 侧 generation {len(g)} 条带 run_id（Run 内模型调用 {s['usage']['model_responses']} 次）",
            sec,
        )
        check(
            bool(g) and not plain and not leaked,
            f"{s['label']}：LiteLLM 侧只有元数据（输入 / 输出均为 redacted-by-litellm，无工单内容）",
            sec,
        )
        out["runs"][s["run_id"]]["litellm_generations"] = len(g)
    out["litellm_callback"] = "checked"
    return out


# ---------------- 报告 ----------------


def write_reports(result: dict[str, Any]) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    DRILL.parent.mkdir(parents=True, exist_ok=True)
    pre = result.get("preflight", {})
    rows = "\n".join(
        f"| {s['label']} | v{s['version']} | {s['harness']} | `{s['run_id']}` | {s['state']} | {s['search_executed']} | "
        f"{s['usage']['input_tokens']}/{s['usage']['output_tokens']} | {len(s['attempts'])} | {s.get('seconds')} |"
        for s in result.get("steps", [])
    )
    snaps = "\n".join(f"| v{v['version']} | {v['harness']} | {v['harness_version']} | {v['status']} |" for v in result.get("snapshots", []))
    sections: dict[str, list[dict[str, Any]]] = {}
    for c in result.get("checks", []):
        sections.setdefault(c["section"] or "其他", []).append(c)
    detail = "\n".join(
        f"\n### {name}\n\n" + "\n".join(f"- [{'x' if c['passed'] else ' '}] {c['check']}" for c in items)
        for name, items in sections.items()
    )
    cancel = result.get("goose", {}).get("cancel", {})
    passed = sum(c["passed"] for c in result.get("checks", []))
    verdict = "全部通过" if result.get("passed") else f"{len(result.get('failures', []))} 项失败"
    versions = pre.get("versions", {})
    leased = len(cancel.get("during", {}).get("group_procs", []))
    bridges = len(cancel.get("after", {}).get("bridges", []))
    callback = result.get("langfuse", {}).get("litellm_callback")
    md = f"""# S6 harness 切换演练记录

> 由 `scripts/verify_s6_e2e.py`（`make verify-s6`）生成，机器可读版本见 `evals/reports/s6-switch-drill.json`。重新运行会覆盖本文件。

- 时间：{result.get("started_at")} → {result.get("finished_at")}（UTC）
- 结论：**{verdict}**（{passed}/{len(result.get("checks", []))} 项检查）
- 二进制：goose {versions.get("goose")}（钉定 {PINNED["goose"]}）、dsh {versions.get("dsh")}（钉定 {PINNED["dsh"]}）
- LiteLLM {pre.get("litellm_version")}；模型别名 `{ALIAS}`

## 演练目的

验证同一个 Agent（ticket-summary）只改 `harness` 字段即可在 claude-sdk、goose、dsh 之间切换并回切：版本快照除 harness 外完全一致；
平台语义（选版、工具只经平台执行且每次调用恰好执行一次、审批挂起与发起人续跑、取消、用量与 Trace）在三种 harness 下一致；
goose / dsh 的进程（ACP 桥 + agent）按进程组回收，池回到 min_idle。

## 步骤

1. 预检钉定版本（goose {PINNED["goose"]}、dsh {PINNED["dsh"]}）与 LiteLLM；起 mcp-ticket ← 能力网关（OBO）← Runtime（强制鉴权、工具经网关、
   goose / dsh 进程池 min_idle={MIN_IDLE}、max_workers=4）。
2. 发布 v1（claude-sdk）；v2 / v3 = v1 发布请求的深拷贝、只改 harness（goose / dsh），依次全量发布；每次发布后 alice **不指定版本**对话，
   由稳定版选版落到新版本：「{QUESTION}」
3. goose 加测（v2）：第二条消息（多轮历史经 shim 结构化重放）、审批续跑（白名单 D79、只有发起人可续跑 D88）、取消后进程回收。
4. 一键回滚到 v1（v2 / v3 retired），再对话一次——切回 claude-sdk。
5. Langfuse：每个 Run 的 Trace（session=run_id、ai.harness、模型 span 用量）；LiteLLM 侧回调只记元数据（`turn_off_message_logging`）。
6. Runtime 退出后 ACP 桥与 agent 进程全部结束。

复现：`make verify-s6`（需 compose 的 keycloak / litellm / observe profile 与 DeepSeek Key，本机装有钉定版本的 goose / dsh）。

## 结果

| 步骤 | 版本 | harness | run_id | 状态 | ticket.search 执行次数 | 用量 输入/输出 | 尝试次数 | 耗时 s |
|---|---|---|---|---|---|---|---|---|
{rows}

版本快照（Runtime 库 t_agent_version；除下列字段外 system_prompt / config / model_alias / skills_digest 等完全一致）：

| 版本 | harness | harness_version（发布时冻结） | 状态 |
|---|---|---|---|
{snaps}

goose 取消：取消时被租用 worker 进程组内 {leased} 个进程，取消后残留 {cancel.get("leftover")}；
池中桥进程 {bridges} 个（min_idle={MIN_IDLE}）。LiteLLM 侧回调检查：{callback}。

## 检查明细
{detail}

## 发现与说明

{chr(10).join("- " + n for n in result.get("notes", [])) or "- 无"}
"""
    DRILL.write_text(md, encoding="utf-8")


# ---------------- 主流程 ----------------


def main() -> int:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))  # 被终止时同样走 finally，结束子进程
    started = STARTED
    print(f"临时目录 {TMP}")
    pre = preflight()
    result: dict[str, Any] = {
        "started_at": started,
        "alias": ALIAS,
        "pinned": PINNED,
        "preflight": pre,
        "steps": [],
        "goose": {},
        "notes": [],
    }
    seen_acp: set[int] = set()
    e = env_stack()
    try:
        with (
            service("mcp-ticket", "mcp_ticket.main:app", "mcp-servers/ticket/src", PORTS["mcp"], e["mcp"]),
            service("gateway", "capability_gateway.main:app", "services/capability-gateway/src", PORTS["gateway"], e["gateway"]),
            service("runtime", "runtime.main:app", "services/runtime/src", PORTS["runtime"], e["runtime"]) as (runtime, rt_pid),
            httpx.Client(base_url=runtime, timeout=httpx.Timeout(30, read=600)) as rt,
        ):

            def remember_acp() -> None:
                for profile in ("goose", "dsh"):
                    snap = acp_snapshot(rt_pid, profile)
                    seen_acp.update(p["pid"] for p in snap["group_procs"])

            print("== 1. 发布 v1（claude-sdk）")
            v1 = {
                "agent_id": AGENT,
                "version": "1",
                "model_alias": ALIAS,
                "harness": "claude-sdk",
                "system_prompt": SYSTEM,
                "config": {"tools": ["ticket.search", "ticket.get"], "skills": [{"name": "ticket-summary"}]},
            }
            check(publish(rt, v1) == 200, "发布 ticket-summary v1（claude-sdk）", "发布")
            result["steps"].append(ticket_step(rt, "v1 claude-sdk", "1", "claude-sdk"))

            v2 = copy.deepcopy(v1) | {"version": "2", "harness": "goose"}
            check(diff_keys(v1, v2) == {"version", "harness"}, f"v2 由 v1 深拷贝，只改 harness（diff {sorted(diff_keys(v1, v2))}）", "发布")
            check(publish(rt, v2) == 200, "发布 ticket-summary v2（goose）", "发布")
            result["steps"].append(ticket_step(rt, "v2 goose", "2", "goose"))
            remember_acp()
            result["goose"]["second_message"] = second_message(rt)
            result["goose"]["approval"] = approval(rt)
            remember_acp()
            result["goose"]["cancel"] = cancel(rt, rt_pid)
            remember_acp()
            settled = wait_pool(rt_pid, "goose")
            check(pool_settled(settled), f"goose 加测结束后池处于 min_idle（桥 {len(settled['bridges'])} 个）", "goose 取消")

            v3 = copy.deepcopy(v1) | {"version": "3", "harness": "dsh"}
            check(diff_keys(v1, v3) == {"version", "harness"}, f"v3 由 v1 深拷贝，只改 harness（diff {sorted(diff_keys(v1, v3))}）", "发布")
            check(publish(rt, v3) == 200, "发布 ticket-summary v3（dsh）", "发布")
            result["steps"].append(ticket_step(rt, "v3 dsh", "3", "dsh"))
            remember_acp()
            dsh_pool = wait_pool(rt_pid, "dsh")
            check(pool_settled(dsh_pool), f"dsh 步骤结束后池处于 min_idle（桥 {len(dsh_pool['bridges'])} 个）", "v3 dsh")

            print("== 4. 回滚到 v1（切回 claude-sdk）")
            r = admin(rt, "POST", f"/agents/{AGENT}/versions/1/rollback")
            check(
                r.status_code == 200 and sorted(r.json().get("retired", [])) == ["2", "3"],
                f"一键回滚到 v1，v2 / v3 retired（{r.text[:120]}）",
                "回切",
            )
            result["steps"].append(ticket_step(rt, "回切 claude-sdk", "1", "claude-sdk"))

            rows = snapshot_rows()
            stable_cols = {
                k for k in rows[0] if k not in ("id", "version", "harness", "harness_version", "status", "created_at", "rollout_percent")
            }
            same = all({k: r[k] for k in stable_cols} == {k: rows[0][k] for k in stable_cols} for r in rows)
            check(
                [r["harness"] for r in rows] == ["claude-sdk", "goose", "dsh"] and same,
                f"Runtime 库中 v1/v2/v3 快照除 harness（及随之冻结的 harness_version）外一致（比较 {sorted(stable_cols)}）",
                "发布",
            )
            result["snapshots"] = [{k: r[k] for k in ("version", "harness", "harness_version", "status")} for r in rows]
            acp_versions = {r["harness"]: r["harness_version"] for r in rows if r["harness"] in PINNED}
            check(
                acp_versions == PINNED,
                f"版本快照的 harness_version 对 goose / dsh 冻结为二进制版本 {acp_versions}（钉定 {PINNED}；开会话时据此核对）",
                "发布",
            )

            extra = [result["goose"].get(k, {}).get("run_id") for k in ("second_message", "approval", "cancel")]
            result["langfuse"] = langfuse_suite(result["steps"], extra, pre["litellm_langfuse_keys"])
            remember_acp()
    except Exception as exc:  # 演练中断（服务起不来、请求异常等）：记为失败，照常写报告
        traceback.print_exc()
        check(False, f"演练中断：{type(exc).__name__}: {str(exc)[:300]}", "中断")
    finally:
        print("== 6. Runtime 退出后 ACP 进程全部结束")
        deadline = time.monotonic() + 15
        left = [p for p in seen_acp if alive(p)]
        while left and time.monotonic() < deadline:
            time.sleep(0.5)
            left = [p for p in seen_acp if alive(p)]
        check(
            bool(seen_acp) and not left,
            f"Runtime 退出后 15s 内演练中出现过的 {len(seen_acp)} 个 ACP 桥 / agent 进程全部结束（残留 {left}）",
            "进程",
        )
    result |= {
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "checks": checks,
        "failures": failures,
        "passed": not failures,
    }
    result["logs_dir"] = str(TMP)
    write_reports(result)
    print(f"  报告：{REPORT.relative_to(ROOT)}；演练记录：{DRILL.relative_to(ROOT)}；日志：{TMP}")
    print("S6 端到端验证：" + ("全部通过" if not failures else f"{len(failures)} 项失败"))
    for f in failures:
        print(f"  - {f}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())

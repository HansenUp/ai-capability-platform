#!/usr/bin/env python3
"""S7 本机出口验证（Run 持久化执行：队列、租约、接管、input-required、跨节点取消）。

前置：compose 已启动 mysql（3316）、redis（6379）、keycloak（8180）。本脚本以子进程启动：Runtime API×2（RUNTIME_ROLE=api，
18290 / 18291）、Worker×2（``python -m runtime.worker``），共享专用 MySQL 库 ``ai_runtime_s7_e2e_test``、Redis 逻辑库 15 与
WORKSPACE_DIR；mock harness（``MOCK_TURN_DELAY_S`` 每轮延迟，便于在执行中注入故障）、强制鉴权（Keycloak 开发用户）、
工具经能力网关分发——网关由本脚本内的桩服务（18292）充当，可让指定写工具"卡住"以便在 TOOL_EXECUTING 期间杀掉 Worker。

按 S7 方案 §10 逐项验证：
 ① A 节点创建、B 节点查询，分页与 session 过滤，越权 403 / 其他租户 404；
 ② 审批跨节点：W1 执行至挂起 → B 批准 → 停 W1 → W2 自动续跑完成（无需 /resume），epoch 递增；
 ③ kill -9 持租约 Worker，60 s 内由另一 Worker 完成（报实际耗时）；
 ④ 写工具 EXECUTING 期间 kill -9 → 接管者转对账（input-required）→ /input 对账 → 完成，每个幂等键只有一条 TOOL_EXECUTED、下游只执行一次；
 ⑤ 同一 Run 重复投递只有一个租约持有者（epoch 1），队列消息只含 run_id 与 reason；
 ⑥ request_user_input → /input 完成；非发起人 403、schema 不符 422；
 ⑦ 跨节点取消（B 节点取消 Worker 上执行的 Run）2 s 内生效（报实际耗时）；
 ⑧ Redis 清空（只 FLUSHDB 本脚本专用的逻辑库 15，不动 compose 其他服务在用的 db 0）后 Run 仍完成；双写者考验：对持租约
   Worker ``kill -STOP`` 持续 LEASE_TTL+5 s，等另一 Worker 抢到租约（epoch 递增、持有者变化）后 ``kill -CONT``——旧持有者
   日志须出现失租隔离（lost），Run 完成且 seq 连续、各类事件条数与基线一致、每个 operation_id 只有一条 TOOL_EXECUTED；
   可选（默认执行，S7_SKIP_MYSQL_PAUSE=1 跳过）：MySQL 暂停 10 s（docker pause / unpause，finally 中必定 unpause）后同样成立；
 ⑨ 三个节点同时对空库启动只迁移一次（独立测试库 ai_runtime_s7_migrate_test 与 Redis 逻辑库 13，结束后删除 / 清空）；
 ⑩ 60 s 有效期令牌（经 Keycloak 管理 API 以 portal-web 为模板临时创建令牌有效期 60 s 的专用客户端，取完令牌即在 finally 中删除，
   不改共享的 portal-web）：
   Worker 转 input-required{kind:credential}，用仍将过期的令牌刷新 422，用新令牌刷新后完成，终态删凭据；
 ⑪ 预算超限（budget.max_wall_s）→ Run failed，RUN_ERROR BUDGET_EXCEEDED。

用法：.venv/bin/python scripts/verify_s7_e2e.py     退出码 0 表示全部通过。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

for _k in ("NO_PROXY", "no_proxy"):  # 本机若配置了 HTTP(S)_PROXY，访问 localhost 时绕过
    os.environ[_k] = ",".join(x for x in (os.environ.get(_k, ""), "localhost,127.0.0.1") if x)

import httpx  # noqa: E402
import redis  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TMP = Path(tempfile.mkdtemp(prefix="s7-e2e-"))
PORTS = {"api_a": 18290, "api_b": 18291, "gateway": 18292}
MYSQL = os.environ.get("S7_MYSQL_URL", "mysql+asyncmy://root:root@localhost:3316")
DB_NAME = "ai_runtime_s7_e2e_test"
MIGRATE_DB = "ai_runtime_s7_migrate_test"
REDIS_URL = os.environ.get("S7_REDIS_URL", "redis://localhost:6379/15")  # 本脚本专用逻辑库：允许 FLUSHDB
REDIS_MIGRATE_URL = os.environ.get("S7_REDIS_MIGRATE_URL", "redis://localhost:6379/13")  # ⑨ 专用逻辑库
MYSQL_CONTAINER = os.environ.get("S7_MYSQL_CONTAINER", "compose-dev-mysql-1")
TURN_DELAY_S = "3"  # mock harness 每轮延迟
LEASE_TTL_S = 15.0  # 子进程使用默认 LEASE_TTL_S（runtime_env 不覆盖）
STREAM = "runtime:runs"
UID = devkit.USER_IDS

failures: list[str] = []
timings: dict[str, float] = {}


def check(ok: bool, what: str) -> bool:
    print(f"  [{'通过' if ok else '失败'}] {what}")
    if not ok:
        failures.append(what)
    return ok


def subject(user: str) -> str:
    return f"human:{UID[user]}"


# ---------------- MySQL / Redis ----------------


def _sql(query: str, params: dict[str, Any] | None = None, db: str | None = DB_NAME) -> list[tuple[Any, ...]]:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    async def run() -> list[tuple[Any, ...]]:
        eng = create_async_engine(f"{MYSQL}/{db}" if db else MYSQL, poolclass=NullPool)
        try:
            async with eng.begin() as conn:
                res = await conn.execute(text(query), params or {})
                return [tuple(r) for r in res.fetchall()] if res.returns_rows else []
        finally:
            await eng.dispose()

    return asyncio.run(run())


def reset_db(name: str) -> None:
    assert re.fullmatch(r"[a-z0-9_]+_test", name)
    _sql(f"DROP DATABASE IF EXISTS `{name}`", db=None)
    _sql(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4", db=None)


def drop_db(name: str) -> None:
    assert re.fullmatch(r"[a-z0-9_]+_test", name)
    _sql(f"DROP DATABASE IF EXISTS `{name}`", db=None)


def lease_row(run_id: str) -> tuple[str, str | None, int, int]:
    rows = _sql("SELECT state, lease_owner, lease_epoch, last_seq FROM t_run WHERE run_id = :r", {"r": run_id})
    state, owner, epoch, seq = rows[0]
    return str(state), owner, int(epoch), int(seq)


def rdb(url: str = REDIS_URL) -> redis.Redis:
    return redis.Redis.from_url(url, decode_responses=True)


def flush_dedicated(url: str) -> None:
    """只清本脚本专用的逻辑库（db 13 / 15），绝不 FLUSHALL：compose 的 Langfuse / LiteLLM 用着 db 0。"""
    db_index = int(urlparse(url).path.lstrip("/") or 0)
    assert db_index in (13, 15), f"拒绝清空 Redis 逻辑库 {db_index}"
    r = rdb(url)
    r.flushdb()
    r.close()


# ---------------- 能力网关桩 ----------------


class StubGateway:
    """能力网关桩：/capabilities 返回本地工具注册表中的规格（风险等级等），/invoke 记录调用并返回模拟结果；
    hang 中的写工具（非 dry_run）阻塞到 release，模拟下游卡住。"""

    def __init__(self, port: int) -> None:
        from runtime.assemble.assembler import LOCAL_TOOLS

        self.tools = LOCAL_TOOLS
        self.invokes: list[dict[str, Any]] = []
        self.hang: set[str] = set()
        self.release = threading.Event()
        self.lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return None

            def _json(self, code: int, body: Any) -> None:
                data = json.dumps(body).encode()
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass  # 调用方（被杀的 Worker）已断开

            def do_GET(self) -> None:  # noqa: N802
                u = urlparse(self.path)
                if u.path == "/healthz":
                    return self._json(200, {"status": "ok"})
                if u.path == "/capabilities":
                    names = [n for n in parse_qs(u.query).get("names", [""])[0].split(",") if n]
                    specs = [stub.tools[n].model_dump(mode="json") for n in names if n in stub.tools]
                    return self._json(200, {"tools": specs})
                return self._json(404, {"detail": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                if urlparse(self.path).path != "/invoke":
                    return self._json(404, {"detail": "not found"})
                with stub.lock:
                    stub.invokes.append(body)
                tool, args, dry = body.get("tool"), body.get("input") or {}, bool(body.get("dry_run"))
                if tool in stub.hang and not dry:
                    stub.release.wait(120)
                if tool == "ticket.close_batch":
                    ids = args.get("ids", [])
                    out: Any = {"impact_preview": {"tickets": len(ids)}} if dry else {"closed": ids}
                elif tool == "mail.send":
                    out = {"sent": True, "to": args.get("to")}
                else:
                    out = {"echo": args}
                return self._json(200, {"ok": True, "output": out})

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def executions(self, run_id: str) -> list[dict[str, Any]]:
        with self.lock:
            return [b for b in self.invokes if b.get("run_id") == run_id and not b.get("dry_run")]


# ---------------- 子进程 ----------------


class Procs:
    def __init__(self) -> None:
        self.procs: dict[str, subprocess.Popen[bytes]] = {}
        self.logs: dict[str, Path] = {}
        self.seq = 0

    def start(self, name: str, args: list[str], env: dict[str, str]) -> subprocess.Popen[bytes]:
        self.seq += 1
        log_path = TMP / f"{name}-{self.seq}.log"
        log = open(log_path, "wb")  # noqa: SIM115
        p = subprocess.Popen([PY, *args], env=os.environ.copy() | env, stdout=log, stderr=log, cwd=ROOT)  # noqa: S603
        log.close()
        self.procs[name], self.logs[name] = p, log_path
        return p

    def stop(self, name: str, sig: int = signal.SIGTERM) -> None:
        p = self.procs.pop(name, None)
        if p is None or p.poll() is not None:
            return
        p.send_signal(sig)
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=5)

    def stop_all(self) -> None:
        for name in list(self.procs):
            self.stop(name)

    def pid_of(self, name: str) -> int:
        return self.procs[name].pid

    def name_of(self, pid: int) -> str | None:
        return next((n for n, p in self.procs.items() if p.pid == pid), None)

    def log_text(self, name: str) -> str:
        return self.logs[name].read_text(encoding="utf-8", errors="replace")


PROCS = Procs()


def runtime_env(db: str = DB_NAME, redis_url: str = REDIS_URL) -> dict[str, str]:
    certs = f"{devkit.REALM}/protocol/openid-connect/certs"
    return {
        "DATABASE_URL": f"{MYSQL}/{db}",
        "REDIS_URL": redis_url,
        "WORKSPACE_DIR": str(TMP / "ws"),  # 所有节点共享（D118）
        "HARNESS": "mock",
        "MOCK_TURN_DELAY_S": TURN_DELAY_S,
        "POLICY_URL": "http://127.0.0.1:1",
        "CAPABILITY_GATEWAY_URL": f"http://127.0.0.1:{PORTS['gateway']}",
        "TOOL_DISPATCH": "gateway",
        "AUTH_REQUIRED": "true",
        "JWKS_URL": certs,
        "JWT_ISSUER": devkit.REALM,
        "JWT_AUDIENCE": "ai-runtime",
        "OTEL_SDK_DISABLED": "true",
    }


def start_api(name: str, port: int) -> None:
    PROCS.start(
        name,
        ["-m", "uvicorn", "runtime.main:app", "--port", str(port), "--app-dir", str(ROOT / "services/runtime/src")],
        runtime_env() | {"RUNTIME_ROLE": "api", "NODE_ID": name},
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if PROCS.procs[name].poll() is not None:
            break
        time.sleep(0.5)
    raise RuntimeError(f"{name} 未能启动，见 {PROCS.logs[name]}")


def start_worker(name: str, env: dict[str, str] | None = None, wait: bool = True) -> None:
    PROCS.start(name, ["-m", "runtime.worker"], (env or runtime_env()) | {"NODE_ID": name})
    if wait:
        wait_worker_ready(name)


def wait_worker_ready(name: str, timeout_s: float = 120) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if "runtime worker started" in PROCS.log_text(name):
            return
        if PROCS.procs[name].poll() is not None:
            break
        time.sleep(0.3)
    raise RuntimeError(f"{name} 未能启动，见 {PROCS.logs[name]}")


def worker_names() -> list[str]:
    return sorted(n for n in PROCS.procs if n.startswith("w"))


# ---------------- 身份与 API ----------------

_tokens: dict[str, tuple[float, str]] = {}


def tok(user: str) -> str:
    at, t = _tokens.get(user, (0.0, ""))
    if time.monotonic() - at > 60:
        t = devkit.user_token(user)
        _tokens[user] = (time.monotonic(), t)
    return t


def auth(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok(user)}"}


def api(node: str) -> httpx.Client:
    return httpx.Client(base_url=f"http://127.0.0.1:{PORTS[node]}", timeout=30)


A: httpx.Client
B: httpx.Client


def publish(name: str, config: dict[str, Any]) -> None:
    body = {"agent_id": name, "version": "1", "system_prompt": "你是测试助手。", "config": config}
    r = A.post("/admin/agents", json=body, headers=auth("carol"))
    assert r.status_code == 201, r.text
    r = A.post(f"/admin/agents/{name}/versions/1/publish", json={"eval_passed": True}, headers=auth("carol"))
    assert r.status_code == 200, r.text


def create(node: httpx.Client, user: str | dict[str, str], agent: str, content: str, **extra: Any) -> str:
    headers = auth(user) if isinstance(user, str) else user
    r = node.post("/v1/runs", json={"agent_id": agent, "input": [{"role": "user", "content": content}]} | extra, headers=headers)
    if r.status_code != 202:
        raise RuntimeError(f"创建 Run 失败：HTTP {r.status_code} {r.text[:200]}")
    return str(r.json()["run_id"])


def state_of(node: httpx.Client, user: str, run_id: str) -> str:
    r = node.get(f"/v1/runs/{run_id}", headers=auth(user))
    return str(r.json().get("state")) if r.status_code == 200 else f"HTTP {r.status_code}"


def wait_state(node: httpx.Client, user: str, run_id: str, *states: str, timeout_s: float = 60) -> str:
    deadline = time.monotonic() + timeout_s
    state = ""
    while time.monotonic() < deadline:
        state = state_of(node, user, run_id)
        if state in states:
            return state
        time.sleep(0.2)
    return state


def wait_until(cond: Callable[[], bool], timeout_s: float = 60, every: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(every)
    return False


def events(node: httpx.Client, user: str, run_id: str) -> list[dict[str, Any]]:
    r = node.get(f"/v1/runs/{run_id}/events", headers=auth(user))
    return list(r.json()) if r.status_code == 200 else []


def of(log: list[dict[str, Any]], typ: str, **match: Any) -> list[dict[str, Any]]:
    return [e for e in log if e["type"] == typ and all(e["payload"].get(k) == v for k, v in match.items())]


def seq_ok(log: list[dict[str, Any]]) -> bool:
    seqs = [e["seq"] for e in log]
    return bool(seqs) and seqs == list(range(1, len(seqs) + 1))


def wait_holder(run_id: str, timeout_s: float = 30) -> tuple[str | None, int]:
    """等到 Run 处于 working 且有租约持有者，返回（持有者所在的 Worker 名，epoch）。"""
    found: list[tuple[str | None, int]] = []

    def held() -> bool:
        state, owner, epoch, _ = lease_row(run_id)
        if state == "working" and owner:
            found.append((PROCS.name_of(int(owner.split(":")[1])), epoch))
            return True
        return False

    wait_until(held, timeout_s, 0.1)
    return found[-1] if found else (None, 0)


ECHO = '!tool echo {"text": "s7"}'
ASK_SCHEMA = {"type": "object", "properties": {"id": {"type": "string", "minLength": 2}}, "required": ["id"], "additionalProperties": False}


# ---------------- ①～⑪ ----------------


def s1_cross_node_query() -> None:
    print("== ① A 节点创建、B 节点查询；分页与 session 过滤；越权 403 / 404")
    sess_a, sess_b = f"s7-a-{uuid.uuid4().hex[:6]}", f"s7-b-{uuid.uuid4().hex[:6]}"
    ids = [create(A, "alice", "s7-basic", f"你好 {i}", session_id=sess_a if i < 2 else sess_b) for i in range(3)]
    check(all(B.get(f"/v1/runs/{r}", headers=auth("alice")).status_code == 200 for r in ids), "A 创建的 3 个 Run 在 B 节点均可查询")
    states = [wait_state(B, "alice", r, "completed") for r in ids]
    check(states == ["completed"] * 3, f"3 个 Run 均由 Worker 执行完成（{states}）")
    p1 = B.get("/v1/runs", params={"session_id": sess_a, "limit": 1}, headers=auth("alice")).json()
    p2 = B.get("/v1/runs", params={"session_id": sess_a, "limit": 1, "cursor": p1.get("next_cursor") or ""}, headers=auth("alice")).json()
    got = [x["run_id"] for x in p1.get("items", []) + p2.get("items", [])]
    check(got == [ids[1], ids[0]] and p2.get("next_cursor") is None, f"键集分页（limit=1）两页取全 session {sess_a} 的 2 个 Run，最新在前")
    only_b = [x["run_id"] for x in B.get("/v1/runs", params={"session_id": sess_b}, headers=auth("alice")).json()["items"]]
    check(only_b == [ids[2]], "session 过滤只返回该会话的 Run")
    check(B.get(f"/v1/runs/{ids[0]}", headers=auth("bob")).status_code == 403, "同租户第三人 bob 读 alice 的 Run 403")
    check(B.get(f"/v1/runs/{ids[0]}/stream", headers=auth("bob")).status_code == 403, "bob 尾随 alice 的 Run 事件流 403")
    check(
        B.get("/v1/runs", params={"subject": subject("alice")}, headers=auth("bob")).status_code == 403, "非管理员按他人 subject 列表 403"
    )
    bob_list = [x["run_id"] for x in B.get("/v1/runs", params={"limit": 100}, headers=auth("bob")).json()["items"]]
    check(not set(ids) & set(bob_list), "bob 的列表里没有 alice 的 Run（只返回本人）")
    check(B.get(f"/v1/runs/{ids[0]}", headers=auth("dave")).status_code == 404, "其他租户 dave 读 Run 404（不泄露存在性）")
    dave_list = B.get("/v1/runs", params={"limit": 100}, headers=auth("dave")).json()["items"]
    check(not any(x["run_id"] in ids for x in dave_list), "其他租户 dave 的列表看不到 alice 的 Run")
    admin_page = B.get("/v1/runs", params={"subject": subject("alice"), "limit": 100}, headers=auth("carol")).json()
    admin = [x["run_id"] for x in admin_page["items"]]
    check(set(ids) <= set(admin), "管理员 carol 可按 subject 查看本租户 alice 的全部 Run")


def s2_approval_across_nodes() -> None:
    print("== ② 审批跨节点：W1 挂起 → B 批准 → 停 W1 → W2 自动续跑完成")
    PROCS.stop("w2")  # 只留 W1，确保挂起前由 W1 执行
    run_id = create(A, "alice", "s7-approve", '!tool ticket.close_batch {"ids": ["T-1", "T-2"]}')
    state = wait_state(A, "alice", run_id, "auth-required", timeout_s=60)
    _, owner, epoch1, _ = lease_row(run_id)
    check(state == "auth-required" and owner is None and epoch1 == 1, f"W1 执行至挂起 auth-required 并释放租约（epoch {epoch1}）")
    start_worker("w2")
    PROCS.stop("w1")
    card = next((e["payload"] for e in of(events(A, "alice", run_id), "tool.decision")), {})
    body = {"call_id": card.get("call_id"), "params_hash": card.get("params_hash")}
    r = B.post(f"/v1/runs/{run_id}/approve", json=body, headers=auth("bob"))
    check(r.status_code == 200 and r.json().get("state") == "working", f"bob 在 B 节点批准（HTTP {r.status_code}）")
    state = wait_state(A, "alice", run_id, "completed", "failed", timeout_s=60)
    _, owner, epoch2, _ = lease_row(run_id)
    log = events(A, "alice", run_id)
    check(state == "completed", f"无需 /resume：W2 以发起人凭据自动续跑完成（{state}）")
    check(epoch2 == epoch1 + 1 and owner is None, f"续跑抢到新租约，epoch {epoch1} → {epoch2}")
    executing = of(log, "tool.executing")
    no_foreign = all("credential_subject" not in e["payload"] for e in executing)
    check(
        len(of(log, "tool.executed")) == 1 and len(STUB.executions(run_id)) == 1 and no_foreign,
        "close_batch 下游只执行一次、TOOL_EXECUTED 一条（发起人令牌，不记 credential_subject）",
    )
    check(seq_ok(log), "事件 seq 连续")
    start_worker("w1")


def s3_kill_holder() -> None:
    print("== ③ kill -9 持租约 Worker，60 s 内接管完成")
    run_id = create(A, "alice", "s7-basic", ECHO)
    victim, epoch = wait_holder(run_id)
    if not check(victim is not None, f"Run 由 Worker {victim} 持租约执行中（epoch {epoch}）"):
        return
    time.sleep(1)
    os.kill(PROCS.pid_of(victim), signal.SIGKILL)
    t0 = time.monotonic()
    PROCS.procs.pop(victim)
    state = wait_state(B, "alice", run_id, "completed", "failed", timeout_s=90)
    took = time.monotonic() - t0
    timings["③ kill -9 接管完成"] = took
    _, owner, epoch2, _ = lease_row(run_id)
    log = events(B, "alice", run_id)
    check(state == "completed" and took <= 60, f"kill -9 {victim} 后 {took:.1f}s 由另一 Worker 完成（{state}，要求 ≤ 60 s）")
    check(epoch2 > epoch and owner is None and seq_ok(log), f"接管 epoch {epoch} → {epoch2}，事件 seq 连续")
    start_worker(victim)


def s4_killed_while_executing() -> None:
    print("== ④ 写工具 EXECUTING 期间 kill -9 → 对账 → /input → 完成")
    STUB.release.clear()
    STUB.hang.add("mail.send")
    try:
        run_id = create(A, "alice", "s7-write", '!tool mail.send {"to": "ops@example.com"}')
        if not check(wait_until(lambda: bool(STUB.executions(run_id)), 30), "Worker 已写下 TOOL_EXECUTING 并调用下游（下游卡住）"):
            return
        _, owner, epoch, _ = lease_row(run_id)
        victim = PROCS.name_of(int(owner.split(":")[1])) if owner else None
        if not check(victim is not None, f"持租约的 Worker：{victim}（epoch {epoch}）"):
            return
        os.kill(PROCS.pid_of(victim), signal.SIGKILL)
        PROCS.procs.pop(victim)
    finally:
        STUB.hang.discard("mail.send")
        STUB.release.set()
    state = wait_state(B, "alice", run_id, "input-required", "completed", "failed", timeout_s=90)
    log = events(B, "alice", run_id)
    err = next((e["payload"] for e in of(log, "run.error", code="UNKNOWN_TOOL_RESULT")), {})
    check(state == "input-required" and bool(err), f"接管者不重试，转对账 input-required（{state}，UNKNOWN_TOOL_RESULT）")
    check(len(STUB.executions(run_id)) == 1, f"下游只被调用一次（{len(STUB.executions(run_id))}）")
    body = {"kind": "reconcile", "call_id": err.get("call_id"), "action": "done", "value": {"sent": True}}
    check(B.post(f"/v1/runs/{run_id}/input", json=body, headers=auth("bob")).status_code == 403, "非发起人对账 403")
    r = B.post(f"/v1/runs/{run_id}/input", json=body, headers=auth("alice"))
    check(r.status_code == 200, f"发起人经 /input 对账 done（HTTP {r.status_code}）")
    state = wait_state(B, "alice", run_id, "completed", "failed", timeout_s=60)
    log = events(B, "alice", run_id)
    executed = of(log, "tool.executed")
    keys = [e["idempotency_key"] for e in executed]
    check(state == "completed", f"对账后入队续跑完成（{state}）")
    check(
        keys == [err.get("operation_id")] and len(STUB.executions(run_id)) == 1,
        f"每个幂等键只有一条 TOOL_EXECUTED（{len(keys)} 条），下游只执行一次",
    )
    check(seq_ok(log), "事件 seq 连续")
    start_worker(victim)


def s5_duplicate_delivery() -> None:
    print("== ⑤ 重复投递只有一个租约持有者")
    r = rdb()
    run_id = create(A, "alice", "s7-basic", ECHO)
    for _ in range(3):
        r.xadd(STREAM, {"run_id": run_id, "reason": "duplicate"})
    state = wait_state(A, "alice", run_id, "completed", "failed", timeout_s=60)
    _, owner, epoch, _ = lease_row(run_id)
    log = events(A, "alice", run_id)
    check(state == "completed" and epoch == 1 and owner is None, f"4 次投递、两个 Worker：只抢到一次租约（epoch {epoch}）并完成")
    check(seq_ok(log) and len(of(log, "run.done")) == 1, "事件 seq 连续、只有一条 run.done（无重复执行）")
    check(wait_until(lambda: int(r.xpending(STREAM, "workers")["pending"]) == 0, 15), "重复消息均被 ACK（无挂起消息）")
    fields = {k for _, f in r.xrange(STREAM) for k in f}
    check(fields <= {"run_id", "reason"}, f"队列消息只含 run_id 与 reason（{sorted(fields)}），不含内容与令牌")
    r.close()


def s6_user_input() -> None:
    print("== ⑥ request_user_input → /input 完成")
    ask = "!tool request_user_input " + json.dumps({"prompt": "要处理哪个工单？", "schema": ASK_SCHEMA}, ensure_ascii=False)
    run_id = create(A, "alice", "s7-ask", ask)
    state = wait_state(A, "alice", run_id, "input-required", timeout_s=60)
    req = next((e["payload"] for e in of(events(A, "alice", run_id), "input.requested")), {})
    check(state == "input-required" and req.get("kind") == "answer", f"Run 挂起 input-required{{kind:answer}}（{state}）")
    url = f"/v1/runs/{run_id}/input"
    bad = B.post(url, json={"kind": "answer", "call_id": req.get("call_id"), "value": {"id": "T"}}, headers=auth("alice"))
    check(bad.status_code == 422, f"回答不符 schema 422（HTTP {bad.status_code}）")
    other = B.post(url, json={"kind": "answer", "call_id": req.get("call_id"), "value": {"id": "T-1001"}}, headers=auth("bob"))
    check(other.status_code == 403, f"非发起人回答 403（HTTP {other.status_code}）")
    ok = B.post(url, json={"kind": "answer", "call_id": req.get("call_id"), "value": {"id": "T-1001"}}, headers=auth("alice"))
    check(ok.status_code == 200 and ok.json().get("state") == "working", f"发起人回答 200（HTTP {ok.status_code}）")
    state = wait_state(A, "alice", run_id, "completed", "failed", timeout_s=60)
    result = next((e["payload"] for e in of(events(A, "alice", run_id), "tool.result", call_id=req.get("call_id"))), {})
    check(state == "completed" and result.get("output") == {"id": "T-1001"}, f"入队续跑完成，回答作为工具结果交还模型（{state}）")


def s7_cross_node_cancel() -> None:
    print("== ⑦ 跨节点取消 2 s 内生效")
    run_id = create(A, "alice", "s7-basic", ECHO)
    holder, _ = wait_holder(run_id)
    if not check(holder is not None, f"Run 正由 Worker {holder} 执行"):
        return
    t0 = time.monotonic()
    r = B.post(f"/v1/runs/{run_id}/cancel", headers=auth("alice"))
    stopped = wait_until(lambda: lease_row(run_id)[1] is None, 10, 0.05)
    took = time.monotonic() - t0
    timings["⑦ 跨节点取消生效"] = took
    log = events(B, "alice", run_id)
    check(r.status_code == 200 and r.json().get("state") == "canceled", f"B 节点取消 200（HTTP {r.status_code}）")
    check(stopped and took <= 2, f"持有者 {holder} 在 {took:.2f}s 内中断 harness、收尾并释放租约（要求 ≤ 2 s）")
    canceled_at = next((e["seq"] for e in of(log, "run.state_changed", to="canceled")), 10**9)
    check(
        not [e for e in log if e["seq"] > canceled_at and e["type"] == "model.response"] and seq_ok(log),
        "取消后再无模型响应写入，事件 seq 连续",
    )
    check(lease_row(run_id)[0] == "canceled", "Run 终态 canceled")


def _echo_counts(log: list[dict[str, Any]]) -> tuple[int, int, int, int, int, int]:
    """（模型响应, 工具请求, 工具执行中, 工具结果, run.done, 迁入 completed）条数：两个写者同时推进同一 Run 时至少一项会多出来。"""
    completed = of(log, "run.state_changed", to="completed")
    return (
        len(of(log, "model.response")),
        len(of(log, "tool.requested")),
        len(of(log, "tool.executing")),
        len(of(log, "tool.result")),
        len(of(log, "run.done")),
        len(completed),
    )


def _one_executed_per_operation(log: list[dict[str, Any]]) -> bool:
    """每个 operation_id（TOOL_EXECUTING 登记的幂等键）恰好一条 TOOL_EXECUTED。"""
    ops = [e["payload"].get("operation_id") for e in of(log, "tool.executing")]
    keys = [e["idempotency_key"] for e in of(log, "tool.executed")]
    return bool(ops) and all(op and keys.count(op) == 1 for op in ops) and len(keys) == len(ops)


MAIL = '!tool mail.send {"to": "ops@example.com"}'


def s8_redis_flush_and_double_writer() -> None:
    print("== ⑧ Redis 清空后 Run 仍完成；kill -STOP 持租约 Worker 考验双写者")
    base_id = create(A, "alice", "s7-basic", ECHO)
    wait_state(A, "alice", base_id, "completed", timeout_s=60)
    baseline = _echo_counts(events(A, "alice", base_id))
    print(f"  基线（模型响应, 工具请求, 执行中, 工具结果, run.done, →completed）= {baseline}")

    run_id = create(A, "alice", "s7-basic", ECHO)
    holder, _ = wait_holder(run_id)
    flush_dedicated(REDIS_URL)  # 等同于 Redis 被清空 / 重启丢数据（只清本脚本专用的 db 15）
    state = wait_state(B, "alice", run_id, "completed", "failed", timeout_s=90)
    log = events(B, "alice", run_id)
    check(state == "completed", f"执行中（{holder}）清空 Redis：Run 仍完成（{state}）")
    check(seq_ok(log) and _echo_counts(log) == baseline, f"无双写者：seq 连续，各类事件条数与基线一致（{_echo_counts(log)}）")
    after = create(A, "alice", "s7-basic", "清空之后")
    check(wait_state(B, "alice", after, "completed", timeout_s=60) == "completed", "清空后新建 Run：Worker 重建消费组后照常执行完成")

    # ---- 双写者：STOP 持有者 → 另一 Worker 接管 → CONT 旧持有者 ----
    mail_base = create(A, "alice", "s7-write", MAIL)
    wait_state(A, "alice", mail_base, "completed", timeout_s=60)
    mail_baseline = _echo_counts(events(A, "alice", mail_base))
    run_id = create(A, "alice", "s7-write", MAIL)
    holder, epoch = wait_holder(run_id)
    if not check(holder is not None, f"Run 由 Worker {holder} 持租约执行中（epoch {epoch}）"):
        return
    old_owner = lease_row(run_id)[1]
    pid = PROCS.pid_of(holder)
    log_before = PROCS.log_text(holder).count("lost")
    os.kill(pid, signal.SIGSTOP)
    t0 = time.monotonic()
    try:
        time.sleep(LEASE_TTL_S + 5)
        taken = wait_until(lambda: lease_row(run_id)[2] > epoch and lease_row(run_id)[1] != old_owner, 60, 0.2)
        took = time.monotonic() - t0
        _, owner2, epoch2, _ = lease_row(run_id)
    finally:
        os.kill(pid, signal.SIGCONT)
    timings["⑧ STOP 持有者后另一 Worker 抢到租约"] = took
    check(taken, f"{holder} 被 STOP 后 {took:.1f}s 另一 Worker 抢到租约（epoch {epoch} → {epoch2}，持有者 {old_owner} → {owner2}）")
    state = wait_state(B, "alice", run_id, "completed", "failed", "input-required", timeout_s=90)
    isolated = wait_until(lambda: PROCS.log_text(holder).count("lost") > log_before, 30, 0.3)
    log = events(B, "alice", run_id)
    check(state == "completed", f"接管者完成 Run（{state}）")
    check(isolated, f"旧持有者 {holder} 恢复后日志出现失租隔离（LeaseLost / lost）")
    check(
        seq_ok(log) and _echo_counts(log) == mail_baseline,
        f"无双写者：seq 连续，各类事件条数与基线一致（{_echo_counts(log)} vs {mail_baseline}）",
    )
    check(
        _one_executed_per_operation(log) and len(STUB.executions(run_id)) == 1,
        "每个 operation_id 只有一条 TOOL_EXECUTED，下游只执行一次",
    )

    if os.environ.get("S7_SKIP_MYSQL_PAUSE") == "1":
        print("  （S7_SKIP_MYSQL_PAUSE=1：跳过 MySQL 暂停用例）")
        return
    run_id = create(A, "alice", "s7-basic", ECHO)
    holder, epoch = wait_holder(run_id)
    paused_ok = False
    try:
        subprocess.run(["docker", "pause", MYSQL_CONTAINER], check=True, capture_output=True)  # noqa: S603, S607
        paused_ok = True
        time.sleep(10)
    finally:
        if paused_ok:
            subprocess.run(["docker", "unpause", MYSQL_CONTAINER], check=False, capture_output=True)  # noqa: S603, S607
    check(paused_ok, f"MySQL 暂停 10 s（docker pause {MYSQL_CONTAINER}）并已恢复")
    state = wait_state(B, "alice", run_id, "completed", "failed", timeout_s=120)
    _, owner, epoch2, _ = lease_row(run_id)
    log = events(B, "alice", run_id)
    check(state == "completed", f"MySQL 暂停期间执行中的 Run（{holder}，epoch {epoch}）恢复后完成（{state}，epoch {epoch2}）")
    check(
        seq_ok(log) and _echo_counts(log) == baseline and owner is None,
        f"无双写者：seq 连续，模型响应 / 工具结果 / run.done 条数与基线一致（{_echo_counts(log)}）",
    )
    lost = sum(PROCS.log_text(n).count("lost") for n in worker_names())
    print(f"  （Worker 日志中失租 / 隔离记录 {lost} 条；epoch {epoch} → {epoch2}）")


def s9_migrate_once() -> None:
    print("== ⑨ 三个节点同时对空库启动只迁移一次")
    reset_db(MIGRATE_DB)
    flush_dedicated(REDIS_MIGRATE_URL)
    names = ["m1", "m2", "m3"]
    try:
        env = runtime_env(MIGRATE_DB, REDIS_MIGRATE_URL)
        for n in names:
            start_worker(n, env, wait=False)
        ready = []
        for n in names:
            try:
                wait_worker_ready(n)
                ready.append(n)
            except RuntimeError as e:
                print(f"  {e}")
        check(ready == names, f"三个节点同时启动均就绪（{ready}）")
        upgrades = {n: re.findall(r"Running upgrade .*", PROCS.log_text(n)) for n in names}
        migrators = [n for n, lines in upgrades.items() if lines]
        first = [line for lines in upgrades.values() for line in lines if "-> 0001" in line]
        check(len(migrators) == 1 and len(first) == 1, f"只有一个节点执行了迁移（{migrators}，{len(sum(upgrades.values(), []))} 步）")
        rows = _sql("SELECT version_num FROM alembic_version", db=MIGRATE_DB)
        heads = sorted(p.name.split("_")[0] for p in (ROOT / "services/runtime/migrations/versions").glob("0*.py"))
        check(len(rows) == 1 and rows[0][0] == heads[-1], f"alembic_version 只有一行且为最新版本（{rows}）")
    finally:
        for n in names:
            PROCS.stop(n)
        drop_db(MIGRATE_DB)
        flush_dedicated(REDIS_MIGRATE_URL)


@contextmanager
def short_lived_client(template: str = "portal-web", seconds: int = 60) -> Iterator[str]:
    """经 Keycloak 管理 API 以 template 客户端为模板临时创建一个访问令牌有效期为 seconds 的专用客户端（协议映射器与
    客户端作用域照搬，保证令牌声明一致），产出其 clientId；退出时在 finally 中删除。不修改共享的 template 客户端。"""
    r = httpx.post(
        f"{devkit.KEYCLOAK}/realms/master/protocol/openid-connect/token",
        data={"grant_type": "password", "client_id": "admin-cli", "username": "admin", "password": "admin"},
        timeout=10,
    )
    r.raise_for_status()
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    base = f"{devkit.KEYCLOAK}/admin/realms/ai-platform/clients"
    tid = httpx.get(base, params={"clientId": template}, headers=h, timeout=10).json()[0]["id"]
    rep = httpx.get(f"{base}/{tid}", headers=h, timeout=10).json()
    client_id = f"s7-e2e-short-{uuid.uuid4().hex[:8]}"
    rep.pop("id", None)
    rep.pop("secret", None)
    for m in rep.get("protocolMappers") or []:
        m.pop("id", None)
    rep["clientId"] = client_id
    rep["name"] = f"S7 e2e {seconds}s token (temporary)"
    rep.setdefault("attributes", {})["access.token.lifespan"] = str(seconds)
    created = httpx.post(base, json=rep, headers=h, timeout=10)
    created.raise_for_status()
    new_id = created.headers["location"].rstrip("/").rsplit("/", 1)[-1]
    try:
        yield client_id
    finally:
        httpx.delete(f"{base}/{new_id}", headers=h, timeout=10).raise_for_status()
        left = httpx.get(base, params={"clientId": client_id}, headers=h, timeout=10).json()
        if left:
            raise RuntimeError(f"临时 Keycloak 客户端 {client_id} 未能删除，请在管理台手工删除")


def s10_expiring_credential() -> None:
    print("== ⑩ 60 s 有效期令牌：转 credential input-required，刷新后完成")
    with short_lived_client() as short_client:
        short = devkit.user_token("alice", client_id=short_client)
    claims = json.loads(base64.urlsafe_b64decode(short.split(".")[1] + "=="))
    check(claims["exp"] - claims["iat"] == 60, f"取得 60 s 有效期的发起人令牌（exp - iat = {claims['exp'] - claims['iat']}）")
    run_id = create(A, {"Authorization": f"Bearer {short}"}, "s7-basic", "令牌即将过期")
    state = wait_state(A, "alice", run_id, "input-required", "completed", "failed", timeout_s=60)
    req = next((e["payload"] for e in of(events(A, "alice", run_id), "input.requested")), {})
    check(
        state == "input-required" and req.get("kind") == "credential" and req.get("reason") == "expiring",
        f"Worker 发现令牌剩余有效期不足：input-required{{kind:credential, reason:{req.get('reason')}}}",
    )
    url = f"/v1/runs/{run_id}/input"
    still = B.post(url, json={"kind": "credential"}, headers={"Authorization": f"Bearer {short}"})
    check(still.status_code == 422, f"用仍将过期的令牌刷新 422（HTTP {still.status_code}）")
    check(B.post(url, json={"kind": "credential"}, headers=auth("bob")).status_code == 403, "他人令牌刷新 403")
    _tokens.pop("alice", None)
    ok = B.post(url, json={"kind": "credential"}, headers=auth("alice"))
    check(ok.status_code == 200, f"发起人新令牌刷新 200（HTTP {ok.status_code}）")
    state = wait_state(A, "alice", run_id, "completed", "failed", timeout_s=60)
    check(state == "completed", f"刷新后入队续跑完成（{state}）")
    left = _sql("SELECT COUNT(*) FROM t_run_credential WHERE run_id = :r", {"r": run_id})[0][0]
    check(left == 0, "Run 终态后存储的凭据已删除")


def s11_budget() -> None:
    print("== ⑪ 预算超限 → BUDGET_EXCEEDED")
    run_id = create(A, "alice", "s7-budget", ECHO)
    state = wait_state(A, "alice", run_id, "failed", "completed", timeout_s=60)
    err = next((e["payload"] for e in of(events(A, "alice", run_id), "run.error")), {})
    check(
        state == "failed" and err.get("code") == "BUDGET_EXCEEDED" and err.get("budget") == "wall",
        f"墙钟预算 2 s、每轮 {TURN_DELAY_S} s：第二轮前判超限，Run failed（{state}，{err.get('code')}）",
    )


# ---------------- 主流程 ----------------

STUB: StubGateway


def preflight() -> None:
    httpx.get(f"{devkit.REALM}/.well-known/openid-configuration", timeout=5).raise_for_status()
    _sql("SELECT 1", db=None)
    rdb().ping()
    out = subprocess.run(["docker", "inspect", "-f", "{{.State.Status}}", MYSQL_CONTAINER], capture_output=True, text=True)  # noqa: S603, S607
    if out.stdout.strip() != "running":
        raise RuntimeError(f"MySQL 容器 {MYSQL_CONTAINER} 不在运行（{out.stdout.strip() or out.stderr.strip()}）")
    for port in PORTS.values():
        try:
            httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1)
            raise RuntimeError(f"端口 {port} 已被占用（上次运行残留的服务？）")
        except httpx.HTTPError:
            pass


def main() -> int:
    global A, B, STUB
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    print(f"临时目录 {TMP}")
    preflight()
    reset_db(DB_NAME)
    flush_dedicated(REDIS_URL)
    STUB = StubGateway(PORTS["gateway"])
    STUB.thread.start()
    try:
        start_api("api_a", PORTS["api_a"])  # 首个节点启动时迁移空库
        start_api("api_b", PORTS["api_b"])
        start_worker("w1")
        start_worker("w2")
        A, B = api("api_a"), api("api_b")
        publish("s7-basic", {"tools": ["echo"]})
        publish("s7-approve", {"tools": ["ticket.close_batch"]})
        publish("s7-write", {"tools": ["mail.send"]})
        publish("s7-ask", {"tools": ["echo"], "user_input": True})
        publish("s7-budget", {"tools": ["echo"], "budget": {"max_wall_s": 2}})
        for scenario in (
            s1_cross_node_query,
            s2_approval_across_nodes,
            s3_kill_holder,
            s4_killed_while_executing,
            s5_duplicate_delivery,
            s6_user_input,
            s7_cross_node_cancel,
            s8_redis_flush_and_double_writer,
            s10_expiring_credential,
            s11_budget,
            s9_migrate_once,
        ):
            try:
                scenario()
            except Exception as e:  # 单项异常记为失败，继续后续项
                check(False, f"{scenario.__name__} 异常：{type(e).__name__}: {e}")
    finally:
        subprocess.run(["docker", "unpause", MYSQL_CONTAINER], check=False, capture_output=True)  # noqa: S603, S607 —— 兜底
        PROCS.stop_all()
        STUB.server.shutdown()
        try:
            drop_db(DB_NAME)
            drop_db(MIGRATE_DB)
            flush_dedicated(REDIS_URL)
            flush_dedicated(REDIS_MIGRATE_URL)
        except Exception as e:
            print(f"  清理失败：{e}")
    for k, v in timings.items():
        print(f"  耗时 {k}：{v:.2f}s")
    print(f"  日志：{TMP}")
    print("S7 端到端验证：" + ("全部通过" if not failures else f"{len(failures)} 项失败"))
    for f in failures:
        print(f"  - {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

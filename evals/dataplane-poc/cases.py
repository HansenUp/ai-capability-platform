"""数据面 POC 功能用例（ADR-0005 §3 维度 2、3、4）：对三个候选逐项验证，结果写 results/cases.json。

维度 2 身份头与 OBO 透传（六项，Keycloak 真令牌）：
  A1 OBO 令牌原样到达上游、不被网关自身凭据替换（自研：上游收到的是网关代用户换得的 OBO，sub=终端用户、azp=网关客户端）
  A2 客户端自带的身份头（X-AI-Act / X-AI-Dev-*）被剥离或覆盖
  A3 act 保留：上游采信的执行者 = 平台声明的 Agent（X-AI-Act，仅 azp 可信时采信）
  A4 过期令牌被拒绝（不到达工具）
  A5 受众不符被拒绝（自研：客户端无法选择受众，上游收到的令牌 aud 恒为目标 Server，另验上游拒绝错受众令牌）
  A6 网关日志里没有明文令牌（检索本轮用过的用户令牌 / OBO 令牌的载荷段与签名段）
维度 3 策略挂钩：P1 deny（未登记工具、跨租户）不到达上游；P2 策略服务停掉后失败关闭；
  P3（第三方候选）判定端点不可达时失败关闭；P4 实现方式无需 fork（配置 / 原生插件）。
维度 4 可观测：T1 traceparent 透传率（100 次调用，上游收到同一 trace-id）；T2 Langfuse 可查到该网关的 Span 且带
  run_id、call_id、工具名、判定结果；另记流式工具经网关时进度通知是否逐条实时到达（首字节）。

用法：python evals/dataplane-poc/cases.py [--targets agentgateway,contextforge]
（自研候选 selfbuilt 的数据面已于 S9 删除，其结果见 results/ 历史数据；selfbuilt 容器只提供判定端点。）
过期令牌：首次运行时取一组令牌存到 evals/dataplane-poc/.tmp/aged-tokens.json（0600，已被 .gitignore 忽略），
若尚未过期则等待到过期（本机 realm 访问令牌有效期 15 分钟）；`poc.py bench` 开始时会预先生成。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import poc  # noqa: E402

devkit = poc.devkit
UP = "http://127.0.0.1:19100"
AGED = HERE / ".tmp" / "aged-tokens.json"
ALICE = devkit.USER_IDS["alice"]


def claims(token: str) -> dict[str, Any]:
    seg = token.split(".")[1]
    return dict(json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))))


def sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def traceparent() -> tuple[str, str]:
    tid = secrets.token_hex(16)
    return tid, f"00-{tid}-{secrets.token_hex(8)}-01"


class Client:
    """最小 MCP streamable-http 客户端（与 loadgen 同一协议路径），记录每条 SSE 消息的到达时刻。"""

    def __init__(self, target: str, headers: dict[str, str]) -> None:
        self.target = target
        self.url = str(poc.urls()[target])
        self.base = {"Accept": "application/json, text/event-stream"} | headers
        self.sess: dict[str, str] = {}
        self.http = httpx.Client(timeout=30)

    def post(self, msg: dict[str, Any], extra: dict[str, str] | None = None) -> tuple[int, list[tuple[float, dict[str, Any]]], str]:
        t0 = time.perf_counter()
        events: list[tuple[float, dict[str, Any]]] = []
        with self.http.stream("POST", self.url, headers=self.base | self.sess | (extra or {}), json=msg) as r:
            if r.headers.get("mcp-session-id") and msg.get("method") == "initialize":
                self.sess = {"Mcp-Session-Id": r.headers["mcp-session-id"], "Mcp-Protocol-Version": "2025-06-18"}
            if r.headers.get("content-type", "").startswith("text/event-stream"):
                for line in r.iter_lines():
                    if line.startswith("data:"):
                        try:
                            events.append(((time.perf_counter() - t0) * 1000, json.loads(line[5:])))
                        except ValueError:
                            continue
            else:
                body = r.read()
                try:
                    events.append(((time.perf_counter() - t0) * 1000, json.loads(body)))
                except ValueError:
                    pass
                return r.status_code, events, body.decode(errors="replace")[:300]
            return r.status_code, events, ""

    def init(self) -> int:
        msg = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "poc-cases", "version": "1"}},
        }
        status, _, _ = self.post(msg)
        if status == 200:
            self.http.post(self.url, headers=self.base | self.sess, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        return status

    def call(self, tool: str, args: dict[str, Any], extra: dict[str, str] | None = None, progress: bool = False) -> dict[str, Any]:
        params: dict[str, Any] = {"name": tool, "arguments": args}
        if progress:
            params["_meta"] = {"progressToken": "p1"}
        try:
            status, events, raw = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}, extra)
        except httpx.HTTPError as e:
            return {"ok": False, "status": None, "error": type(e).__name__}
        final = next((m for _, m in events if isinstance(m, dict) and ("result" in m or "error" in m)), None)
        ok = status == 200 and final is not None and "result" in final and not final["result"].get("isError")
        err = None if ok else (final.get("error") if final and "error" in final else (final or {}).get("result") or raw or f"HTTP {status}")
        return {
            "ok": ok,
            "status": status,
            "error": json.dumps(err, ensure_ascii=False)[:300] if err else None,
            "progress_ms": [round(t, 1) for t, m in events if isinstance(m, dict) and m.get("method") == "notifications/progress"],
            "final_ms": round(next((t for t, m in events if m is final), 0.0), 1) if final else None,
        }


def probe_rows(pid: str) -> list[dict[str, Any]]:
    return list(httpx.get(f"{UP}/_poc/probes/{pid}", timeout=10).json())


def rejections(since: float) -> list[dict[str, Any]]:
    return list(httpx.get(f"{UP}/_poc/rejections", params={"since": since}, timeout=10).json())


def aged_tokens() -> dict[str, str]:
    """已过期的用户令牌与 OBO 令牌（首次生成后等到过期）。"""
    AGED.parent.mkdir(exist_ok=True)
    if AGED.exists():
        data = json.loads(AGED.read_text())
    else:
        ut = devkit.user_token("alice")
        data = {"user": ut, "obo": poc.obo_token(ut)}
        AGED.write_text(json.dumps(data))
        AGED.chmod(0o600)
    exp = max(claims(data["user"])["exp"], claims(data["obo"])["exp"])
    if time.time() < exp + 2:
        print(f"等待令牌过期：{int(exp + 2 - time.time())} 秒", flush=True)
        time.sleep(exp + 2 - time.time())
    return dict(data)


def compose(*args: str) -> None:
    subprocess.run([*poc.COMPOSE, *args], check=True, capture_output=True)  # noqa: S603


def wait_ok(url: str) -> None:
    poc.wait_http(url, timeout=120)


def headers(target: str, user: str = "alice", **extra: str) -> tuple[dict[str, str], dict[str, str]]:
    """(请求头, 本次用到的令牌)。"""
    h = poc.headers_for(target, user, **extra)
    toks = {k: h[k].split(" ", 1)[1] for k in ("Authorization", "X-Upstream-Authorization") if h.get(k, "").lower().startswith("bearer ")}
    return h, toks


def run_target(target: str, aged: dict[str, str], used_tokens: list[str]) -> dict[str, Any]:
    search, stream, purge = poc.tool_names(target)
    res: dict[str, Any] = {}
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    h, toks = headers(target, **{"X-AI-Run-Id": run_id})
    used_tokens.extend(toks.values())
    presented = toks.get("X-Upstream-Authorization") or toks.get("Authorization", "")
    c = Client(target, h)
    assert c.init() == 200, f"{target} initialize failed"

    # A1 / A2 / A3：同一次带伪造身份头的探针调用
    pid = f"a-{target}-{uuid.uuid4().hex[:8]}"
    forged = {"X-AI-Act": "agent:evil@9", "X-AI-Dev-Sub": "mallory", "X-AI-Dev-Tenant": "evil", "X-AI-Dev-Roles": "platform-admin"}
    r = c.call(search, {"q": "x", "probe": pid}, extra=forged)
    rows = probe_rows(pid)
    rec = rows[0] if rows else {}
    cl = rec.get("claims") or {}
    aud = cl.get("aud") if isinstance(cl.get("aud"), list) else [cl.get("aud")]
    if target == "selfbuilt":
        a1 = bool(rec) and cl.get("sub") == ALICE and cl.get("azp") == "capability-gateway" and rec.get("token_sha256") != sha(presented)
    else:
        a1 = bool(rec) and rec.get("token_sha256") == sha(presented) and cl.get("sub") == ALICE and cl.get("azp") == "capability-gateway"
    a1 = a1 and "mcp-ticket" in aud and not str(cl.get("sub", "")).startswith("service-account")
    res["A1_obo_passthrough"] = {
        "pass": a1,
        "call": r,
        "upstream_claims": cl,
        "presented_equals_upstream": rec.get("token_sha256") == sha(presented),
    }
    xai = rec.get("x_ai_headers") or {}
    a2 = bool(rec) and not any(k.startswith("x-ai-dev-") for k in xai) and xai.get("x-ai-act") != "agent:evil@9"
    res["A2_client_identity_headers_stripped"] = {"pass": a2, "upstream_x_ai_headers": xai}
    a3 = bool(rec) and rec.get("act_trusted") == f"agent:{poc.AGENT}"
    res["A3_act_preserved"] = {"pass": a3, "upstream_act_trusted": rec.get("act_trusted"), "upstream_act_header": rec.get("act_header")}

    # A4 过期令牌
    if not aged:
        res["A4_expired_token_rejected"] = {"pass": False, "skipped": True}
    since = time.time()
    exp_h = dict(h)
    if not aged:
        pass
    elif target == "selfbuilt":
        exp_h["Authorization"] = f"Bearer {aged['user']}"
    elif target == "contextforge":
        exp_h["X-Upstream-Authorization"] = f"Bearer {aged['obo']}"
    else:
        exp_h["Authorization"] = f"Bearer {aged['obo']}"
    pid = f"exp-{target}-{uuid.uuid4().hex[:8]}"
    ce = Client(target, exp_h)
    init_status = ce.init() if aged else 0
    r = (
        ce.call(search, {"q": "x", "probe": pid})
        if init_status == 200
        else {"ok": False, "status": init_status, "error": "initialize rejected"}
    )
    res["A4_expired_token_rejected"] = res.get("A4_expired_token_rejected") or {
        "pass": not r["ok"] and not probe_rows(pid),
        "call": r,
        "init_status": init_status,
        "upstream_rejections": [x["reason"] for x in rejections(since)],
    }

    # A5 受众不符
    since = time.time()
    pid = f"aud-{target}-{uuid.uuid4().hex[:8]}"
    if target == "selfbuilt":
        # 客户端给的是受众为 ai-runtime 的用户令牌：网关换取后上游收到的令牌受众必须是 mcp-ticket（见 A1）；
        # 另验下游确实拒绝错受众的令牌（直连上游呈现用户令牌）
        user_tok = h["Authorization"].split(" ", 1)[1]
        direct = Client("baseline", {"Authorization": f"Bearer {user_tok}"})
        st = direct.init()
        rej = [x["reason"] for x in rejections(since)]
        res["A5_audience_mismatch_rejected"] = {
            "pass": a1 and st == 401 and "audience mismatch" in rej,
            "upstream_aud_from_gateway": aud,
            "direct_wrong_aud_status": st,
            "upstream_rejections": rej,
        }
    else:
        wrong = dict(h)
        user_tok = devkit.user_token("alice")  # aud=ai-runtime，不是 mcp-ticket
        used_tokens.append(user_tok)
        wrong["X-Upstream-Authorization" if target == "contextforge" else "Authorization"] = f"Bearer {user_tok}"
        cw = Client(target, wrong)
        init_status = cw.init()
        r = (
            cw.call(search, {"q": "x", "probe": pid})
            if init_status == 200
            else {"ok": False, "status": init_status, "error": "initialize rejected"}
        )
        res["A5_audience_mismatch_rejected"] = {
            "pass": not r["ok"] and not probe_rows(pid),
            "call": r,
            "init_status": init_status,
            "upstream_rejections": [x["reason"] for x in rejections(since)],
        }

    # P1 deny 不到达上游：未登记工具；跨租户（dave 属租户 t2）
    pid = f"deny-{target}-{uuid.uuid4().hex[:8]}"
    r1 = c.call(purge, {"probe": pid})
    hd, toks_d = headers(target, user="dave")
    used_tokens.extend(toks_d.values())
    cd = Client(target, hd)
    cd.init()
    pid2 = f"deny2-{target}-{uuid.uuid4().hex[:8]}"
    r2 = cd.call(search, {"q": "x", "probe": pid2})
    res["P1_deny_not_forwarded"] = {
        "pass": not r1["ok"] and not r2["ok"] and not probe_rows(pid) and not probe_rows(pid2),
        "unregistered_tool": r1,
        "cross_tenant": r2,
    }

    # P2 策略服务不可达 → 失败关闭
    compose("stop", "policy")
    try:
        pid = f"pdown-{target}-{uuid.uuid4().hex[:8]}"
        r = c.call(search, {"q": "x", "probe": pid})
        res["P2_policy_down_fail_closed"] = {"pass": not r["ok"] and not probe_rows(pid), "call": r}
    finally:
        compose("start", "policy")
        wait_ok("http://127.0.0.1:19083/healthz")

    # P3 第三方候选：判定端点（selfbuilt 的 /authz/mcp-check）不可达 → 失败关闭
    if target != "selfbuilt":
        compose("stop", "selfbuilt")
        try:
            pid = f"hdown-{target}-{uuid.uuid4().hex[:8]}"
            r = c.call(search, {"q": "x", "probe": pid})
            res["P3_hook_endpoint_down_fail_closed"] = {"pass": not r["ok"] and not probe_rows(pid), "call": r}
        finally:
            compose("start", "selfbuilt")
            wait_ok("http://127.0.0.1:19200/healthz")
    res["P4_no_fork"] = {
        "pass": True,
        "how": {
            "selfbuilt": "平台自有代码（capability_gateway/mcp_proxy.py）",
            "agentgateway": "声明式配置 extAuthz(HTTP) + jwtAuth + transformations（agentgateway.yaml），无源码改动",
            "contextforge": "原生插件 TOOL_PRE_INVOKE（contextforge/platform_authz.py，经 plugins.yaml 加载），无源码改动",
        }[target],
    }

    # 流式：进度通知是否逐条实时到达
    r = c.call(stream, {"id": "T-1001"}, progress=True)
    res["S_stream_progress"] = {"ok": r["ok"], "progress_ms": r["progress_ms"], "final_ms": r["final_ms"]}

    # T1 traceparent 透传率
    hits, sent = 0, 100
    for _ in range(sent):
        tid, tp = traceparent()
        pid = f"tp-{target}-{uuid.uuid4().hex[:8]}"
        c.call(search, {"q": "x", "probe": pid}, extra={"traceparent": tp})
        rows = probe_rows(pid)
        got = (rows[0].get("traceparent") or "") if rows else ""
        hits += int(got.split("-")[1:2] == [tid])
    res["T1_traceparent_propagation"] = {"pass": hits == sent, "rate": hits / sent, "sent": sent}

    # T2 Langfuse 中该网关的 Span
    tid, tp = traceparent()
    call_id = f"call-{uuid.uuid4().hex[:8]}"
    c.call(search, {"q": "x"}, extra={"traceparent": tp, "X-AI-Call-Id": call_id})
    res["T2_langfuse_span"] = langfuse_check(tid, run_id, call_id, target)
    return res


def langfuse_check(tid: str, run_id: str, call_id: str, target: str, timeout: float = 120) -> dict[str, Any]:
    deadline = time.time() + timeout
    obs: list[dict[str, Any]] = []
    while time.time() < deadline:
        r = devkit.langfuse("GET", f"/api/public/traces/{tid}")
        if r.status_code == 200:
            obs = r.json().get("observations") or []
            if obs:
                time.sleep(5)  # 等同一 Trace 的其余 Span 落库
                obs = devkit.langfuse("GET", f"/api/public/traces/{tid}").json().get("observations") or []
                break
        time.sleep(3)
    text = json.dumps(obs, ensure_ascii=False)
    found = {
        "trace_found": bool(obs),
        "observations": [o.get("name") for o in obs],
        "run_id": run_id in text,
        "call_id": call_id in text,
        "tool_name": "ticket.search" in text or "ticket-search" in text,
        "decision": any(k in text for k in ("ai.authz.decision", "platform_authz_decision", '"allow"')),
    }
    found["pass"] = all(found[k] for k in ("trace_found", "run_id", "call_id", "tool_name", "decision"))
    return found


def token_log_check(targets: list[str], used: list[str]) -> dict[str, Any]:
    """A6：在网关容器日志里检索本轮用过的令牌的载荷段与签名段（JWT 头段各令牌相同，不作为判据）。"""
    needles = {seg for t in used for seg in t.split(".")[1:] if len(seg) > 20}
    out = {}
    for t in targets:
        r = subprocess.run(["docker", "logs", f"{poc.PROJECT}-{t}-1"], capture_output=True, text=True, check=False)  # noqa: S603,S607
        logs = r.stdout + r.stderr
        leaks = sum(1 for n in needles if n in logs)
        out[t] = {"pass": leaks == 0, "leaked_segments": leaks, "segments_checked": len(needles), "log_bytes": len(logs)}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", default="agentgateway,contextforge")
    ap.add_argument("--no-wait", action="store_true", help="调试用：令牌未过期时跳过 A4，不等待")
    args = ap.parse_args()
    targets = args.targets.split(",")
    if "selfbuilt" in targets:
        raise SystemExit("selfbuilt 数据面（mcp_proxy）已于 S9 删除（D153），只保留判定端点；历史结果见 results/")
    aged = {} if args.no_wait else aged_tokens()
    poc.trace_sink(forward=True)
    used: list[str] = []
    results: dict[str, Any] = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "targets": {}}
    for t in targets:
        print(f"== cases {t} ==", flush=True)
        try:
            results["targets"][t] = run_target(t, aged, used)
        except Exception as e:  # noqa: BLE001 —— 记录候选无法完成的原因，继续其余候选
            traceback.print_exc()
            results["targets"][t] = {"error": f"{type(e).__name__}: {e}"}
    for t, leak in token_log_check(targets, used).items():
        if "error" not in results["targets"].get(t, {}):
            results["targets"][t]["A6_no_plaintext_token_in_logs"] = leak
    for r in results["targets"].values():
        if "error" in r:
            continue
        r["summary"] = {
            "dim2_identity": all(r[k]["pass"] for k in r if k.startswith("A")),
            "dim3_policy_hook": all(r[k]["pass"] for k in r if k.startswith("P")),
            "dim4_observability": r["T1_traceparent_propagation"]["pass"] and r["T2_langfuse_span"]["pass"],
        }
    poc.RESULTS.mkdir(exist_ok=True)
    (poc.RESULTS / "cases.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(json.dumps({t: r.get("summary", r.get("error")) for t, r in results["targets"].items()}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

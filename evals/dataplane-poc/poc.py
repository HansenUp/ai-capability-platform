"""数据面 POC 编排（ADR-0005）：构建镜像、起停 POC 容器、准备 ContextForge、取令牌、按轮次交错压测、汇总报告。

前置：deploy/compose-dev 的 keycloak 与 observe profile（otel-collector、langfuse）已在运行；已拉取
ghcr.io/agentgateway/agentgateway:v1.6.0 与 ghcr.io/ibm/mcp-context-forge:v1.0.11。

子命令：
    python evals/dataplane-poc/poc.py up                # 构建 POC 镜像并启动（含 ContextForge 网关登记）
    python evals/dataplane-poc/poc.py bench --warmup 60 --duration 300 --rounds 3
    python evals/dataplane-poc/poc.py cases             # 鉴权六项、策略挂钩、traceparent 与 Langfuse（见 cases.py）
    python evals/dataplane-poc/poc.py report            # results/*.json → results/summary.json 与 summary.md
    python evals/dataplane-poc/poc.py down              # 停止并删除 POC 容器、网络与本地构建的镜像

本机 .py 文件是透明加密的（docker 读到的是密文），所以镜像构建上下文由本脚本用 Python 读出明文后打成 tar 经 stdin 交给 docker build。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import statistics
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RESULTS = HERE / "results"
sys.path.insert(0, str(ROOT / "scripts"))
import devkit  # noqa: E402

PROJECT = "dataplane-poc"
COMPOSE = ["docker", "compose", "-p", PROJECT, "-f", str(HERE / "docker-compose.yml")]
BASE_IMAGE = "ghcr.io/ibm/mcp-context-forge:v1.0.11"
AGW_IMAGE = "ghcr.io/agentgateway/agentgateway:v1.6.0"
PY_IMAGE = "ai-platform/dataplane-poc-py:local"
CF_IMAGE = "ai-platform/dataplane-poc-contextforge:local"
CF_SECRET = "poc-contextforge-jwt-secret-0123456789abcdef"  # noqa: S105 —— 与 docker-compose.yml 一致，仅本 POC 容器内有效
AGENT = "poc-agent@1"
# 自研数据面（capability_gateway/mcp_proxy.py）已于 S9 删除（D153）：selfbuilt 容器只保留判定端点 /authz/mcp-check，
# 不再作为压测 / 功能用例目标；results/ 中的 selfbuilt 数据为历史结论（ADR-0005）。
TARGETS = ("baseline", "agentgateway", "contextforge")
URLS = {  # 宿主机访问地址（cases.py 用）
    "baseline": "http://127.0.0.1:19100/mcp",
    "agentgateway": "http://127.0.0.1:19300/mcp",
    "contextforge": None,  # 由 up 时登记的虚拟服务器决定，见 cf_state()
}
# 压测地址：压测器在 POC 网络内的容器里运行，基线与候选走同一网络路径（Docker Desktop 的宿主机端口转发
# 在 50 并发长连接下自身成为瓶颈并出现连接被断开，会把差异淹没，见 README）
NET_URLS = {
    "baseline": "http://upstream:9100/mcp",
    "agentgateway": "http://agentgateway:3000/mcp",
    "contextforge": None,
}
LOADGEN_CPUS = "6"  # 压测器容器的 CPU 上限（10 个进程实际约用 1.5 CPU；限得过紧时压测器自己成为瓶颈）
# 补充对照目标：名字 → (实际目标, 网络内地址覆盖, 网关容器, 额外 loadgen 参数)
EXTRA_TARGETS: dict[str, tuple[str, str | None, str, list[str]]] = {
    "contextforge-notrace": ("contextforge", None, "contextforge", ["--no-traceparent"]),
}
GATEWAY_CONTAINER = {"agentgateway": "agentgateway", "contextforge": "contextforge", "baseline": None}
NO_PROXY_ENV = {"NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1"}
os.environ.update({k: ",".join(x for x in (os.environ.get(k, ""), v) if x) for k, v in NO_PROXY_ENV.items()})


def sh(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, check=False, **kw)  # noqa: S603


# ---------------- 镜像与容器 ----------------
def _add(tar: tarfile.TarFile, src: Path, arc: str) -> None:
    """把文件（.py 经 Python 读出明文）加入构建上下文。"""
    data = src.read_bytes() if src.suffix != ".py" else src.read_text(encoding="utf-8").encode()
    info = tarfile.TarInfo(arc)
    info.size, info.mode, info.mtime = len(data), 0o644, int(time.time())
    tar.addfile(info, io.BytesIO(data))


def _tree(tar: tarfile.TarFile, src: Path, arc: str) -> None:
    for p in sorted(src.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and ".egg-info" not in str(p):
            _add(tar, p, f"{arc}/{p.relative_to(src)}")


def _build(tag: str, dockerfile: str, files: list[tuple[Path, str, bool]]) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        df = dockerfile.encode()
        info = tarfile.TarInfo("Dockerfile")
        info.size = len(df)
        tar.addfile(info, io.BytesIO(df))
        for src, arc, is_tree in files:
            (_tree if is_tree else _add)(tar, src, arc)
    r = subprocess.run(["docker", "build", "-t", tag, "-"], input=buf.getvalue(), capture_output=True, check=False)  # noqa: S603,S607
    if r.returncode != 0:
        raise SystemExit(f"docker build {tag} failed:\n{r.stderr.decode()[-3000:]}")
    print(f"built {tag}")


def build() -> None:
    py_df = f"""FROM {BASE_IMAGE}
COPY gateway /poc/gateway
COPY policy /poc/policy
COPY evals /poc/evals
ENV PYTHONPATH=/poc/gateway:/poc/policy:/poc/evals
WORKDIR /poc/evals
ENTRYPOINT []
"""
    evals = [(HERE / n, f"evals/{n}", False) for n in ("upstream.py", "relay.py", "policy_app.py", "loadgen.py")]
    _build(
        PY_IMAGE,
        py_df,
        [
            (ROOT / "services/capability-gateway/src/capability_gateway", "gateway/capability_gateway", True),
            (ROOT / "services/policy/src/policy_service", "policy/policy_service", True),
            *evals,
        ],
    )
    cf_df = f"""FROM {BASE_IMAGE}
COPY platform_authz /app/plugins/platform_authz
COPY poc-config.yaml /app/plugins/poc-config.yaml
"""
    _build(
        CF_IMAGE,
        cf_df,
        [
            (HERE / "contextforge/platform_authz.py", "platform_authz/plugin.py", False),
            (HERE / "contextforge/__init__.txt", "platform_authz/__init__.py", False),
            (HERE / "contextforge/plugins.yaml", "poc-config.yaml", False),
        ],
    )


def wait_http(url: str, timeout: float = 120, ok: tuple[int, ...] = (200,)) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=3)
            if r.status_code in ok:
                return
            last = f"HTTP {r.status_code}"
        except httpx.HTTPError as e:
            last = type(e).__name__
        time.sleep(1)
    raise SystemExit(f"{url} not ready: {last}")


def up() -> None:
    for img in (BASE_IMAGE, AGW_IMAGE):
        if sh(["docker", "image", "inspect", img]).returncode != 0:
            raise SystemExit(f"缺少镜像 {img}：请先 docker pull {img}")
    build()
    r = sh([*COMPOSE, "up", "-d", "--force-recreate"])
    if r.returncode != 0:
        raise SystemExit(r.stderr)
    wait_http("http://127.0.0.1:19100/healthz")
    wait_http("http://127.0.0.1:19083/healthz")
    wait_http("http://127.0.0.1:19200/healthz")
    wait_http("http://127.0.0.1:19400/health", timeout=240)
    setup_contextforge()
    print("POC 已启动：", json.dumps(urls(), ensure_ascii=False))


def trace_sink(forward: bool) -> None:
    """切换 POC Collector：forward=True 时 Span 转发到 compose-dev（→ Langfuse），否则只计数丢弃（压测用）。"""
    env = os.environ | {"POC_TRACE_EXPORTER": "otlphttp/forward" if forward else "debug"}
    r = subprocess.run([*COMPOSE, "up", "-d", "--force-recreate", "otel"], env=env, capture_output=True, text=True, check=False)  # noqa: S603
    if r.returncode != 0:
        raise SystemExit(r.stderr)
    time.sleep(3)


def down() -> None:
    r = sh([*COMPOSE, "down", "-v", "--remove-orphans"])
    print(r.stdout or r.stderr)
    for img in (PY_IMAGE, CF_IMAGE):
        sh(["docker", "image", "rm", img])
    (RESULTS / ".cf-state.json").unlink(missing_ok=True)


# ---------------- 令牌 ----------------
def obo_token(user_token: str, audience: str = "mcp-ticket") -> str:
    """与 capability-gateway 的 _obo_token 相同的 RFC 8693 换取（网关客户端代表用户换取受众为 MCP Server 的令牌）。"""
    r = httpx.post(
        devkit.TOKEN_URL,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "client_id": "capability-gateway",
            "client_secret": "capability-gateway-dev-secret",
            "subject_token": user_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "audience": audience,
            "scope": f"{audience}-audience",
        },
        timeout=10,
    )
    r.raise_for_status()
    return str(r.json()["access_token"])


def cf_admin_token(minutes: int = 120) -> str:
    """在 ContextForge 容器内用其自带工具签发管理 JWT（只在 POC 容器内有效）。"""
    r = sh(
        [
            "docker",
            "exec",
            f"{PROJECT}-contextforge-1",
            "python3",
            "-m",
            "mcpgateway.utils.create_jwt_token",
            "--username",
            "admin@example.com",
            "--exp",
            str(minutes),
            "--secret",
            CF_SECRET,
        ]
    )
    token = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    if r.returncode != 0 or token.count(".") != 2:
        raise SystemExit(f"create_jwt_token failed: {r.stderr[-1000:]}")
    return token


def cf_state() -> dict[str, Any]:
    p = RESULTS / ".cf-state.json"
    return json.loads(p.read_text()) if p.exists() else {}


def setup_contextforge() -> None:
    """登记上游为 ContextForge 网关（一次性认证发现工具），建虚拟服务器，记录其 MCP 端点与工具名。"""
    admin = cf_admin_token()
    obo = obo_token(devkit.user_token("alice"))
    h = {"Authorization": f"Bearer {admin}"}
    base = "http://127.0.0.1:19400"
    gw = {
        "name": "poc-upstream",
        "url": "http://upstream:9100/mcp",
        "transport": "STREAMABLEHTTP",
        "passthrough_headers": ["X-Upstream-Authorization", "X-AI-Agent", "X-AI-Run-Id", "X-AI-Call-Id", "traceparent"],
        "auth_type": "bearer",
        "auth_token": obo,
        "one_time_auth": True,
    }
    r = httpx.post(f"{base}/gateways", headers=h, json=gw, timeout=60)
    if r.status_code not in (200, 201, 409):
        raise SystemExit(f"register gateway failed: HTTP {r.status_code} {r.text[:500]}")
    tools = httpx.get(f"{base}/tools", headers=h, params={"limit": 0}, timeout=30).json()
    items = tools if isinstance(tools, list) else tools.get("items", tools.get("tools", []))
    mine = [t for t in items if str(t.get("originalName") or t.get("original_name") or "").startswith("ticket.")]
    if not mine:
        raise SystemExit(f"no tools discovered: {json.dumps(items, ensure_ascii=False)[:800]}")
    server = {
        "server": {"name": "poc", "description": "dataplane poc", "associated_tools": [t["id"] for t in mine], "visibility": "public"}
    }
    r = httpx.post(f"{base}/servers", headers=h, json=server, timeout=30)
    if r.status_code not in (200, 201):
        raise SystemExit(f"create server failed: HTTP {r.status_code} {r.text[:500]}")
    sid = r.json()["id"]
    names = {str(t.get("originalName") or t.get("original_name")): str(t["name"]) for t in mine}
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / ".cf-state.json").write_text(json.dumps({"server_id": sid, "tool_names": names}, ensure_ascii=False, indent=2))
    print("ContextForge 工具名：", names)


def urls(in_network: bool = False) -> dict[str, str | None]:
    out = dict(NET_URLS if in_network else URLS)
    st = cf_state()
    if st:
        host = "http://contextforge:4444" if in_network else "http://127.0.0.1:19400"
        out["contextforge"] = f"{host}/servers/{st['server_id']}/mcp"
    return out


def tool_names(target: str) -> tuple[str, str, str]:
    """(普通工具, 流式工具, 未登记工具) 在该目标上的名字。"""
    if target == "contextforge":
        n = cf_state()["tool_names"]
        return n["ticket.search"], n["ticket.get"], n["ticket.purge"]
    return "ticket.search", "ticket.get", "ticket.purge"


def headers_for(target: str, user: str = "alice", **extra: str) -> dict[str, str]:
    """各目标的客户端请求头：自研收终端用户令牌（网关换 OBO）；其余收已换好的 OBO 令牌（数据面只透传）。"""
    ut = devkit.user_token(user)
    ctx = {"X-AI-Agent": AGENT, "X-AI-Run-Id": f"run-{int(time.time())}"} | extra
    obo = obo_token(ut)
    if target == "baseline":  # 直连：客户端自己扮演网关输出的头
        return {"Authorization": f"Bearer {obo}", "X-AI-Act": f"agent:{AGENT}", "X-AI-Run-Id": ctx["X-AI-Run-Id"]}
    if target == "agentgateway":
        return {"Authorization": f"Bearer {obo}"} | ctx
    return {"Authorization": f"Bearer {cf_admin_token()}", "X-Upstream-Authorization": f"Bearer {obo}"} | ctx


# ---------------- 压测 ----------------
class Stats(threading.Thread):
    """docker stats 采样（约每 2 秒一次）：CPU% 与内存（RSS 近似为 cgroup 内存用量）。"""

    def __init__(self, containers: list[str]) -> None:
        super().__init__(daemon=True)
        self.containers = containers
        self.samples: dict[str, list[tuple[float, float]]] = {c: [] for c in containers}
        self.stop = threading.Event()

    def run(self) -> None:
        names = [f"{PROJECT}-{c}-1" for c in self.containers]
        while not self.stop.is_set():
            r = sh(["docker", "stats", "--no-stream", "--format", "{{json .}}", *names])
            for line in r.stdout.splitlines():
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                c = d["Name"].removeprefix(f"{PROJECT}-").removesuffix("-1")
                if c in self.samples:
                    self.samples[c].append((float(d["CPUPerc"].rstrip("%")), _mib(d["MemUsage"].split("/")[0].strip())))

    def summary(self) -> dict[str, Any]:
        out = {}
        for c, s in self.samples.items():
            cpu = [x for x, _ in s]
            mem = [y for _, y in s]
            out[c] = {
                "cpu_pct_mean": statistics.fmean(cpu) if cpu else None,
                "cpu_pct_max": max(cpu) if cpu else None,
                "mem_mib_max": max(mem) if mem else None,
                "samples": len(s),
            }
        return out


def _mib(text: str) -> float:
    units = {"KiB": 1 / 1024, "MiB": 1, "GiB": 1024, "kB": 1 / 1024, "MB": 1, "GB": 1024, "B": 1 / 1024 / 1024}
    for u, f in sorted(units.items(), key=lambda x: -len(x[0])):
        if text.endswith(u):
            return float(text[: -len(u)]) * f
    return float(text)


def bench_one(name: str, rnd: int, args: argparse.Namespace) -> dict[str, Any]:
    target, url_override, gw_container, extra = EXTRA_TARGETS[name] if name in EXTRA_TARGETS else (name, None, GATEWAY_CONTAINER[name], [])
    url = url_override or urls(in_network=True)[target]
    search, stream, _ = tool_names(target)
    env = os.environ | {"LOADGEN_HEADERS": json.dumps(headers_for(target))}
    containers = ["upstream", "policy"] + ([gw_container] if gw_container else [])
    if target in ("agentgateway", "contextforge"):
        containers.append("selfbuilt")  # 判定端点所在容器
    out = RESULTS / f"{name}-r{rnd}.json"
    # 压测器跑在 POC 网络内的容器里（令牌经环境变量传入，不出现在命令行）
    cmd = ["docker", "run", "--rm", "--network", f"{PROJECT}_default", "--cpus", LOADGEN_CPUS, "-e", "LOADGEN_HEADERS"]
    cmd += ["-v", f"{RESULTS}:/out", PY_IMAGE, "python", "loadgen.py", "--url", str(url), "--label", f"{name}-r{rnd}", *extra]
    cmd += ["--concurrency", str(args.concurrency), "--procs", str(args.procs), "--warmup", str(args.warmup)]
    cmd += ["--duration", str(args.duration), "--tool", search, "--stream-tool", stream, "--out", f"/out/{out.name}"]
    stats = Stats(containers)
    timer = threading.Timer(args.warmup + 1, stats.start)  # 只采样测量窗口
    timer.start()
    r = subprocess.run(cmd, env=env, text=True, capture_output=True, check=False)  # noqa: S603
    timer.cancel()
    stats.stop.set()
    if stats.is_alive():
        stats.join(timeout=10)
    if r.returncode != 0 or not out.exists():
        raise SystemExit(f"loadgen {name} failed: {r.stderr[-2000:]}")
    res = json.loads(out.read_text())
    res["resources"] = stats.summary()
    res["gateway_container"] = gw_container
    res["target"], res["round"] = name, rnd
    res["hardware"] = HARDWARE
    res["loadgen_runtime"] = {"where": f"docker container on network {PROJECT}_default", "cpus": LOADGEN_CPUS, "image": PY_IMAGE}
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2))
    print(r.stdout.strip())
    return res


HARDWARE: dict[str, Any] = {}
AGED = HERE / ".tmp" / "aged-tokens.json"  # cases.py 的过期令牌用例使用；.tmp/ 已被 .gitignore 忽略


def prepare_aged_tokens() -> None:
    """预先取一组令牌留到过期（本机 realm 访问令牌 15 分钟），压测结束后 cases.py 即可直接用作过期令牌。"""
    if AGED.exists():
        return
    AGED.parent.mkdir(exist_ok=True)
    ut = devkit.user_token("alice")
    AGED.write_text(json.dumps({"user": ut, "obo": obo_token(ut)}))
    AGED.chmod(0o600)


def bench(args: argparse.Namespace) -> None:
    from loadgen import hardware

    RESULTS.mkdir(exist_ok=True)
    prepare_aged_tokens()
    trace_sink(forward=False)
    HARDWARE.update(hardware())
    HARDWARE["images"] = {"agentgateway": AGW_IMAGE, "contextforge": BASE_IMAGE, "python_runtime": f"{PY_IMAGE} (FROM {BASE_IMAGE})"}
    targets = args.targets.split(",") if args.targets else list(TARGETS)
    for rnd in range(1, args.rounds + 1):
        for t in targets:  # 每轮内交错执行各目标，减少时间漂移对比较的影响
            print(f"== round {rnd} {t} ==", flush=True)
            bench_one(t, rnd, args)


# ---------------- 汇总 ----------------
def _median(values: list[float | None]) -> float | None:
    v = [x for x in values if x is not None]
    return statistics.median(v) if v else None


def report() -> dict[str, Any]:
    runs: dict[str, list[dict[str, Any]]] = {}
    for p in sorted(RESULTS.glob("*-r*.json")):
        d = json.loads(p.read_text())
        runs.setdefault(d["target"], []).append(d)
    agg: dict[str, Any] = {}
    for t, rs in runs.items():

        def m(f: Any, rs: list[dict[str, Any]] = rs) -> float | None:
            return _median([f(r) for r in rs])

        agg[t] = {
            "rounds": len(rs),
            "params": rs[0]["params"],
            "search_p50": m(lambda r: r["latency_ms"]["search"]["p50"]),
            "search_p95": m(lambda r: r["latency_ms"]["search"]["p95"]),
            "search_p99": m(lambda r: r["latency_ms"]["search"]["p99"]),
            "stream_p95": m(lambda r: r["latency_ms"]["stream"]["p95"]),
            "ttfb_p50": m(lambda r: r["ttfb_ms"]["p50"]),
            "ttfb_p95": m(lambda r: r["ttfb_ms"]["p95"]),
            "all_p50": m(lambda r: r["latency_ms"]["all"]["p50"]),
            "all_p95": m(lambda r: r["latency_ms"]["all"]["p95"]),
            "all_p99": m(lambda r: r["latency_ms"]["all"]["p99"]),
            "error_rate": m(lambda r: r["error_rate"]),
            "rps": m(lambda r: r["rps"]),
            "gateway_cpu_pct_mean": m(lambda r: (r["resources"].get(r.get("gateway_container") or "", {}) or {}).get("cpu_pct_mean")),
            "gateway_mem_mib_max": m(lambda r: (r["resources"].get(r.get("gateway_container") or "", {}) or {}).get("mem_mib_max")),
            "authz_adapter_cpu_pct_mean": m(
                lambda r: (
                    (r["resources"].get("selfbuilt", {}) or {}).get("cpu_pct_mean")
                    if r["target"].split("-")[0] in ("agentgateway", "contextforge")
                    else None
                )
            ),
            "policy_cpu_pct_mean": m(lambda r: r["resources"]["policy"]["cpu_pct_mean"]),
            "upstream_cpu_pct_mean": m(lambda r: r["resources"]["upstream"]["cpu_pct_mean"]),
            "per_round_all_p95": [r["latency_ms"]["all"]["p95"] for r in rs],
        }
    base = agg.get("baseline")
    if base:
        for a in agg.values():
            for k in ("all_p95", "search_p95", "ttfb_p95", "all_p50"):
                a[f"extra_{k}"] = (a[k] - base[k]) if a.get(k) is not None and base.get(k) is not None else None
    hw = next(iter(runs.values()))[0]["hardware"] if runs else {}
    out = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "hardware": hw, "targets": agg}
    cases_p = RESULTS / "cases.json"
    if cases_p.exists():
        out["cases"] = json.loads(cases_p.read_text())
    out["ops_scores"] = OPS_SCORES
    (RESULTS / "summary.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
    (RESULTS / "summary.md").write_text(summary_md(out))
    print(json.dumps(agg, ensure_ascii=False, indent=2))
    return out


# 维度 5 运维复杂度评分（1～5，越高越好；依据见 rationale，ADR-0005 §3 的五个子项）
OPS_SCORES: dict[str, dict[str, Any]] = {
    "selfbuilt": {
        "scores": {"部署组件数": 5, "声明式配置/GitOps": 3, "升级与回滚成本": 5, "许可证与社区": 3, "Python 栈维护成本": 4},
        "rationale": "不新增组件（随 capability-gateway 发布）；MCP Server 登记走网关 API/种子配置，尚未声明式；升级回滚即网关版本；"
        "自有代码无许可证风险但无社区；约 270 行 Python（mcp_proxy.py），MCP 协议演进需自己跟进。",
    },
    "agentgateway": {
        "scores": {"部署组件数": 4, "声明式配置/GitOps": 5, "升级与回滚成本": 3, "许可证与社区": 5, "Python 栈维护成本": 3},
        "rationale": "新增 1 个 Rust 二进制（~45 MiB 内存）+ 网关内判定端点；YAML 声明式且热加载、K8s 有 CRD；1.x 每月发版、"
        "配置项仍在演进（本 POC 即踩到 CEL transformations 与 extAuthz.metadata 组合导致 SSE 缓冲）；Apache-2.0、LF 项目、活跃；"
        "团队不写 Rust，只能配置不能改源码。",
    },
    "contextforge": {
        "scores": {"部署组件数": 2, "声明式配置/GitOps": 2, "升级与回滚成本": 2, "许可证与社区": 5, "Python 栈维护成本": 3},
        "rationale": "网关 + 数据库（生产需 Postgres）+ 多实例需 Redis；上游与虚拟服务器通过 Admin API 登记、存库，非声明式；"
        "每版带数据库迁移、默认值变化大（本 POC 需关闭账号限流/锁定、放开 SSRF 私网）；Apache-2.0、IBM 主导、活跃；"
        "Python 栈可写插件，但代码库庞大（工具调用路径上有会话池、RBAC、指标落库等），定位/调优成本高。",
    },
}

CASE_LABELS = {
    "A1_obo_passthrough": "A1 OBO 令牌透传、不被网关凭据替换",
    "A2_client_identity_headers_stripped": "A2 客户端身份头剥离/覆盖",
    "A3_act_preserved": "A3 act（执行者）保留",
    "A4_expired_token_rejected": "A4 过期令牌被拒绝",
    "A5_audience_mismatch_rejected": "A5 受众不符被拒绝",
    "A6_no_plaintext_token_in_logs": "A6 日志无明文令牌",
    "P1_deny_not_forwarded": "P1 deny 不到达上游",
    "P2_policy_down_fail_closed": "P2 策略服务停掉 → 失败关闭",
    "P3_hook_endpoint_down_fail_closed": "P3 判定端点不可达 → 失败关闭",
    "P4_no_fork": "P4 无需 fork",
    "T1_traceparent_propagation": "T1 traceparent 透传率 100%",
    "T2_langfuse_span": "T2 Langfuse 可查 Span（run_id/call_id/工具/判定）",
}


def _f(x: Any, nd: int = 1) -> str:
    return "—" if x is None else (f"{x:.{nd}f}" if isinstance(x, float) else str(x))


def summary_md(out: dict[str, Any]) -> str:
    t = out["targets"]
    order = [k for k in ("baseline", "selfbuilt", "agentgateway", "contextforge", "selfbuilt-4cpu", "contextforge-notrace") if k in t]
    hw = out.get("hardware", {})
    lines = [
        "# 数据面 POC 结果汇总（ADR-0005）",
        "",
        f"生成时间：{out['generated_at']}。原始数据：`results/<目标>-r<轮次>.json`、`results/cases.json`；本文件由 `poc.py report` 生成。",
        "",
        "## 环境与任务画像",
        "",
        f"- 硬件：{hw.get('cpu')}，{hw.get('ncpu')} 核，内存 {int(hw.get('memsize') or 0) // 2**30} GiB；{hw.get('platform')}",
        f"- Docker：{hw.get('docker')}；镜像：{json.dumps(hw.get('images', {}), ensure_ascii=False)}",
        "- 网络路径：压测器容器 →（候选网关 →）上游容器，同一 Docker 网络；基线直连上游。",
        "- 资源：每个候选 2 CPU（补充对照 selfbuilt-4cpu 为 4 CPU）；上游 MCP Server 为单进程 Python（有状态会话、JWKS 验签）。",
        "- 负载：闭环 50 并发（10 进程 × 5），tools/call 请求体约 1 KB、响应约 10 KB；80% 普通工具 + 20% 流式工具"
        "（3 条进度通知间隔 10 ms 后返回结果）；每请求新 traceparent（sampled）。预热与时长见下表 params。",
        "",
        "## 维度 1：透传延迟与资源（各轮中位数，单位 ms）",
        "",
        "| 目标 | 轮数 | P50 | P95 | P99 | 额外 P95 | 普通工具 P95 | 流式首字节 P95 | 额外首字节 P95 "
        "| 错误率 | RPS | 网关 CPU% | 判定端点 CPU% | 网关内存 MiB |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for k in order:
        a = t[k]
        lines.append(
            f"| {k} | {a['rounds']} | {_f(a['all_p50'])} | {_f(a['all_p95'])} | {_f(a['all_p99'])} | {_f(a.get('extra_all_p95'))} | "
            f"{_f(a['search_p95'])} | {_f(a['ttfb_p95'])} | {_f(a.get('extra_ttfb_p95'))} | {_f(a['error_rate'], 4)} | {_f(a['rps'], 0)} | "
            f"{_f(a['gateway_cpu_pct_mean'], 0)} | {_f(a.get('authz_adapter_cpu_pct_mean'), 0)} | {_f(a['gateway_mem_mib_max'], 0)} |"
        )
    lines += ["", "各轮 P95（ms）：" + "；".join(f"{k} {[round(x, 1) for x in t[k]['per_round_all_p95']]}" for k in order), ""]
    if t:
        lines += [f"压测参数：`{json.dumps(next(iter(t.values()))['params'], ensure_ascii=False)}`", ""]
    cases = out.get("cases", {}).get("targets", {})
    if cases:
        cand = [c for c in ("selfbuilt", "agentgateway", "contextforge") if c in cases]
        lines += ["## 维度 2～4：功能用例（cases.py）", "", "| 用例 | " + " | ".join(cand) + " |", "|---|" + "---|" * len(cand)]
        for key, label in CASE_LABELS.items():
            row = []
            for c in cand:
                v = cases[c].get(key)
                row.append("—" if v is None else ("通过" if v.get("pass") else "**不通过**"))
            lines.append(f"| {label} | " + " | ".join(row) + " |")
        lines.append(
            "| 流式进度通知逐条实时到达 | "
            + " | ".join(
                ("是" if (lambda p: bool(p) and p[0] < p[-1] - 5)(cases[c].get("S_stream_progress", {}).get("progress_ms") or []) else "否")
                for c in cand
            )
            + " |"
        )
        lines.append("")
    lines += ["## 维度 5：运维复杂度评分（1～5）", "", "| 子项 | " + " | ".join(OPS_SCORES) + " |", "|---|" + "---|" * len(OPS_SCORES)]
    for item in next(iter(OPS_SCORES.values()))["scores"]:
        lines.append(f"| {item} | " + " | ".join(str(v["scores"][item]) for v in OPS_SCORES.values()) + " |")
    lines.append("| **平均** | " + " | ".join(f"{statistics.fmean(v['scores'].values()):.1f}" for v in OPS_SCORES.values()) + " |")
    lines += [""] + [f"- {k}：{v['rationale']}" for k, v in OPS_SCORES.items()] + [""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build")
    sub.add_parser("up")
    sub.add_parser("down")
    sub.add_parser("setup-cf")
    sub.add_parser("report")
    b = sub.add_parser("bench")
    b.add_argument("--warmup", type=float, default=60)
    b.add_argument("--duration", type=float, default=300)
    b.add_argument("--rounds", type=int, default=3)
    b.add_argument("--concurrency", type=int, default=50)
    b.add_argument("--procs", type=int, default=10)
    b.add_argument("--targets", default="")
    args = ap.parse_args()
    {
        "build": lambda: build(),
        "up": lambda: up(),
        "down": lambda: down(),
        "setup-cf": lambda: setup_contextforge(),
        "report": lambda: report(),
        "bench": lambda: bench(args),
    }[args.cmd]()
    return 0


if __name__ == "__main__":
    sys.exit(main())

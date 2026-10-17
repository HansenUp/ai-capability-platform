"""数据面 POC 压测器（ADR-0005 §3）：多进程、固定并发的闭环压测，MCP streamable-http 真实会话。

每个并发单元先 initialize（拿到 Mcp-Session-Id）并发送 notifications/initialized，然后循环 tools/call：
按 --stream-ratio 的比例调用流式工具（带 progressToken，先收到进度通知再收到结果），其余调用普通工具。
请求体用 padding 补齐到约 1 KB，上游响应约 10 KB。每个请求带新的 W3C traceparent 与 X-AI-Call-Id。

统计（只计入预热结束后发起的请求）：端到端延迟 P50/P95/P99（普通 / 流式分开）、流式首字节（收到第一条 SSE data 行）、
错误率（非 200、JSON-RPC error、结果缺失或超时都算错误）、RPS。结果以 JSON 输出。

请求头（含令牌）从环境变量 LOADGEN_HEADERS（JSON 对象）读取，不出现在命令行与结果文件里。

用法：
    LOADGEN_HEADERS='{"Authorization": "Bearer ..."}' python loadgen.py --url http://127.0.0.1:19100/mcp \
        --concurrency 50 --procs 5 --warmup 60 --duration 300 --out results/x.json --label baseline
"""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing as mp
import os
import platform
import random
import secrets
import statistics
import subprocess
import sys
import time
from typing import Any

import httpx

PROTOCOL_VERSION = "2025-06-18"


def percentile(values: list[float], p: float) -> float | None:
    """线性插值分位数（与 numpy 默认一致）；空列表返回 None。"""
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _traceparent() -> str:
    return f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"


async def _post(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], msg: dict[str, Any]
) -> tuple[int, float | None, str, httpx.Headers]:
    """发送一条 JSON-RPC，流式读取响应；返回（状态码, 首个 data 行的时刻, 全文, 响应头）。"""
    first: float | None = None
    parts: list[str] = []
    async with client.stream("POST", url, headers=headers, json=msg) as r:
        if r.headers.get("content-type", "").startswith("text/event-stream"):
            async for line in r.aiter_lines():
                if line.startswith("data:"):
                    if first is None:
                        first = time.perf_counter()
                    parts.append(line[5:].strip())
            # 读到流结束（上游发完最终响应即关闭本次 SSE 流）：提前退出会让 httpx 丢弃该连接，每请求新建 TCP 连接
        else:
            body = await r.aread()
            first = time.perf_counter()
            parts.append(body.decode(errors="replace"))
        return r.status_code, first, "\n".join(parts), r.headers


async def _session(client: httpx.AsyncClient, url: str, base: dict[str, str]) -> dict[str, str]:
    """建立 MCP 会话，返回后续请求要带的会话头。"""
    h = base | {"traceparent": _traceparent()}
    init = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "poc-loadgen", "version": "1"}},
    }
    status, _, text, headers = await _post(client, url, h, init)
    if status != 200 or '"result"' not in text:
        raise RuntimeError(f"initialize failed: HTTP {status} {text[:200]}")
    sess = {"Mcp-Protocol-Version": PROTOCOL_VERSION}
    if headers.get("mcp-session-id"):
        sess["Mcp-Session-Id"] = headers["mcp-session-id"]
    r = await client.post(url, headers=base | sess, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    if r.status_code not in (200, 202):
        raise RuntimeError(f"notifications/initialized failed: HTTP {r.status_code}")
    return sess


def _call(rid: int, tool: str, stream: bool, padding: str) -> dict[str, Any]:
    params: dict[str, Any] = {"name": tool}
    if stream:
        params["arguments"] = {"id": "T-1001", "padding": padding}
        params["_meta"] = {"progressToken": f"p-{rid}"}
    else:
        params["arguments"] = {"q": "设备重启", "filters": {"status": "open", "limit": 20}, "padding": padding}
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": params}


async def _worker(wid: int, args: argparse.Namespace, base: dict[str, str], t_measure: float, t_end: float, out: dict[str, Any]) -> None:
    rnd = random.Random(wid)  # noqa: S311 —— 仅用于请求混合比例
    padding = "x" * 760  # 使 tools/call 请求体约 1 KB
    timeout = httpx.Timeout(args.timeout, connect=10)
    async with httpx.AsyncClient(timeout=timeout, limits=httpx.Limits(max_connections=4)) as client:
        sess: dict[str, str] | None = None
        rid = 0
        while time.perf_counter() < t_end:
            try:
                if sess is None:
                    sess = await _session(client, args.url, base)
            except (httpx.HTTPError, RuntimeError) as e:
                out["session_errors"] += 1
                out["error_samples"].append(str(e)[:200])
                await asyncio.sleep(0.5)
                continue
            rid += 1
            stream = rnd.random() < args.stream_ratio
            tool = args.stream_tool if stream else args.tool
            h = base | sess | {"X-AI-Call-Id": f"call-{wid}-{rid}"} | ({} if args.no_traceparent else {"traceparent": _traceparent()})
            t0 = time.perf_counter()
            ok, first = False, None
            try:
                status, first, text, _ = await _post(client, args.url, h, _call(rid, tool, stream, padding))
                ok = status == 200 and '"result"' in text and '"isError":true' not in text.replace(" ", "")
                if not ok and len(out["error_samples"]) < 20:
                    out["error_samples"].append(f"HTTP {status} {text[:200]}")
                if status == 404:  # 会话失效：重建
                    sess = None
            except httpx.HTTPError as e:
                if len(out["error_samples"]) < 20:
                    out["error_samples"].append(type(e).__name__)
                sess = None
            t1 = time.perf_counter()
            if t0 < t_measure or t1 > t_end + args.timeout:
                continue
            kind = "stream" if stream else "search"
            out["count"][kind] += 1
            if ok:
                out["lat"][kind].append((t1 - t0) * 1000)
                if stream and first is not None:
                    out["ttfb"].append((first - t0) * 1000)
            else:
                out["errors"][kind] += 1


def _proc(idx: int, args: argparse.Namespace, base: dict[str, str], t_start_wall: float, q: Any) -> None:
    """子进程：本进程负责 concurrency/procs 个并发单元。perf_counter 不跨进程，统一用 wall 时间换算起点。"""
    offset = time.perf_counter() - time.time()
    t_measure = t_start_wall + args.warmup + offset
    t_end = t_measure + args.duration
    out: dict[str, Any] = {
        "lat": {"search": [], "stream": []},
        "ttfb": [],
        "count": {"search": 0, "stream": 0},
        "errors": {"search": 0, "stream": 0},
        "session_errors": 0,
        "error_samples": [],
    }
    n = args.concurrency // args.procs + (1 if idx < args.concurrency % args.procs else 0)

    async def main() -> None:
        await asyncio.gather(*(_worker(idx * 1000 + i, args, base, t_measure, t_end, out) for i in range(n)))

    asyncio.run(main())
    q.put(out)


def hardware() -> dict[str, Any]:
    """硬件与软件画像（ADR-0005 / P0-5.11 的测量口径要求）。"""
    info: dict[str, Any] = {"platform": platform.platform(), "python": sys.version.split()[0], "machine": platform.machine()}
    for key, cmd in {
        "cpu": ["sysctl", "-n", "machdep.cpu.brand_string"],
        "ncpu": ["sysctl", "-n", "hw.ncpu"],
        "memsize": ["sysctl", "-n", "hw.memsize"],
        "docker": ["docker", "info", "--format", "{{.ServerVersion}} cpus={{.NCPU}} mem={{.MemTotal}}"],
    }.items():
        try:
            info[key] = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False).stdout.strip()  # noqa: S603
        except (OSError, subprocess.SubprocessError):
            info[key] = None
    return info


def summarize(parts: list[dict[str, Any]], duration: float) -> dict[str, Any]:
    lat = {k: [x for p in parts for x in p["lat"][k]] for k in ("search", "stream")}
    ttfb = [x for p in parts for x in p["ttfb"]]
    count = {k: sum(p["count"][k] for p in parts) for k in ("search", "stream")}
    errors = {k: sum(p["errors"][k] for p in parts) for k in ("search", "stream")}
    total = sum(count.values())

    def pct(v: list[float]) -> dict[str, float | None]:
        return {
            "p50": percentile(v, 50),
            "p95": percentile(v, 95),
            "p99": percentile(v, 99),
            "mean": statistics.fmean(v) if v else None,
            "n": len(v),
        }

    return {
        "latency_ms": {k: pct(v) for k, v in lat.items()} | {"all": pct(lat["search"] + lat["stream"])},
        "ttfb_ms": pct(ttfb),
        "requests": total,
        "errors": sum(errors.values()),
        "errors_by_kind": errors,
        "error_rate": (sum(errors.values()) / total) if total else None,
        "rps": total / duration if duration else None,
        "session_errors": sum(p["session_errors"] for p in parts),
        "error_samples": [s for p in parts for s in p["error_samples"]][:20],
    }


def run(args: argparse.Namespace, base: dict[str, str]) -> dict[str, Any]:
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    t_start = time.time() + 1.0  # 给子进程留启动时间，统一起点
    procs = [ctx.Process(target=_proc, args=(i, args, base, t_start, q)) for i in range(args.procs)]
    for p in procs:
        p.start()
    parts = [q.get(timeout=args.warmup + args.duration + args.timeout + 120) for _ in procs]
    for p in procs:
        p.join()
    return {
        "label": args.label,
        "url": args.url,
        "params": {
            "concurrency": args.concurrency,
            "procs": args.procs,
            "warmup_s": args.warmup,
            "duration_s": args.duration,
            "stream_ratio": args.stream_ratio,
            "tool": args.tool,
            "stream_tool": args.stream_tool,
            "traceparent": not args.no_traceparent,
        },
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t_start)),
        "measure_window": [t_start + args.warmup, t_start + args.warmup + args.duration],
    } | summarize(parts, args.duration)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--label", default="run")
    ap.add_argument("--concurrency", type=int, default=50)
    ap.add_argument("--procs", type=int, default=5)
    ap.add_argument("--warmup", type=float, default=60)
    ap.add_argument("--duration", type=float, default=300)
    ap.add_argument("--stream-ratio", type=float, default=0.2)
    ap.add_argument("--tool", default="ticket.search")
    ap.add_argument("--stream-tool", default="ticket.get")
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--no-traceparent", action="store_true", help="补充对照：请求不带 traceparent")
    ap.add_argument("--out")
    args = ap.parse_args()
    base = {"Accept": "application/json, text/event-stream"} | json.loads(os.environ.get("LOADGEN_HEADERS", "{}"))
    result = run(args, base) | {"hardware": hardware()}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    print(json.dumps({k: result[k] for k in ("label", "requests", "error_rate", "rps", "latency_ms", "ttfb_ms")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

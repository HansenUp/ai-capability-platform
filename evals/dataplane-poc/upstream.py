"""数据面 POC 的上游 MCP Server（ADR-0005 §3 测试条件）：mcp 2.x 的 MCPServer（原 FastMCP），streamable-http。

工具（名字与能力目录中的种子能力一致，便于网关按名字做授权判定）：
- `ticket.search`：普通工具，请求约 1 KB（客户端用 padding 参数补齐），返回约 10 KB 文本；
- `ticket.get`：流式工具，客户端带 progressToken 时先发 3 条进度通知（间隔 10 ms）再返回约 10 KB；
- `ticket.purge`：未在能力目录登记的工具，用于"拒绝时请求不到达上游"用例。

身份（UPSTREAM_AUTH=jwt，默认）：与 mcp-ticket 相同的口径——Bearer JWT 按 JWKS 本地验签，校验 iss / aud / exp；
`X-AI-Act` 只在令牌 azp 属于可信网关时采信。验签失败返回 401 并记录拒绝原因（不记录令牌本身）。

探针记录：工具参数带 `probe` 时把本次调用看到的身份信息（令牌摘要与 claims、X-AI-* 头、traceparent）写入 SQLite，
`GET /_poc/probes/{probe}` 读取；`GET /_poc/rejections?since=<unix 秒>` 读取验签拒绝记录。压测请求不带 probe，不落盘。

运行：`uvicorn upstream:app --host 0.0.0.0 --port 9100`（会话有状态，单进程；见 README 的容量说明）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from typing import Any

import anyio
import jwt
from jwt import PyJWKClient
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

ISSUER = os.environ.get("UPSTREAM_ISSUER", "http://localhost:8180/realms/ai-platform")
JWKS_URL = os.environ.get("UPSTREAM_JWKS_URL", f"{ISSUER}/protocol/openid-connect/certs")
AUDIENCE = os.environ.get("UPSTREAM_AUDIENCE", "mcp-ticket")
TRUSTED_AZP = frozenset(os.environ.get("UPSTREAM_TRUSTED_AZP", "capability-gateway").split(","))
AUTH_MODE = os.environ.get("UPSTREAM_AUTH", "jwt")  # jwt | off
PROBE_DB = os.environ.get("UPSTREAM_PROBE_DB", "/tmp/upstream-probe.sqlite")  # noqa: S108 —— 容器内临时文件
STATELESS = os.environ.get("UPSTREAM_STATELESS", "0") == "1"

# 约 10 KB 的响应体：20 条工单摘要
TICKETS = [
    {
        "id": f"T-{1000 + i}",
        "title": f"售后工单 {i}：设备运行异常需上门检修",
        "status": "open",
        "summary": "客户反馈设备在高负载下间歇性重启，已远程排查电源与固件版本，需工程师现场确认主板状态。" * 2,
        "created_at": "2026-10-01T09:00:00+08:00",
        "owner": "aftersales",
    }
    for i in range(20)
]
RESPONSE = json.dumps({"tickets": TICKETS}, ensure_ascii=False)
RESPONSE += " " * max(0, 10 * 1024 - len(RESPONSE.encode()))

mcp = MCPServer("dataplane-poc-upstream")


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(PROBE_DB, timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS probes (probe TEXT, ts REAL, tool TEXT, data TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS rejections (ts REAL, reason TEXT, path TEXT)")
    return conn


def _unverified_claims(token: str) -> dict[str, Any]:
    try:
        return dict(jwt.decode(token, options={"verify_signature": False}))
    except jwt.PyJWTError:
        return {}


def _record(probe: str, tool: str, ctx: Context) -> None:
    """探针调用：记录上游实际收到的身份与追踪信息（只存令牌摘要，不存令牌）。"""
    h = {k.lower(): v for k, v in (ctx.headers or {}).items()}
    auth = h.get("authorization", "")
    token = auth.split(" ", 1)[1].strip() if auth.lower().startswith("bearer ") else ""
    claims = _unverified_claims(token) if token else {}
    azp = claims.get("azp")
    data = {
        "token_sha256": hashlib.sha256(token.encode()).hexdigest() if token else None,
        "claims": {k: claims.get(k) for k in ("sub", "azp", "aud", "tenant", "iss", "exp", "act")},
        "act_header": h.get("x-ai-act"),
        "act_trusted": h.get("x-ai-act") if azp in TRUSTED_AZP else None,
        "x_ai_headers": {k: v for k, v in h.items() if k.startswith("x-ai-")},
        "traceparent": h.get("traceparent"),
        "mcp_session_id": h.get("mcp-session-id"),
        "header_names": sorted(h),
    }
    with _db() as conn:
        conn.execute("INSERT INTO probes VALUES (?, ?, ?, ?)", (probe, time.time(), tool, json.dumps(data)))


@mcp.tool(name="ticket.search", description="按关键字查询工单（POC：返回约 10 KB）", structured_output=False)
async def ticket_search(ctx: Context, q: str, filters: dict[str, Any] | None = None, padding: str = "", probe: str | None = None) -> str:
    if probe:
        _record(probe, "ticket.search", ctx)
    return RESPONSE


@mcp.tool(name="ticket.get", description="查询工单详情（POC：流式，先发进度通知）", structured_output=False)
async def ticket_get(ctx: Context, id: str, padding: str = "", probe: str | None = None) -> str:  # noqa: A002 —— 与真实工具入参一致
    if probe:
        _record(probe, "ticket.get", ctx)
    for i in range(3):
        await ctx.report_progress(i + 1, 3, "loading")
        await anyio.sleep(0.01)
    return RESPONSE


@mcp.tool(name="ticket.purge", description="未登记的工具（POC：用于拒绝用例）", structured_output=False)
async def ticket_purge(ctx: Context, probe: str | None = None) -> str:
    if probe:
        _record(probe, "ticket.purge", ctx)
    return "purged"


@mcp.custom_route("/_poc/probes/{probe}", methods=["GET"])
async def probes(request: Request) -> JSONResponse:
    with _db() as conn:
        rows = conn.execute("SELECT ts, tool, data FROM probes WHERE probe = ? ORDER BY ts", (request.path_params["probe"],)).fetchall()
    return JSONResponse([{"ts": ts, "tool": tool} | json.loads(data) for ts, tool, data in rows])


@mcp.custom_route("/_poc/rejections", methods=["GET"])
async def rejections(request: Request) -> JSONResponse:
    since = float(request.query_params.get("since", "0"))
    with _db() as conn:
        rows = conn.execute("SELECT ts, reason, path FROM rejections WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
    return JSONResponse([{"ts": ts, "reason": reason, "path": path} for ts, reason, path in rows])


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


class JwtAuth:
    """/mcp 的 Bearer JWT 验签（ASGI 中间件）；/_poc 与 /healthz 不鉴权（只在 POC 网络内暴露）。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.jwks = PyJWKClient(JWKS_URL, cache_keys=True, lifespan=300)
        self.keys: dict[str, Any] = {}  # kid → 公钥：命中时不再进线程池查 JWKS

    async def _key(self, token: str) -> Any:
        kid = jwt.get_unverified_header(token).get("kid", "")
        if kid not in self.keys:
            self.keys[kid] = (await anyio.to_thread.run_sync(self.jwks.get_signing_key_from_jwt, token)).key
        return self.keys[kid]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/mcp") or AUTH_MODE == "off":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        auth = headers.get("authorization", "")
        reason = None
        if not auth.lower().startswith("bearer "):
            reason = "missing bearer token"
        else:
            token = auth.split(" ", 1)[1].strip()
            try:
                key = await self._key(token)
                jwt.decode(
                    token, key, algorithms=["RS256"], audience=AUDIENCE, issuer=ISSUER, options={"require": ["exp", "sub", "iss", "aud"]}
                )
            except jwt.ExpiredSignatureError:
                reason = "token expired"
            except jwt.InvalidAudienceError:
                reason = "audience mismatch"
            except (jwt.PyJWTError, jwt.PyJWKClientError) as e:
                reason = f"invalid token: {type(e).__name__}"
        if reason is None:
            await self.app(scope, receive, send)
            return
        with _db() as conn:
            conn.execute("INSERT INTO rejections VALUES (?, ?, ?)", (time.time(), reason, scope["path"]))
        resp = JSONResponse({"error": f"upstream: {reason}"}, status_code=401, headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})
        await resp(scope, receive, send)


app = JwtAuth(
    mcp.streamable_http_app(
        stateless_http=STATELESS,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host="0.0.0.0",  # noqa: S104 —— 容器内监听，端口只绑宿主机 127.0.0.1
    )
)

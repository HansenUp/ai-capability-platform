#!/usr/bin/env python3
"""本机联调用的应用进程启停（compose 中的基础设施另行 `make compose-up`）。

启动 7 个进程，日志写到 .dev/logs/，进程号记在 .dev/pids.json：
- mcp-ticket     :8091  工单系统只读 MCP Server（mock 数据，按 OBO 令牌做行级权限）
- gateway        :8082  能力网关（Token Exchange 换取 OBO 令牌后分发工具调用）
- runtime        :8080  强制鉴权（Keycloak JWT）、litellm 真实模型、MySQL、网关 Key 来自 Vault、反馈经观测适配器
- console-api    :8081  MySQL；同步 Runtime 时用 console-api 的 Client Credentials 令牌
- observe-adapter:8084  反馈转发 Langfuse
- portal-web     :5173  门户（Keycloak PKCE 登录）
- console-web    :5174  Console 前端（开发态身份头，可切换角色 / 租户）

用法：
    .venv/bin/python scripts/dev_stack.py start    # 启动并等待就绪
    .venv/bin/python scripts/dev_stack.py status
    .venv/bin/python scripts/dev_stack.py stop
本脚本中的口令、令牌均为本机 compose 的公开开发值，不用于任何其他环境。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DEV = ROOT / ".dev"
PIDS = DEV / "pids.json"
VENV = ROOT / ".venv" / "bin"
REALM = "http://localhost:8180/realms/ai-platform"
DB = "mysql+asyncmy://root:root@localhost:3316/ai_platform"

RUNTIME_ENV = {
    "DATABASE_URL": DB,
    "WORKSPACE_DIR": str(ROOT / "runtime-workspace"),
    "HARNESS": "litellm",
    "POLICY_URL": "http://127.0.0.1:1",  # 策略服务尚未接入 → 闸口回退基线规则
    "CAPABILITY_GATEWAY_URL": "http://127.0.0.1:8082",
    "TOOL_DISPATCH": "gateway",  # 工具经能力网关分发到 MCP Server（S4.3）
    "AUTH_REQUIRED": "true",
    "JWKS_URL": f"{REALM}/protocol/openid-connect/certs",
    "JWT_ISSUER": REALM,
    "JWT_AUDIENCE": "ai-runtime",
    "VAULT_ADDR": "http://localhost:8200",
    "VAULT_TOKEN": "dev-root",
    "OBSERVE_ADAPTER_URL": "http://127.0.0.1:8084",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318",
}
CONSOLE_ENV = {
    "CONSOLE_DATABASE_URL": DB,
    "RUNTIME_ADMIN_URL": "http://127.0.0.1:8080",
    "CONSOLE_OIDC_TOKEN_URL": f"{REALM}/protocol/openid-connect/token",
    "CONSOLE_CLIENT_SECRET": "console-api-dev-secret",
}
MCP_TICKET_ENV = {
    "MCP_TICKET_JWKS_URL": f"{REALM}/protocol/openid-connect/certs",
    "MCP_TICKET_ISSUER": REALM,
    "MCP_TICKET_AUDIENCE": "mcp-ticket",
    "MCP_TICKET_DB": str(DEV / "mcp-ticket.db"),
}
GATEWAY_ENV = {
    "KEYCLOAK_TOKEN_URL": f"{REALM}/protocol/openid-connect/token",
    "GATEWAY_CLIENT_SECRET": "capability-gateway-dev-secret",
    "MCP_TICKET_URL": "http://127.0.0.1:8091",
    "POLICY_URL": "http://127.0.0.1:1",
}
OBSERVE_ENV = {"LANGFUSE_HOST": "http://localhost:3000", "LANGFUSE_PUBLIC_KEY": "pk-lf-dev", "LANGFUSE_SECRET_KEY": "sk-lf-dev"}


def uvicorn(app: str, app_dir: str, port: int) -> list[str]:
    return [str(VENV / "uvicorn"), app, "--port", str(port), "--app-dir", str(ROOT / app_dir)]


SERVICES: list[tuple[str, list[str], dict[str, str], str]] = [
    ("mcp-ticket", uvicorn("mcp_ticket.main:app", "mcp-servers/ticket/src", 8091), MCP_TICKET_ENV, "http://127.0.0.1:8091/healthz"),
    (
        "gateway",
        uvicorn("capability_gateway.main:app", "services/capability-gateway/src", 8082),
        GATEWAY_ENV,
        "http://127.0.0.1:8082/healthz",
    ),
    ("runtime", uvicorn("runtime.main:app", "services/runtime/src", 8080), RUNTIME_ENV, "http://127.0.0.1:8080/healthz"),
    ("console-api", uvicorn("console_api.main:app", "services/console-api/src", 8081), CONSOLE_ENV, "http://127.0.0.1:8081/healthz"),
    (
        "observe-adapter",
        uvicorn("observe_adapter.main:app", "services/observe-adapter/src", 8084),
        OBSERVE_ENV,
        "http://127.0.0.1:8084/healthz",
    ),
    ("portal-web", ["pnpm", "--dir", "apps/portal-web", "dev"], {}, "http://localhost:5173/"),
    ("console-web", ["pnpm", "--dir", "apps/console-web", "dev"], {}, "http://localhost:5174/"),
]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _up(url: str) -> bool:
    try:
        return httpx.get(url, timeout=2).status_code < 500
    except httpx.HTTPError:
        return False


def start() -> int:
    (DEV / "logs").mkdir(parents=True, exist_ok=True)
    pids = json.loads(PIDS.read_text()) if PIDS.exists() else {}
    for name, cmd, env, url in SERVICES:
        if _up(url):
            print(f"{name:16} 已在运行（{url}）")
            continue
        log = open(DEV / "logs" / f"{name}.log", "a")  # noqa: SIM115 —— 由子进程持有
        proc = subprocess.Popen(cmd, cwd=ROOT, env=os.environ | env, stdout=log, stderr=log, start_new_session=True)  # noqa: S603
        pids[name] = proc.pid
        print(f"{name:16} 启动中（pid {proc.pid}）")
    PIDS.write_text(json.dumps(pids))
    deadline = time.time() + 90
    pending = {name: url for name, _, _, url in SERVICES}
    while pending and time.time() < deadline:
        pending = {n: u for n, u in pending.items() if not _up(u)}
        time.sleep(1)
    for n in pending:
        print(f"{n:16} 未就绪，查看 .dev/logs/{n}.log")
    return 0 if not pending else 1


def stop() -> int:
    pids = json.loads(PIDS.read_text()) if PIDS.exists() else {}
    for name, pid in pids.items():
        if _alive(pid):
            os.killpg(pid, signal.SIGTERM)
            print(f"{name:16} 已停止（pid {pid}）")
    PIDS.unlink(missing_ok=True)
    return 0


def status() -> int:
    for name, _, _, url in SERVICES:
        print(f"{name:16} {'运行中' if _up(url) else '未运行'}  {url}")
    return 0


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    sys.exit({"start": start, "stop": stop, "status": status}[action]())

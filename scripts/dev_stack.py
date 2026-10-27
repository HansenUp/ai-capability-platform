#!/usr/bin/env python3
"""本机联调用的应用进程启停（compose 中的基础设施另行 `make compose-up`）。

启动 12 个进程，日志写到 .dev/logs/，进程号记在 .dev/pids.json：
- cedar-engine   :8183  Cedar 判定引擎 sidecar（宿主机二进制，先 make cedar-build；策略文件改动 5 s 内热加载）
- policy         :8083  策略服务（POLICY_ENGINE=cedar，/authz/check-batch；引擎不可达一律 deny）
- mcp-ticket     :8091  工单系统 MCP Server（mock 数据；生产态：OBO 令牌 JWKS 验签 + 网关 act 断言验签，行级权限）
- mock-knowledge :8092  知识库 mock 连接器（文档 ACL 与分级）      ┐ 生产态鉴权（OBO + act 断言），
- mock-crm       :8093  CRM mock 连接器（商机，confidential）        │ /_mock/calls 审计上游收到的身份，
- mock-mail      :8094  邮件 mock 连接器（mail.send 幂等 + dry-run） ┘ /_mock/outbox 发件箱计数
- gateway        :8082  能力网关：目录 MySQL（库 ai_gateway，启动前自动建库、导入种子并重建 ES 索引）+ ES 混合检索
                        （fastembed bge-small-zh）、服务间鉴权（服务令牌 azp ∈ ai-runtime-svc / console-api）、OBO + act 断言、
                        经策略服务判定；Skill 商店（S9）：包存 compose MinIO（SKILL_STORE=s3，桶 ai-skills），签名私钥为开发
                        密钥 ~/.ai-platform/keys/skill-signing.pem（不入库），数据分级矩阵为开发矩阵
- runtime        :8080  强制鉴权（Keycloak JWT）、litellm 真实模型、MySQL、网关 Key 来自 Vault、反馈经观测适配器；
                        以服务客户端 ai-runtime-svc 调用网关，闸口经策略服务判定（失败关闭）；S9：Skill 物化到 .dev/runtime-skills
                        并按 .dev/skill-trust-keys.json（启动前由签名私钥导出）验签，节点代理以 ai-node-svc 服务令牌向 Console
                        注册 / 心跳 / 拉指令，出站白名单由配置推导（模型网关、能力网关、策略服务、观测、Keycloak、Console），
                        沙箱 L1 用本机 docker（python:3.12-slim 按 digest 预拉取）
- console-api    :8081  MySQL；同步 Runtime 时用 console-api 的 Client Credentials 令牌；以同一服务客户端调网关 /skills*
                        （审核页转发、评测回放回写）；节点接口只接受 ai-node-svc 服务令牌（aud=console-api）
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEV = ROOT / ".dev"
PIDS = DEV / "pids.json"
VENV = ROOT / ".venv" / "bin"
REALM = "http://localhost:8180/realms/ai-platform"
DB = "mysql+asyncmy://root:root@localhost:3316/ai_platform"
GATEWAY_DB = "mysql+asyncmy://root:root@localhost:3316/ai_gateway"
CERTS = f"{REALM}/protocol/openid-connect/certs"
ACT_JWKS = "http://127.0.0.1:8082/.well-known/jwks.json"  # 网关签发 act 断言的公钥
# 本机网关的登记管理令牌（启用 Keycloak 时不配置则登记接口关闭）；仅本机开发值
GATEWAY_ADMIN_TOKEN = "dev-gateway-admin"  # noqa: S105

# S9 Skill：签名私钥（开发值，不入库）→ 导出给 Runtime 的信任公钥；Runtime 物化目录；数据分级开发矩阵（未签署）
SKILL_KEY = devkit.SKILL_DEV_KEY_FILE
SKILL_TRUST_KEYS = DEV / "skill-trust-keys.json"
DATA_CLASS_MATRIX = str(ROOT / "services" / "runtime" / "data_class_matrix.dev.json")

CEDAR_ENV = {"CEDAR_LISTEN": "127.0.0.1:8183"}
POLICY_ENV = {"POLICY_ENGINE": "cedar", "CEDAR_URL": "http://127.0.0.1:8183"}

RUNTIME_ENV = (
    {
        "DATABASE_URL": DB,
        "WORKSPACE_DIR": str(ROOT / "runtime-workspace"),
        "HARNESS": "litellm",
        "POLICY_URL": "http://127.0.0.1:8083",  # 闸口经策略服务判定，不可达即拒绝
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
        "SKILLS_DIR": str(DEV / "runtime-skills"),
        "SKILL_TRUST_KEYS_FILE": str(SKILL_TRUST_KEYS),
        "CONSOLE_URL": "http://127.0.0.1:8081",  # 节点代理：注册 / 心跳 / 拉指令（reload_skills）/ 回执，吊销列表随心跳刷新
        "DATA_CLASS_MATRIX_FILE": DATA_CLASS_MATRIX,
    }
    | devkit.runtime_svc_env()
    | devkit.node_svc_env()
)
CONSOLE_ENV = {
    "CONSOLE_DATABASE_URL": DB,
    "RUNTIME_ADMIN_URL": "http://127.0.0.1:8080",
    "CONSOLE_OIDC_TOKEN_URL": f"{REALM}/protocol/openid-connect/token",
    "CONSOLE_CLIENT_SECRET": "console-api-dev-secret",
    "CONSOLE_GATEWAY_URL": "http://127.0.0.1:8082",  # S9：/skills* 转发与评测回放回写（服务客户端 console-api，网关受信调用方）
    "CONSOLE_GATEWAY_CLIENT_ID": "console-api",
    "CONSOLE_GATEWAY_ADMIN_TOKEN": GATEWAY_ADMIN_TOKEN,  # 审核页的吊销转发（网关吊销需管理令牌）
} | devkit.console_node_auth_env()
MCP_TICKET_ENV = {
    "MCP_TICKET_JWKS_URL": f"{REALM}/protocol/openid-connect/certs",
    "MCP_TICKET_ISSUER": REALM,
    "MCP_TICKET_AUDIENCE": "mcp-ticket",
    "MCP_TICKET_DB": str(DEV / "mcp-ticket.db"),
    "MCP_TICKET_ACT_JWKS_URL": ACT_JWKS,
}


def mock_env(name: str) -> dict[str, str]:
    """mock 连接器的生产态鉴权：OBO 令牌经 Keycloak JWKS 验签（aud=mcp-<name>），act 断言经网关 JWKS 验签。"""
    prefix = f"MCP_{name.upper()}"
    env = {f"{prefix}_JWKS_URL": CERTS, f"{prefix}_ISSUER": REALM, f"{prefix}_ACT_JWKS_URL": ACT_JWKS}
    return env | ({"MCP_MAIL_DB": str(DEV / "mcp-mail.db")} if name == "mail" else {})


GATEWAY_ENV = {
    "KEYCLOAK_TOKEN_URL": f"{REALM}/protocol/openid-connect/token",
    "GATEWAY_CLIENT_SECRET": "capability-gateway-dev-secret",
    "GATEWAY_ADMIN_TOKEN": GATEWAY_ADMIN_TOKEN,
    # Runtime 服务客户端 + Console 服务客户端（Skill 评测结果回写、审核页转发）。节点代理不直连网关（取包 / 吊销列表经
    # Runtime 的 ai-runtime-svc），ai-node-svc 令牌的 aud 为 console-api，不在此列
    "GATEWAY_TRUSTED_CALLERS": "ai-runtime-svc,console-api",
    "GATEWAY_USER_AUDIENCE": "capability-gateway",
    "GATEWAY_ACT_SIGNING_KEY_FILE": str(DEV / "act-signing-key.pem"),
    "CATALOG_BACKEND": "mysql",
    "GATEWAY_DATABASE_URL": GATEWAY_DB,
    "ES_URL": "http://localhost:9200",
    "EMBED_PROVIDER": "fastembed",
    "POLICY_URL": "http://127.0.0.1:8083",
    "MCP_TICKET_URL": "http://127.0.0.1:8091",
    "MCP_KNOWLEDGE_URL": "http://127.0.0.1:8092",
    "MCP_CRM_URL": "http://127.0.0.1:8093",
    "MCP_MAIL_URL": "http://127.0.0.1:8094",
    "SKILL_STORE": "s3",  # 本机覆盖 MinIO 路径（CI 夜间用 fs）
    "SKILL_S3_ENDPOINT": "127.0.0.1:9000",
    "SKILL_S3_ACCESS_KEY": "minio",
    "SKILL_S3_SECRET_KEY": "minio12345",  # compose-dev MinIO 开发值
    "SKILL_S3_BUCKET": "ai-skills",
    "SKILL_SIGNING_KEY_FILE": str(SKILL_KEY),
    "DATA_CLASS_MATRIX_FILE": DATA_CLASS_MATRIX,
}
OBSERVE_ENV = {"LANGFUSE_HOST": "http://localhost:3000", "LANGFUSE_PUBLIC_KEY": "pk-lf-dev", "LANGFUSE_SECRET_KEY": "sk-lf-dev"}


def uvicorn(app: str, app_dir: str, port: int) -> list[str]:
    return [str(VENV / "uvicorn"), app, "--port", str(port), "--app-dir", str(ROOT / app_dir)]


SERVICES: list[tuple[str, list[str], dict[str, str], str]] = [
    ("cedar-engine", [str(devkit.CEDAR_BIN)], CEDAR_ENV, "http://127.0.0.1:8183/healthz"),
    ("policy", uvicorn("policy_service.main:app", "services/policy/src", 8083), POLICY_ENV, "http://127.0.0.1:8083/healthz"),
    *(
        (f"mock-{n}", [str(VENV / "python"), "-m", "mcp_mocks", n], mock_env(n), f"http://127.0.0.1:{p}/healthz")
        for n, p in (("knowledge", 8092), ("crm", 8093), ("mail", 8094))
    ),
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


def prepare_gateway() -> None:
    """网关启动前：建目录库 ai_gateway、导入开发种子（只补缺失项）、按当前 EMBED_PROVIDER 重建 ES 索引并切别名。"""
    devkit.act_key_file(DEV / "act-signing-key.pem")
    devkit.ensure_mysql_db(GATEWAY_DB)
    env = os.environ | GATEWAY_ENV
    for mod in ("capability_gateway.seed", "capability_gateway.reindex"):
        subprocess.run([str(VENV / "python"), "-m", mod], cwd=ROOT, env=env, check=True)  # noqa: S603


def prepare_runtime() -> None:
    """Runtime 启动前：由 Skill 签名私钥导出信任公钥（SKILL_TRUST_KEYS_FILE），物化目录就位。"""
    devkit.skill_trust_keys(SKILL_KEY, SKILL_TRUST_KEYS)
    (DEV / "runtime-skills").mkdir(parents=True, exist_ok=True)


def start() -> int:
    (DEV / "logs").mkdir(parents=True, exist_ok=True)
    if not devkit.CEDAR_BIN.is_file():
        print(f"未找到 Cedar 引擎 {devkit.CEDAR_BIN}：先执行 make cedar-build")
        return 1
    pids = json.loads(PIDS.read_text()) if PIDS.exists() else {}
    for name, cmd, env, url in SERVICES:
        if _up(url):
            print(f"{name:16} 已在运行（{url}）")
            continue
        if name == "gateway":
            prepare_gateway()
        if name == "runtime":
            prepare_runtime()
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

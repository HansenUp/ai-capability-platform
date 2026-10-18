"""本机联调与验证脚本共用的小工具：Keycloak 登录（授权码 + PKCE，与门户相同的请求参数）、Client Credentials 令牌、
Langfuse 公共 API，以及 S8 起的策略栈（Cedar 引擎 sidecar + 策略服务）子进程启停、Runtime 服务客户端配置、建 MySQL 库。
口令与密钥均为本机 compose 的开发值（services/identity/realm-export.json），只在本机有效。

命令行：``python scripts/devkit.py ensure-db <mysql+asyncmy://...>``（库不存在时创建，供 Makefile / CI 使用）。"""

from __future__ import annotations

import atexit
import base64
import hashlib
import html
import os
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
KEYCLOAK = os.environ.get("KEYCLOAK_URL", "http://localhost:8180")
REALM = f"{KEYCLOAK}/realms/ai-platform"
TOKEN_URL = f"{REALM}/protocol/openid-connect/token"
CERTS_URL = f"{REALM}/protocol/openid-connect/certs"
# Cedar 引擎二进制（make cedar-build 产物；CEDAR_ENGINE_BIN 可指定其他位置）
CEDAR_BIN = Path(os.environ.get("CEDAR_ENGINE_BIN") or ROOT / "services/policy/cedar-engine/target/release/cedar-engine")
LANGFUSE = os.environ.get("LANGFUSE_HOST", "http://localhost:3000")
LF_PK, LF_SK = os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-dev"), os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-dev")
LF_AUTH = {"Authorization": "Basic " + base64.b64encode(f"{LF_PK}:{LF_SK}".encode()).decode()}
# 本机 realm 种子中的开发用户 → 固定用户 id
USER_IDS = {
    "alice": "00000000-0000-4000-8000-0000000a11ce",
    "bob": "00000000-0000-4000-8000-000000000b0b",
    "carol": "00000000-0000-4000-8000-0000000ca201",
    "dave": "00000000-0000-4000-8000-0000000da7e0",
}


def dev_password(user: str) -> str:
    return f"{user}-dev-password"


def user_token(user: str, client_id: str = "portal-web", redirect: str = "http://localhost:5173/callback") -> str:
    """以门户客户端走授权码 + PKCE 登录本机开发用户，返回访问令牌。"""
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    q = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect,
        "scope": "openid",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    with httpx.Client(timeout=15, follow_redirects=False) as c:
        page = c.get(f"{REALM}/protocol/openid-connect/auth?{urlencode(q)}")
        m = re.search(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', page.text)
        if not m:
            raise RuntimeError(f"未找到 Keycloak 登录表单（HTTP {page.status_code}）")
        # Keycloak 会话 Cookie 带 Secure：httpx 在 http 下不自动发送，显式带上（浏览器把 localhost 视为安全上下文）
        cookie = {"Cookie": "; ".join(f"{k}={v}" for k, v in c.cookies.items())}
        r = c.post(html.unescape(m.group(1)), data={"username": user, "password": dev_password(user), "credentialId": ""}, headers=cookie)
        loc = urlparse(r.headers.get("location", ""))
        params = parse_qs(loc.query)
        if r.status_code not in (302, 303) or params.get("state") != [state]:
            raise RuntimeError(f"登录失败（HTTP {r.status_code}）")
        tok = c.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": params["code"][0],
                "redirect_uri": redirect,
                "code_verifier": verifier,
            },
        )
        tok.raise_for_status()
        return str(tok.json()["access_token"])


def client_token(client_id: str) -> str:
    """Client Credentials 令牌（本机开发客户端密钥为 <clientId>-dev-secret）。"""
    r = httpx.post(
        TOKEN_URL, data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": f"{client_id}-dev-secret"}, timeout=10
    )
    r.raise_for_status()
    return str(r.json()["access_token"])


def langfuse(method: str, path: str, **kwargs: Any) -> httpx.Response:
    """调用 Langfuse 公共 API（Basic Auth：项目公钥 / 私钥）。"""
    return httpx.request(method, f"{LANGFUSE}{path}", headers=LF_AUTH, timeout=30, **kwargs)


def runtime_svc_env() -> dict[str, str]:
    """Runtime 以服务客户端 ai-runtime-svc 调用能力网关（契约 3）的配置：网关启用 Keycloak 时必须配置，否则 401。"""
    return {
        "RUNTIME_GATEWAY_TOKEN_URL": TOKEN_URL,
        "RUNTIME_GATEWAY_CLIENT_ID": "ai-runtime-svc",
        "RUNTIME_GATEWAY_CLIENT_SECRET": "ai-runtime-svc-dev-secret",
    }


def act_key_file(path: Path) -> Path:
    """能力网关 act 断言的 ES256 签名密钥（PEM，P-256）；不存在时生成。本机持久化，网关重启后连接器缓存的 JWKS 仍然有效。"""
    if not path.exists():
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pem)
        path.chmod(0o600)
    return path


def _wait_http(url: str, proc: subprocess.Popen[bytes], name: str, log: Path, timeout_s: float = 60) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{name} 启动后退出（code {proc.returncode}），见 {log}")
        try:
            if httpx.get(url, timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"{name} 未就绪（{url}），见 {log}")


@contextmanager
def process(name: str, cmd: list[str], env: dict[str, str], health_url: str, log_dir: Path) -> Iterator[subprocess.Popen[bytes]]:
    """以子进程启动一个服务（环境变量叠加在当前环境之上），等健康检查 200 后交出进程；退出时终止。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / f"{name}.log"
    with log.open("ab") as out:
        proc = subprocess.Popen(cmd, cwd=ROOT, env=os.environ | env, stdout=out, stderr=subprocess.STDOUT)  # noqa: S603 —— 固定参数
        try:
            _wait_http(health_url, proc, name, log)
            yield proc
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def cedar_binary() -> Path:
    """Cedar 引擎二进制；未编译时给出编译命令。"""
    if not CEDAR_BIN.is_file():
        raise RuntimeError(f"未找到 Cedar 引擎 {CEDAR_BIN}：先执行 make cedar-build")
    return CEDAR_BIN


@contextmanager
def policy_stack(
    log_dir: Path, *, policy_port: int = 18083, cedar_port: int = 18183, cedar_env: dict[str, str] | None = None
) -> Iterator[str]:
    """启动真实的策略栈：Cedar 引擎 sidecar（127.0.0.1:cedar_port）+ 策略服务（POLICY_ENGINE=cedar，127.0.0.1:policy_port），
    交出策略服务基址（Runtime / 网关的 POLICY_URL）。cedar_env 可覆盖 CEDAR_POLICY_FILE 等（热加载验证用）。"""
    cedar = f"127.0.0.1:{cedar_port}"
    with (
        process("cedar-engine", [str(cedar_binary())], {"CEDAR_LISTEN": cedar} | (cedar_env or {}), f"http://{cedar}/healthz", log_dir),
        process(
            "policy",
            [PY, "-m", "uvicorn", "policy_service.main:app", "--host", "127.0.0.1", "--port", str(policy_port)],
            {"POLICY_ENGINE": "cedar", "CEDAR_URL": f"http://{cedar}"},
            f"http://127.0.0.1:{policy_port}/healthz",
            log_dir,
        ),
    ):
        yield f"http://127.0.0.1:{policy_port}"


def start_policy_stack(log_dir: Path, **kwargs: Any) -> str:
    """policy_stack 的非上下文形式（进程内运行 Runtime 的验证脚本用）：进程退出时自动停止。"""
    stack = ExitStack()
    url = stack.enter_context(policy_stack(log_dir, **kwargs))
    atexit.register(stack.close)
    return url


def ensure_mysql_db(url: str) -> None:
    """MySQL 库不存在时创建（utf8mb4）。url 形如 mysql+asyncmy://user:pass@host:port/db。"""
    import asyncio

    import asyncmy  # type: ignore[import-untyped]
    from sqlalchemy.engine import make_url

    u = make_url(url)
    db = u.database or ""
    if not re.fullmatch(r"[A-Za-z0-9_]+", db):
        raise ValueError(f"非法库名：{db!r}")

    async def run() -> None:
        conn = await asyncmy.connect(host=u.host or "localhost", port=u.port or 3306, user=u.username or "root", password=u.password or "")
        try:
            async with conn.cursor() as cur:
                await cur.execute("SET sql_notes = 0")  # 库已存在的 note 不必作为警告输出
                await cur.execute(f"CREATE DATABASE IF NOT EXISTS `{db}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
        finally:
            conn.close()

    asyncio.run(run())


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "ensure-db":
        ensure_mysql_db(sys.argv[2])
        print(f"数据库已就绪：{sys.argv[2].rsplit('/', 1)[-1]}")
    else:
        sys.exit("用法：python scripts/devkit.py ensure-db <mysql+asyncmy://user:pass@host:port/db>")

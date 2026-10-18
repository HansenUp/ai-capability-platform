#!/usr/bin/env python3
"""S3 本机出口验证：10% 灰度放量与一键回滚、门户 OIDC（PKCE）登录后对话、反馈在 Langfuse Trace 上可见。

前置：compose 已启动 keycloak、litellm 与 observe profile（Langfuse）。本脚本以子进程启动：
- 阶段 A：Runtime（开发态身份头，mock Harness，不耗模型）+ Console（临时 SQLite）。经 Console 发布 v1 全量、
  v2 灰度 10%，用 200 个不同主体对话统计实际版本分布；放量到 50% 再统计；一键回滚到 v1 后应全部回到 v1。
- 阶段 B：Runtime（AUTH_REQUIRED=true，Keycloak 验签，litellm 真实模型）+ 观测适配器（转发 Langfuse）。
  以门户客户端 portal-web 走授权码 + PKCE 登录本机开发用户 alice（realm 种子数据中的测试账号），
  用拿到的令牌列出 Agent、对话，再点踩并提交修正，最后在 Langfuse 中查到挂在该 Trace 上的 user_feedback 评分。

用法：
    .venv/bin/python scripts/verify_s3_e2e.py
退出码 0 表示全部通过。
"""

from __future__ import annotations

import base64
import hashlib
import html
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
KEYCLOAK = os.environ.get("KEYCLOAK_URL", "http://localhost:8180")
REALM = f"{KEYCLOAK}/realms/ai-platform"
LANGFUSE = os.environ.get("LANGFUSE_HOST", "http://localhost:3000")
LF_PK, LF_SK = os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-dev"), os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-dev")
LF_AUTH = {"Authorization": "Basic " + base64.b64encode(f"{LF_PK}:{LF_SK}".encode()).decode()}
ALIAS = os.environ.get("S3_MODEL_ALIAS", "deepseek-v4-pro-2026-08")
# 本机 realm 种子数据中的开发测试账号（services/identity/realm-export.json），仅限本机 compose
DEV_USER, DEV_PASSWORD = "alice", "alice-dev-password"  # noqa: S105
TADMIN = {"X-Console-User": "ops", "X-Console-Role": "TENANT_ADMIN"}

failures: list[str] = []
_tmp = Path(tempfile.mkdtemp(prefix="s3-e2e-"))


def check(ok: bool, what: str) -> None:
    print(f"  [{'通过' if ok else '失败'}] {what}")
    if not ok:
        failures.append(what)


@contextmanager
def service(name: str, app: str, app_dir: str, port: int, env: dict[str, str]) -> Iterator[str]:
    """以子进程启动一个 uvicorn 服务，等 /healthz 就绪后交出基址；退出时终止进程。"""
    log = open(_tmp / f"{name}.log", "w")  # noqa: SIM115
    full_env = os.environ.copy() | env
    proc = subprocess.Popen(  # noqa: S603 —— 固定参数启动本仓库的服务
        [PY, "-m", "uvicorn", app, "--port", str(port), "--app-dir", str(ROOT / app_dir)], env=full_env, stdout=log, stderr=log
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError(f"{name} 未能启动，见 {_tmp / (name + '.log')}")
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        log.close()


_policy_url: str | None = None


def policy_url() -> str:
    """真实策略栈（Cedar 引擎 + 策略服务子进程，首次调用时启动，进程退出时停止；需 make cedar-build）。"""
    global _policy_url
    if _policy_url is None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import devkit

        _policy_url = devkit.start_policy_stack(_tmp)
    return _policy_url


def runtime_env(tag: str, **extra: str) -> dict[str, str]:
    return {
        "DATABASE_URL": f"sqlite+aiosqlite:///{_tmp}/runtime-{tag}.db",
        "WORKSPACE_DIR": str(_tmp / f"ws-{tag}"),
        "HARNESS": "mock",
        "POLICY_URL": policy_url(),
        "CAPABILITY_GATEWAY_URL": "http://127.0.0.1:1",
        "OTEL_EXPORTER_OTLP_ENDPOINT": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"),
    } | extra


# ---------------- 阶段 A：灰度与回滚 ----------------


def served_versions(runtime: str, subjects: list[str]) -> Counter[str]:
    """以每个主体对话一次，统计实际服务的版本（取 RUN_CREATED 事件中的 agent_version）。"""
    counts: Counter[str] = Counter()
    with httpx.Client(base_url=runtime, timeout=30) as c:
        for s in subjects:
            h = {"X-AI-Subject": s}
            body = {"agent_id": "gray-demo", "messages": [{"role": "user", "content": "hi"}], "stream": False}
            run_id = c.post("/v1/messages", json=body, headers=h).json()["run_id"]
            counts[c.get(f"/v1/runs/{run_id}/events", headers=h).json()[0]["payload"]["agent_version"]] += 1
    return counts


def phase_a() -> None:
    print("== 阶段 A：经 Console 灰度放量与一键回滚（mock Harness）")
    with (
        service("runtime-a", "runtime.main:app", "services/runtime/src", 18080, runtime_env("a")) as runtime,
        service(
            "console",
            "console_api.main:app",
            "services/console-api/src",
            18081,
            {"CONSOLE_DATABASE_URL": f"sqlite+aiosqlite:///{_tmp}/console.db", "RUNTIME_ADMIN_URL": runtime, "CONSOLE_CLIENT_SECRET": ""},
        ) as console,
        httpx.Client(base_url=console, headers=TADMIN, timeout=30) as c,
    ):
        for v in ("1", "2"):
            assert c.post("/agents", json={"agent_id": "gray-demo", "version": v, "system_prompt": f"v{v}"}).status_code == 201
        check(c.post("/agents/gray-demo/versions/1/publish", json={"eval_passed": True}).status_code == 200, "Console 发布 v1（全量）")
        r = c.post(
            "/agents/gray-demo/versions/2/publish", json={"eval_passed": True, "canary_percent": 10, "canary_allowlist": ["human:vip"]}
        )
        check(r.status_code == 200, "Console 发布 v2（灰度 10%，白名单 human:vip）")
        subjects = [f"human:user{i}" for i in range(200)]
        dist = served_versions(runtime, subjects)
        print(f"  200 个主体的版本分布（灰度 10%）：{dict(dist)}")
        check(8 <= dist["2"] <= 34, "约 10% 的主体进入 v2")
        check(served_versions(runtime, ["human:vip"]) == Counter({"2": 1}), "白名单主体固定进入 v2")
        check(c.put("/agents/gray-demo/versions/2/canary", json={"percent": 50}).status_code == 200, "Console 放量到 50%")
        dist50 = served_versions(runtime, subjects)
        print(f"  200 个主体的版本分布（灰度 50%）：{dict(dist50)}")
        check(70 <= dist50["2"] <= 130 and dist50["2"] > dist["2"], "放量后约一半主体进入 v2，且原灰度主体仍在 v2")
        r = c.post("/agents/gray-demo/versions/1/rollback")
        check(r.status_code == 200 and r.json()["retired"] == ["2"], "Console 一键回滚到 v1")
        after = served_versions(runtime, subjects + ["human:vip"])
        print(f"  回滚后的版本分布：{dict(after)}")
        check(after == Counter({"1": 201}), "回滚后全部主体（含白名单）回到 v1")
        actions = [a["action"] for a in c.get("/audit").json()]
        check(
            ["agent.publish", "agent.publish", "agent.canary", "agent.rollback"] == [a for a in actions if a != "agent.create"],
            "Console 审计记录完整",
        )


# ---------------- 阶段 B：门户登录、对话与反馈 ----------------


def pkce_login() -> dict[str, Any]:
    """模拟门户的授权码 + PKCE 登录（与 apps/portal-web/src/auth.ts 相同的请求参数），返回令牌响应。"""
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    redirect = "http://localhost:5173/callback"
    q = {
        "response_type": "code",
        "client_id": "portal-web",
        "redirect_uri": redirect,
        "scope": "openid",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    with httpx.Client(timeout=15, follow_redirects=False) as c:

        def cookies() -> dict[str, str]:
            # Keycloak 的会话 Cookie 带 Secure：浏览器把 localhost 视为安全上下文照常发送，httpx 在 http 下不发，这里显式带上
            return {"Cookie": "; ".join(f"{k}={v}" for k, v in c.cookies.items())}

        page = c.get(f"{REALM}/protocol/openid-connect/auth?{urlencode(q)}")
        m = re.search(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', page.text)
        assert m, f"未找到 Keycloak 登录表单（HTTP {page.status_code}）"
        r = c.post(html.unescape(m.group(1)), data={"username": DEV_USER, "password": DEV_PASSWORD, "credentialId": ""}, headers=cookies())
        assert r.status_code in (302, 303), f"登录未跳转（HTTP {r.status_code}）"
        loc = urlparse(r.headers["location"])
        params = parse_qs(loc.query)
        assert f"{loc.scheme}://{loc.netloc}{loc.path}" == redirect and params["state"] == [state], "回跳地址或 state 不符"
        no_verifier = c.post(
            f"{REALM}/protocol/openid-connect/token",
            data={"grant_type": "authorization_code", "client_id": "portal-web", "code": params["code"][0], "redirect_uri": redirect},
        )
        check(no_verifier.status_code == 400, "缺少 code_verifier 的换取请求被 Keycloak 拒绝（PKCE 生效）")
        # 缺 verifier 的失败尝试会让授权码作废：重新走一遍授权
        page = c.get(f"{REALM}/protocol/openid-connect/auth?{urlencode(q)}", headers=cookies())
        loc = urlparse(page.headers.get("location", ""))  # 已有 SSO 会话：直接带新授权码回跳
        code = parse_qs(loc.query)["code"][0]
        tok = c.post(
            f"{REALM}/protocol/openid-connect/token",
            data={
                "grant_type": "authorization_code",
                "client_id": "portal-web",
                "code": code,
                "redirect_uri": redirect,
                "code_verifier": verifier,
            },
        )
        tok.raise_for_status()
        return dict(tok.json())


def client_token(client_id: str) -> str:
    r = httpx.post(
        f"{REALM}/protocol/openid-connect/token",
        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": f"{client_id}-dev-secret"},
        timeout=10,
    )
    r.raise_for_status()
    return str(r.json()["access_token"])


def langfuse_scores(trace_id: str, timeout_s: float = 90) -> list[dict[str, Any]]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = httpx.get(f"{LANGFUSE}/api/public/traces/{trace_id}", headers=LF_AUTH, timeout=10)
        if r.status_code == 200 and r.json().get("scores"):
            return list(r.json()["scores"])
        time.sleep(3)
    return []


def phase_b() -> None:
    import jwt

    print("== 阶段 B：门户 OIDC（PKCE）登录 → 对话 → 反馈进 Langfuse")
    auth = {
        "AUTH_REQUIRED": "true",
        "JWKS_URL": f"{REALM}/protocol/openid-connect/certs",
        "JWT_ISSUER": REALM,
        "JWT_AUDIENCE": "ai-runtime",
        "OBSERVE_ADAPTER_URL": "http://127.0.0.1:18084",
    }
    adapter_env = {"LANGFUSE_HOST": LANGFUSE, "LANGFUSE_PUBLIC_KEY": LF_PK, "LANGFUSE_SECRET_KEY": LF_SK}
    with (
        service("observe", "observe_adapter.main:app", "services/observe-adapter/src", 18084, adapter_env),
        service("runtime-b", "runtime.main:app", "services/runtime/src", 18090, runtime_env("b", HARNESS="litellm", **auth)) as runtime,
        httpx.Client(base_url=runtime, timeout=180) as rt,
    ):
        admin = {"Authorization": f"Bearer {client_token('console-api')}"}
        agent = {
            "agent_id": "portal-demo",
            "version": "1",
            "harness": "litellm",
            "model_alias": ALIAS,
            "system_prompt": "你是门户助手，回答简短。",
        }
        assert rt.post("/admin/agents", json=agent, headers=admin).status_code == 201
        assert rt.post("/admin/agents/portal-demo/versions/1/publish", json={"eval_passed": True}, headers=admin).status_code == 200

        tokens = pkce_login()
        claims = jwt.decode(tokens["access_token"], options={"verify_signature": False})
        check(claims.get("preferred_username") == DEV_USER and claims.get("tenant") == "default", "PKCE 登录拿到 alice 的令牌（含租户）")
        user = {"Authorization": f"Bearer {tokens['access_token']}"}
        check(rt.get("/v1/agents").status_code == 401, "未登录不能列出 Agent")
        agents = [a["agent_id"] for a in rt.get("/v1/agents", headers=user).json()]
        check("portal-demo" in agents, "登录后可列出租户内的 Agent")
        body = {"agent_id": "portal-demo", "messages": [{"role": "user", "content": "用一句话介绍你自己。"}], "stream": False}
        r = rt.post("/v1/messages", json=body, headers=user)
        reply = r.json().get("content", "") if r.status_code == 200 else ""
        print(f"  回复：{reply[:80]!r}")
        check(r.status_code == 200 and bool(reply.strip()), "门户用户对话成功（真实模型）")
        run_id = r.json()["run_id"]
        check(rt.get(f"/v1/runs/{run_id}", headers=user).json()["subject"] == f"human:{claims['sub']}", "Run 记为人员身份 human:<sub>")

        app = {"Authorization": f"Bearer {client_token('agent-demo')}"}
        check(rt.post(f"/v1/runs/{run_id}/feedback", json={"score": 1}, headers=app).status_code == 403, "非发起人不能反馈")
        fb = rt.post(f"/v1/runs/{run_id}/feedback", json={"score": -1, "comment": "太笼统", "correction": "我是门户助手。"}, headers=user)
        check(fb.status_code == 200, "点踩并提交修正")
    trace_id = fb.json().get("trace_id", "") if fb.status_code == 200 else ""
    scores = langfuse_scores(trace_id)
    mine = [s for s in scores if s.get("name") == "user_feedback"]
    print(f"  Langfuse Trace {trace_id} 上的评分：{[(s.get('name'), s.get('value'), s.get('comment')) for s in mine]}")
    check(bool(mine) and mine[0].get("value") == -1 and mine[0].get("comment") == "太笼统", "反馈在 Langfuse 对应 Trace 上可见")


def main() -> int:
    phase_a()
    phase_b()
    print("S3 端到端验证：" + ("全部通过" if not failures else f"{len(failures)} 项失败"))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())

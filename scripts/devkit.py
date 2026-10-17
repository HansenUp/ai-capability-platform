"""本机联调与验证脚本共用的小工具：Keycloak 登录（授权码 + PKCE，与门户相同的请求参数）、Client Credentials 令牌、
Langfuse 公共 API。口令与密钥均为本机 compose 的开发值（services/identity/realm-export.json），只在本机有效。"""

from __future__ import annotations

import base64
import hashlib
import html
import os
import re
import secrets
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

KEYCLOAK = os.environ.get("KEYCLOAK_URL", "http://localhost:8180")
REALM = f"{KEYCLOAK}/realms/ai-platform"
TOKEN_URL = f"{REALM}/protocol/openid-connect/token"
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

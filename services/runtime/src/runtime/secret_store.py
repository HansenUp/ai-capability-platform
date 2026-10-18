"""敏感配置项的解析（S2.4）：环境变量只保留非敏感项，密钥从 Vault 或挂载文件注入。

解析顺序（先命中者生效）：

1. Vault KV v2：设置了 ``VAULT_ADDR`` 时，用 ``VAULT_TOKEN``（或 ``VAULT_TOKEN_FILE``）读取
   ``{VAULT_KV_MOUNT}/data/{VAULT_SECRET_PATH}``，键名即下方 ``SECRET_NAMES`` 中的变量名。
   本机 compose 用这种方式（Vault dev 服务器 + 初始化任务写入开发密钥）。
2. 挂载文件：``<NAME>_FILE`` 指向的文件内容（去掉首尾空白）。Helm 用这种方式：
   Vault Agent Injector 把密钥渲染到 ``/vault/secrets/<name>``，容器只拿到文件路径。
3. 环境变量 ``<NAME>``：只允许开发环境使用；``RUNTIME_ENV=prod`` 时由 ``config.validate_for_production``
   拒绝启动（见 ``source``）。
4. 代码默认值（开发用的公开值，如本机 LiteLLM 主密钥）。

Vault 只在进程启动时读一次（模块级缓存）；读失败直接抛错，让 Runtime 在启动阶段失败，
而不是带着开发默认值静默运行。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import httpx

# Runtime 认作密钥的配置项：只能经 Vault / 挂载文件注入（生产），不得写进 Helm values 或明文环境变量
SECRET_NAMES = (
    "DATABASE_URL",
    "MODEL_GATEWAY_API_KEY",
    "CLAUDE_SDK_AUTH_TOKEN",
    "HOOK_DIGEST_KEY",
    "CREDENTIAL_ENC_KEY",
    "REDIS_URL",
    "RUNTIME_GATEWAY_CLIENT_SECRET",  # 服务客户端 ai-runtime-svc 的密钥（S8，调用能力网关的服务令牌）
    "POLICY_SERVICE_TOKEN",  # 调用策略服务的服务间共享令牌（S8 评审 #7）
)
VAULT_TIMEOUT_S = 5.0  # 读取 Vault 的超时（秒）


class SecretError(RuntimeError):
    """密钥来源配置错误或读取失败；启动阶段抛出，阻止 Runtime 以错误的凭据运行。"""


def _vault_token() -> str:
    """Vault 令牌：优先 VAULT_TOKEN，其次 VAULT_TOKEN_FILE 指向的文件。"""
    token = os.environ.get("VAULT_TOKEN")
    if token:
        return token
    path = os.environ.get("VAULT_TOKEN_FILE")
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    raise SecretError("已设置 VAULT_ADDR，但未提供 VAULT_TOKEN 或 VAULT_TOKEN_FILE")


@lru_cache(maxsize=1)
def vault_data() -> dict[str, str]:
    """读取 Vault KV v2 中 Runtime 的密钥（未设置 VAULT_ADDR 时返回空字典）。结果按进程缓存。"""
    addr = os.environ.get("VAULT_ADDR")
    if not addr:
        return {}
    mount = os.environ.get("VAULT_KV_MOUNT", "secret").strip("/")
    path = os.environ.get("VAULT_SECRET_PATH", "ai-platform/runtime").strip("/")
    url = f"{addr.rstrip('/')}/v1/{mount}/data/{path}"
    try:
        r = httpx.get(url, headers={"X-Vault-Token": _vault_token()}, timeout=VAULT_TIMEOUT_S)
    except httpx.HTTPError as e:
        raise SecretError(f"读取 Vault 失败（{url}）：{type(e).__name__}") from e
    if r.status_code != 200:
        raise SecretError(f"读取 Vault 失败（{url}）：HTTP {r.status_code}")
    data = r.json().get("data", {}).get("data", {})
    return {k: str(v) for k, v in data.items() if k in SECRET_NAMES and v not in (None, "")}


def source(name: str) -> str:
    """返回该密钥的实际来源：vault | file | env | default。"""
    if name in vault_data():
        return "vault"
    if os.environ.get(f"{name}_FILE"):
        return "file"
    if os.environ.get(name):
        return "env"
    return "default"


def get(name: str, default: str | None = None) -> str | None:
    """按 Vault → 挂载文件 → 环境变量 → 默认值的顺序解析一个密钥。"""
    if name not in SECRET_NAMES:
        raise KeyError(f"{name} 不是登记的密钥项（见 SECRET_NAMES）")
    data = vault_data()
    if name in data:
        return data[name]
    file_path = os.environ.get(f"{name}_FILE")
    if file_path:
        try:
            return Path(file_path).read_text(encoding="utf-8").strip()
        except OSError as e:
            raise SecretError(f"{name}_FILE 指向的文件不可读：{file_path}") from e
    return os.environ.get(name) or default

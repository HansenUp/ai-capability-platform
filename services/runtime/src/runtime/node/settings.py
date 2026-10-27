"""节点代理的配置（S9.8，D148）。生效优先级：环境变量 > 代码默认值；每次 ``load()`` 现读环境变量（便于测试）。

- ``CONSOLE_URL``：Console API 地址；未配置时节点代理不启动（单机开发）。
- ``NODE_TOKEN_URL`` / ``NODE_CLIENT_ID``（默认 ``ai-node-svc``）/ ``NODE_CLIENT_SECRET``：以 client credentials 换取节点服务令牌，
  调 Console ``/nodes/*`` 时带 ``Authorization: Bearer``。密钥解析顺序同 runtime.secret_store：已登记为密钥项则经
  secret_store（Vault → ``*_FILE`` → 环境变量），未登记时 ``NODE_CLIENT_SECRET_FILE`` → ``NODE_CLIENT_SECRET``。
- ``NODE_CONSOLE_TOKEN``：开发态静态节点令牌（与 Console 的 ``CONSOLE_NODE_TOKEN`` 同值），仅在未配置令牌端点时使用。
- ``NODE_POLL_INTERVAL_S``：心跳 / 拉取指令 / 刷新吊销列表的周期（默认 30 秒；吊销生效时延 ≤ 该周期）。
- ``NODE_LABELS``：节点组标签（逗号分隔，默认 general）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from runtime import secret_store


def _secret(name: str) -> str | None:
    if name in secret_store.SECRET_NAMES:
        return secret_store.get(name) or None
    path = os.environ.get(f"{name}_FILE")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip() or None
        except OSError as e:
            raise secret_store.SecretError(f"{name}_FILE 指向的文件不可读：{path}") from e
    return os.environ.get(name) or None


@dataclass(frozen=True)
class NodeSettings:
    console_url: str | None = field(default_factory=lambda: os.environ.get("CONSOLE_URL") or None)
    token_url: str | None = field(default_factory=lambda: os.environ.get("NODE_TOKEN_URL") or None)
    client_id: str = field(default_factory=lambda: os.environ.get("NODE_CLIENT_ID", "ai-node-svc"))
    client_secret: str | None = field(default_factory=lambda: _secret("NODE_CLIENT_SECRET"), repr=False)
    static_token: str | None = field(default_factory=lambda: _secret("NODE_CONSOLE_TOKEN"), repr=False)
    poll_interval_s: float = field(default_factory=lambda: float(os.environ.get("NODE_POLL_INTERVAL_S", "30")))
    labels: tuple[str, ...] = field(
        default_factory=lambda: tuple(x for x in os.environ.get("NODE_LABELS", "general").split(",") if x) or ("general",)
    )
    timeout_s: float = field(default_factory=lambda: float(os.environ.get("NODE_HTTP_TIMEOUT_S", "10")))


def load() -> NodeSettings:
    return NodeSettings()


def problems(ns: NodeSettings, runtime_env: str) -> list[str]:
    """生产要求：配置了 Console 时必须以 ai-node-svc 服务令牌访问（令牌端点 + 密钥），不得使用开发静态令牌。"""
    out: list[str] = []
    if runtime_env == "prod" and ns.console_url:
        if not ns.token_url or not ns.client_secret:
            out.append("CONSOLE_URL 已配置但缺少 NODE_TOKEN_URL / NODE_CLIENT_SECRET（节点须以 ai-node-svc 服务令牌访问 Console）")
        if ns.static_token:
            out.append("NODE_CONSOLE_TOKEN 是开发态静态令牌，生产禁止使用")
    return out

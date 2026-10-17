"""运行配置：全部来自环境变量，每次请求现读（便于测试用 monkeypatch 切换，无需重启进程）。

| 变量 | 说明 | 默认 |
|---|---|---|
| ``MCP_TICKET_JWKS_URL`` | IdP 的 JWKS 地址（非开发模式必填） | 无 |
| ``MCP_TICKET_ISSUER`` | 令牌签发者 iss（非开发模式必填） | 无 |
| ``MCP_TICKET_AUDIENCE`` | 令牌受众 aud（令牌里可以是字符串或列表） | ``mcp-ticket`` |
| ``MCP_TICKET_TRUSTED_AZP`` | 可信的签发客户端（逗号分隔）；只有它们签发的令牌才信任 ``X-AI-Act`` | ``capability-gateway`` |
| ``MCP_TICKET_DEV_AUTH`` | 为 ``true`` 时接受 ``X-AI-Dev-*`` 开发头代替令牌（仅本机开发） | ``false`` |
| ``MCP_TICKET_DATA`` | mock 工单数据文件（JSON 数组） | 包内 ``data/tickets.json`` |
| ``MCP_TICKET_DB`` | SQLite 文件（状态覆盖 + 幂等记录） | 系统临时目录下 ``mcp-ticket.db`` |
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DATA = Path(__file__).parent / "data" / "tickets.json"


def _csv(value: str) -> frozenset[str]:
    return frozenset(p.strip() for p in value.split(",") if p.strip())


@dataclass(frozen=True)
class Settings:
    """一次请求内使用的配置快照。"""

    jwks_url: str | None
    issuer: str | None
    audience: str
    trusted_azp: frozenset[str]
    dev_auth: bool
    data_path: Path
    db_path: Path


def get_settings() -> Settings:
    """从环境变量读取配置。"""
    env = os.environ
    return Settings(
        jwks_url=env.get("MCP_TICKET_JWKS_URL") or None,
        issuer=env.get("MCP_TICKET_ISSUER") or None,
        audience=env.get("MCP_TICKET_AUDIENCE") or "mcp-ticket",
        trusted_azp=_csv(env.get("MCP_TICKET_TRUSTED_AZP", "capability-gateway")),
        dev_auth=env.get("MCP_TICKET_DEV_AUTH", "").strip().lower() == "true",
        data_path=Path(env.get("MCP_TICKET_DATA") or DEFAULT_DATA),
        db_path=Path(env.get("MCP_TICKET_DB") or Path(tempfile.gettempdir()) / "mcp-ticket.db"),
    )

"""dsh（deepseek-ai/deepseek-harness，npm ``@deepseek-ai/dsh``，MIT）的 ACP profile：``dsh --profile acp``。

S6 阶段 0 spike（dsh 0.2.0-rc.2，Node 24）实测：
- ``DSH_HOME`` 指向临时目录即得到全新的 acp profile；``--patch`` 叠加 ``cordis.patch.yml``（本包内）可把模型路由到
  shim、禁用内置工具与数据出口插件；禁用后模型请求里只剩 ``mcp__platform__<MCP 工具名>``；
- ``session/new.mcpServers`` 接受 HTTP 条目；MCP ``tools/call`` 不带模型侧 tool_call id（桥按"工具名 + 入参"认领）；
- MCP 工具调用**不发** ``session/request_permission``（没有第二道闸；平台工具本身就是唯一可调的工具，
  shim 的工具白名单仍失败关闭）；
- 没有 goose 那样的扩展列表接口，开会话后检查不适用（隔离靠 patch + shim 白名单）；
- ``session/cancel`` 后 prompt 以 ``stopReason=cancelled`` 结束，挂起的 MCP 调用收到 ``notifications/cancelled``。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from ..acp.bridge import BaseAcpProfile, LaunchContext, cached_agent_version, isolated_env
from ..acp.shim import PLACEHOLDER_MODEL

PATCH = Path(__file__).with_name("cordis.patch.yml")
DEFAULT_BIN = Path.home() / ".local" / "share" / "ai-platform" / "dsh" / "node_modules" / ".bin" / "dsh"


def dsh_binary() -> str | None:
    """dsh 启动脚本：``DSH_BIN`` 环境变量 > 平台约定的安装位置 > PATH。"""
    for cand in (os.environ.get("DSH_BIN"), str(DEFAULT_BIN), shutil.which("dsh")):
        if cand and os.path.isfile(cand):
            return cand
    return None


def node_binary() -> str | None:
    """dsh 需要的 node：``DSH_NODE`` 环境变量 > PATH。"""
    for cand in (os.environ.get("DSH_NODE"), shutil.which("node")):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def package_version(binary: str) -> str:
    """从 dsh 启动脚本的 realpath 向上找 ``@deepseek-ai/dsh/package.json``，读 version（ACP initialize 的
    agentInfo.version 是 ACP 适配层自己的版本 0.0.1，不是 dsh 的发布版本）。"""
    for d in Path(os.path.realpath(binary)).parents:
        pkg = d / "package.json"
        if pkg.is_file():
            data = json.loads(pkg.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("name") == "@deepseek-ai/dsh":
                return str(data.get("version") or "")
    return ""


class DshProfile(BaseAcpProfile):
    name = "dsh"

    def agent_version(self) -> str:
        return cached_agent_version(dsh_binary(), package_version)

    def command(self, ctx: LaunchContext) -> list[str]:
        binary, node = dsh_binary(), node_binary()
        if binary is None or node is None:
            raise FileNotFoundError("dsh or node not found (set DSH_BIN / DSH_NODE)")
        return [node, os.path.realpath(binary), "--profile", "acp", "--patch", str(PATCH)]

    def env(self, ctx: LaunchContext) -> dict[str, str]:
        node = node_binary()
        env = isolated_env(ctx, f"{os.path.dirname(node)}:/usr/bin:/bin" if node else "/usr/bin:/bin")
        env.update(
            {
                "DSH_HOME": str(ctx.root / "dsh"),  # profile / 会话持久化 / 存储全部落在临时目录
                "DSH_TELEMETRY_MODE": "DISABLED",
                "DSH_TELEMETRY_DISABLED": "1",  # dsh 官方退出开关（见 dsh patch 注释）
                "DSH_TELEMETRY_OTLP_URL": "http://127.0.0.1:9/v1/logs",
                "DSH_PERMISSION_MODE": "read-only",
                "AI_SHIM_BASE_URL": ctx.openai_base_url,
                "AI_SHIM_TOKEN": ctx.token,  # 每会话 bearer，不是平台模型网关的密钥
                "AI_MODEL_ALIAS": PLACEHOLDER_MODEL,
                "NODE_OPTIONS": "--max-old-space-size=512",
                "NODE_USE_ENV_PROXY": "1",  # Node 24 的 fetch 也走黑洞代理（只放行本机回环）
            }
        )
        return env

    def model_tool_name(self, wire: str) -> str:
        return f"mcp__platform__{wire}"

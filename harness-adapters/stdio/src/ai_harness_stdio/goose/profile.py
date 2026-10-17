"""goose（aaif-goose/goose，Homebrew ``block-goose-cli``，Apache-2.0）的 ACP profile：``goose acp``。

S6 阶段 0 spike（goose 1.54.0）实测：
- ``initialize`` 报告 ``mcpCapabilities.http=true``；``session/new.mcpServers`` 接受 HTTP 条目（含 Authorization 头），
  扩展名 ``platform``，模型看到的工具名为 ``platform__<MCP 工具名>``；
- 全新 ``GOOSE_PATH_ROOT`` 下 goose 仍自动加载 7 个内置 platform 扩展（developer=shell/文件写、apps、summon、
  extensionmanager、skills、analyze、tom）——config.yaml 里 ``extensions: {}`` 不能阻止；
  ``_goose/unstable/session/extensions/list`` / ``.../remove``（参数 ``extensionKey``）可用，移除后模型请求只剩平台工具；
- ``GOOSE_MODE=approve`` 下 MCP 工具调用前发 ``session/request_permission``（toolCallId = 模型侧 tool_call id），
  MCP ``tools/call`` 的 ``_meta.agent-tool-call-request-id`` 也带同一 id；
- ``GOOSE_DISABLE_SESSION_NAMING`` 须为 ``true``（``1`` 无效，仍会多一次起标题的模型调用）；
- ``session/cancel`` 后 prompt 以 ``stopReason=cancelled`` 结束，挂起的 MCP 调用收到 ``notifications/cancelled``。

开会话后检查（失败关闭）：列出扩展，移除 ``platform`` 以外的全部扩展，再列一次——只剩指向本桥工具代理的
``platform`` MCP 扩展才算通过，否则 IsolationError。approve 模式的权限请求由桥只对平台工具放行（第二道闸）。
"""

from __future__ import annotations

import os
import shutil
from typing import Any

from ..acp.bridge import BaseAcpProfile, IsolationError, LaunchContext, cached_agent_version, isolated_env, version_from_cli
from ..acp.jsonrpc import JsonRpcConnection, JsonRpcError, as_object
from ..acp.shim import PLACEHOLDER_MODEL
from ..acp.toolproxy import MCP_SERVER_NAME

EXT_LIST = "_goose/unstable/session/extensions/list"
EXT_REMOVE = "_goose/unstable/session/extensions/remove"
DEFAULT_BIN = "/opt/homebrew/bin/goose"


def goose_binary() -> str | None:
    """goose 可执行文件：``GOOSE_BIN`` 环境变量 > PATH > Homebrew 默认位置。"""
    for cand in (os.environ.get("GOOSE_BIN"), shutil.which("goose"), DEFAULT_BIN):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


class GooseProfile(BaseAcpProfile):
    name = "goose"

    def agent_version(self) -> str:
        """``goose --version``（输出如 `` 1.54.0``）；与 ACP initialize 的 agentInfo.version 一致。"""
        return cached_agent_version(goose_binary(), version_from_cli)

    def command(self, ctx: LaunchContext) -> list[str]:
        binary = goose_binary()
        if binary is None:
            raise FileNotFoundError("goose binary not found (set GOOSE_BIN or install block-goose-cli)")
        return [binary, "acp"]

    def env(self, ctx: LaunchContext) -> dict[str, str]:
        env = isolated_env(ctx)
        env.update(
            {
                "GOOSE_PATH_ROOT": str(ctx.root / "goose"),  # 配置 / 会话库 / 日志全部落在临时目录
                "GOOSE_PROVIDER": "openai",
                "GOOSE_MODEL": PLACEHOLDER_MODEL,  # shim 一律改写为平台别名
                "OPENAI_HOST": ctx.base_url,
                "OPENAI_API_KEY": ctx.token,  # 每会话 bearer，不是平台模型网关的密钥
                "GOOSE_MODE": "approve",
                "GOOSE_DISABLE_SESSION_NAMING": "true",
                "GOOSE_AUTO_COMPACT_THRESHOLD": "0",
                "GOOSE_DISABLE_KEYRING": "1",
                "GOOSE_TELEMETRY_ENABLED": "false",
                "GOOSE_TELEMETRY_OFF": "1",
                "GOOSE_CLI_SHOW_THINKING": "false",
            }
        )
        return env

    async def after_session_new(self, rpc: JsonRpcConnection, session_id: str, ctx: LaunchContext) -> None:
        try:
            listed = await rpc.request(EXT_LIST, {"sessionId": session_id})
            for key in _keys(listed):
                if key != MCP_SERVER_NAME:
                    await rpc.request(EXT_REMOVE, {"sessionId": session_id, "extensionKey": key})
            listed = await rpc.request(EXT_LIST, {"sessionId": session_id})
        except JsonRpcError as exc:
            raise IsolationError(f"goose extension check failed: {exc}") from exc
        exts = [e for e in (listed or {}).get("extensions") or [] if isinstance(e, dict)]
        bad = [str(e.get("extensionKey")) for e in exts if not _is_platform(e, ctx)]
        if bad or len(exts) != 1:
            raise IsolationError(
                f"goose session still has non-platform extensions after removal: {bad or [str(e.get('extensionKey')) for e in exts]}"
            )


def _keys(listed: Any) -> list[str]:
    return [str(e.get("extensionKey")) for e in (listed or {}).get("extensions") or [] if isinstance(e, dict)]


def _is_platform(entry: dict[str, Any], ctx: LaunchContext) -> bool:
    ext = as_object(entry.get("extension"))
    server = as_object(ext.get("server"))
    return entry.get("extensionKey") == MCP_SERVER_NAME and ext.get("type") == "mcp" and server.get("url") == ctx.mcp_url

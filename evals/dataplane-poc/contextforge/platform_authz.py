"""ContextForge 原生插件（POC 候选 3）：TOOL_PRE_INVOKE 时同步回调平台判定端点，并整形转发给上游的身份头。

- 判定：POST {authz_url} {"server", "tool", "subject", "tenant", "agent"}（带 X-Gateway-Authz-Token: config.authz_token）；
  主体与租户取自透传给上游的 OBO 令牌（X-Upstream-Authorization，ContextForge 转发时改名为 Authorization）；
  server 由工具名的网关前缀经 config.gateway_servers 映射为平台 MCP Server 登记名（判定端点据此校验工具归属，
  映射不到时为空串、按 deny）。非 200、超时、异常一律拒绝（失败关闭）。
- 身份头：删除客户端带来的全部 X-AI-* 头，按 X-AI-Agent 重写 X-AI-Act、保留 X-AI-Run-Id。
- 工具名：ContextForge 会给上游工具加网关前缀（如 poc-upstream-ticket-search），用 config.tool_names 映射回能力目录里的名字。
配置见 plugins.yaml；需 plugin_settings.fail_on_plugin_error: true，插件自身异常也按拒绝处理。
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
from cpex.framework import (
    HttpHeaderPayload,
    Plugin,
    PluginConfig,
    PluginContext,
    PluginViolation,
    ToolPreInvokePayload,
    ToolPreInvokeResult,
)


def _claims(token: str) -> dict[str, Any]:
    """读取 OBO 令牌 claims（验签由上游完成；这里只取主体与租户用于判定）。"""
    try:
        seg = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
    except (IndexError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class PlatformAuthzPlugin(Plugin):
    """平台转发前判定 + 身份头整形。"""

    def __init__(self, config: PluginConfig) -> None:
        super().__init__(config)
        cfg = self._config.config or {}
        self._url = str(cfg["authz_url"])
        self._names: dict[str, str] = dict(cfg.get("tool_names") or {})
        self._servers: dict[str, str] = dict(cfg.get("gateway_servers") or {})
        self._sep = str(cfg.get("separator", "-"))  # ContextForge 工具名中网关前缀的分隔符（GATEWAY_TOOL_NAME_SEPARATOR）
        token = str(cfg.get("authz_token") or "")
        self._auth_headers = {"X-Gateway-Authz-Token": token} if token else {}
        self._client = httpx.AsyncClient(timeout=float(cfg.get("timeout_s", 2.0)))

    def _deny(self, decision: str, tool: str) -> ToolPreInvokeResult:
        return ToolPreInvokeResult(
            continue_processing=False,
            violation=PluginViolation(
                reason="denied by platform policy",
                description=f"tool {tool} rejected before forwarding (decision={decision})",
                code="PLATFORM_AUTHZ_" + decision.upper(),
                details={"decision": decision},
            ),
        )

    async def tool_pre_invoke(self, payload: ToolPreInvokePayload, context: PluginContext) -> ToolPreInvokeResult:
        headers = payload.headers.model_dump() if payload.headers else {}
        lower = {k.lower(): v for k, v in headers.items()}
        tool = self._names.get(payload.name, payload.name)
        server = next((s for prefix, s in self._servers.items() if payload.name.startswith(prefix + self._sep)), "")
        auth = lower.get("x-upstream-authorization") or lower.get("authorization") or ""
        token = auth.split(" ", 1)[1].strip() if auth.lower().startswith("bearer ") else ""
        claims = _claims(token)
        agent = lower.get("x-ai-agent") or None
        sub, tenant = str(claims.get("sub") or ""), str(claims.get("tenant") or "")
        body = {"server": server, "tool": tool, "subject": sub, "tenant": tenant, "agent": agent}
        try:
            r = await self._client.post(self._url, json=body, headers=self._auth_headers)
            decision = (
                str(r.json().get("decision", "error")) if r.headers.get("content-type", "").startswith("application/json") else "error"
            )
            allowed = r.status_code == 200
        except (httpx.HTTPError, ValueError):
            decision, allowed = "error", False
        if not allowed:
            return self._deny(decision if decision in ("deny", "error") else "error", tool)
        shaped = {k: v for k, v in headers.items() if not k.lower().startswith("x-ai-")}
        if agent:
            shaped["X-AI-Act"] = f"agent:{agent}"
        if lower.get("x-ai-run-id"):
            shaped["X-AI-Run-Id"] = lower["x-ai-run-id"]
        context.metadata["platform_authz_decision"] = decision
        return ToolPreInvokeResult(modified_payload=payload.model_copy(update={"headers": HttpHeaderPayload(root=shaped)}))

    async def shutdown(self) -> None:
        await self._client.aclose()

"""连接器管理（S8.5 / D132）：登记、版本、激活、探活。

- ``POST /mcp/servers``：登记连接器（默认版本 "1"）；该名字尚无 active 版本时走与 activate 相同的"探活 + 写规范校验"，
  通过才激活，否则保持 inactive（登记本身成功，201）。已 active 的版本不可经登记改写（409）。
- ``GET /mcp/servers``、``GET /mcp/servers/{name}``：列出 active 版本 / 某连接器的全部版本及其工具校验结果（管理令牌或服务令牌）。
- ``PUT /mcp/servers/{name}/versions/{v}``：新增或更新一个 inactive 版本（不激活）；active 版本不可修改（409，先登记新版本再激活）。
- ``POST /mcp/servers/{name}/versions/{v}/activate``：先探活该版本，不可达或 manifest 中有不合规工具则 409，否则切为 active，
  并以此刻的 manifest 哈希作为激活基线（activated_manifest_hash）。
- ``POST /mcp/servers/{name}/probe``：立即探活 active 版本。
``/invoke`` 前由 ``require_compliant_tool`` 检查：最近一次 manifest 存在（从未探活成功时先探活一次，仍无则 503）、与激活基线一致
（漂移 409，须重新激活）、该工具在 manifest 中且写规范合规（否则 409）。
后台每 GATEWAY_PROBE_INTERVAL_S（30 s）探活全部 active 版本：healthz + manifest（REST ``GET /tools``；MCP 协议 ``tools/list``），
记录 manifest 哈希（工具按名排序后规范化 JSON 的 sha256）与每个工具的写规范校验结果；连续 GATEWAY_PROBE_FAIL_THRESHOLD（3）次
失败置 down，``/invoke`` 对 down 的连接器返回 503；任一次成功即恢复 up。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from capability_gateway import config, state
from capability_gateway.auth import require_admin_or_service, require_gateway_admin
from capability_gateway.catalog import ConnectorTool, ConnectorVersion, normalize_protocol
from capability_gateway.errors import GatewayError, responses
from capability_gateway.spec import is_read_only, validate_tool

log = logging.getLogger("capability_gateway.connectors")
router = APIRouter()
MCP_PROTOCOL_VERSION = "2025-06-18"


# ---- MCP 协议（streamable-http）最小客户端：initialize → 请求 ----
def _rpc_result(r: httpx.Response, rid: int) -> Any:
    """解析 JSON 或 SSE 应答中 id 匹配的 JSON-RPC 结果；error 抛 ValueError。"""
    ctype = r.headers.get("content-type", "")
    messages: list[Any] = []
    if "text/event-stream" in ctype:
        for line in r.text.splitlines():
            if line.startswith("data:"):
                messages.append(json.loads(line[5:].strip()))
    else:
        body = r.json()
        messages = body if isinstance(body, list) else [body]
    for m in messages:
        if isinstance(m, dict) and m.get("id") == rid:
            if "error" in m:
                raise ValueError(f"JSON-RPC error: {m['error']}")
            return m.get("result")
    raise ValueError("no JSON-RPC response")


async def mcp_call(
    client: httpx.AsyncClient, endpoint: str, method: str, params: dict[str, Any], headers: dict[str, str] | None = None
) -> Any:
    """对 MCP streamable-http 端点完成 initialize 握手后发送一次请求，返回 result。"""
    base = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"} | (headers or {})
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "capability-gateway", "version": "1"},
        },
    }  # noqa: E501
    r = await client.post(endpoint, json=init, headers=base)
    r.raise_for_status()
    _rpc_result(r, 1)
    session = r.headers.get("mcp-session-id")
    h = base | ({"Mcp-Session-Id": session} if session else {})
    await client.post(endpoint, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h)
    r = await client.post(endpoint, json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params}, headers=h)
    r.raise_for_status()
    return _rpc_result(r, 2)


def manifest_hash(tools: list[dict[str, Any]]) -> str:
    canon = json.dumps(sorted(tools, key=lambda t: str(t.get("name"))), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


async def fetch_manifest(cv: ConnectorVersion) -> list[dict[str, Any]]:
    """健康检查 + 读取 manifest；失败抛 httpx.HTTPError / ValueError。"""
    async with httpx.AsyncClient(timeout=config.PROBE_TIMEOUT_S, transport=config.transport) as c:
        if cv.protocol == "mcp":
            result = await mcp_call(c, cv.endpoint, "tools/list", {})
            tools = result.get("tools") if isinstance(result, dict) else None
        else:
            base = cv.endpoint.rstrip("/")
            (await c.get(f"{base}/healthz")).raise_for_status()
            r = await c.get(f"{base}/tools")
            r.raise_for_status()
            body = r.json()
            tools = body.get("tools") if isinstance(body, dict) else body
    if not isinstance(tools, list) or not all(isinstance(t, dict) and t.get("name") for t in tools):
        raise ValueError("malformed tool manifest")
    return tools


def check_tools(cv: ConnectorVersion, manifest: list[dict[str, Any]]) -> list[ConnectorTool]:
    return [
        ConnectorTool(
            connector=cv.name, version=cv.version, name=str(t["name"]), spec=t, read_only=is_read_only(t), errors=validate_tool(t, manifest)
        )
        for t in manifest
    ]


async def probe(cv: ConnectorVersion) -> tuple[ConnectorVersion, list[ConnectorTool] | None]:
    """探活一个版本并写回目录；返回更新后的版本与（成功时）工具校验结果。"""
    now = time.time()
    try:
        manifest = await fetch_manifest(cv)
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, KeyError, TypeError) as e:
        failures = cv.consecutive_failures + 1
        health = "down" if failures >= config.PROBE_FAIL_THRESHOLD else cv.health
        updated = cv.model_copy(
            update={
                "consecutive_failures": failures,
                "health": health,
                "last_probe_at": now,
                "last_error": f"{type(e).__name__}: {e}"[:512],
            }
        )
        await state.catalog.save_probe(updated, None)
        return updated, None
    tools = check_tools(cv, manifest)
    digest = manifest_hash(manifest)
    updated = cv.model_copy(
        update={
            "consecutive_failures": 0,
            "health": "up",
            "last_probe_at": now,
            "last_error": None,
            "manifest_hash": digest,
            # 未经 activate 端点激活的 active 版本（种子 / 存量）：首次探活成功的 manifest 即激活基线（TOFU）
            "activated_manifest_hash": cv.activated_manifest_hash or (digest if cv.status == "active" else None),
        }
    )
    await state.catalog.save_probe(updated, tools)
    return updated, tools


async def probe_all() -> None:
    for cv in await state.catalog.list_connectors():
        if cv.status == "active":
            await probe(cv)


async def run_prober(interval_s: float) -> None:
    while True:
        try:
            await probe_all()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 —— 后台任务不能因单次失败退出
            log.warning("connector prober: %s", e)
        await asyncio.sleep(interval_s)


async def active_manifest(name: str) -> list[dict[str, Any]] | None:
    """连接器 active 版本最近一次探活成功时的 manifest（从未成功时 None）。"""
    cv = await state.catalog.active_connector(name)
    if cv is None or cv.manifest_hash is None:
        return None
    return [t.spec for t in await state.catalog.connector_tools(cv.name, cv.version)]


async def require_compliant_tool(cv: ConnectorVersion, tool: str) -> None:
    """/invoke 前的写规范与 manifest 一致性检查（第三处校验的连接器侧）：
    最近一次 manifest 须存在（从未探活成功时先同步探活一次，仍无 503）、与激活基线一致（漂移 409）、
    该工具在 manifest 中（409）且 validate_tool 无错误（409）。"""
    if cv.manifest_hash is None:
        cv, _ = await probe(cv)
        if cv.manifest_hash is None:
            raise GatewayError(503, "connector_manifest_unavailable", f"connector '{cv.name}' has no probed manifest yet")
    if cv.activated_manifest_hash is not None and cv.manifest_hash != cv.activated_manifest_hash:
        raise GatewayError(409, "manifest_drift", f"connector '{cv.name}' manifest changed since activation; re-activate the version")
    found = next((t for t in await state.catalog.connector_tools(cv.name, cv.version) if t.name == tool), None)
    if found is None:
        raise GatewayError(409, "tool_not_in_manifest", f"tool '{tool}' is not in the manifest of connector '{cv.name}'")
    if found.errors:
        raise GatewayError(409, "tool_spec_violation", "; ".join(found.errors))


# ---- API ----
class McpServerIn(BaseModel):
    """连接器登记信息。"""

    name: str = Field(min_length=1, max_length=128)
    endpoint: str = Field(min_length=1, max_length=512)  # REST 基地址或 MCP streamable-http 端点
    protocol: Literal["rest", "http", "mcp", "streamable-http"] = "streamable-http"
    auth_type: Literal["inherit", "service"] = "inherit"  # inherit：转发 OBO 令牌
    system: str = Field(default="", max_length=64)  # 所属业务系统
    version: str = Field(default="1", min_length=1, max_length=32)


class VersionIn(BaseModel):
    endpoint: str = Field(min_length=1, max_length=512)
    protocol: Literal["rest", "http", "mcp", "streamable-http"] = "streamable-http"
    auth_type: Literal["inherit", "service"] = "inherit"
    system: str | None = Field(default=None, max_length=64)  # 缺省沿用该连接器已有版本的 system


class ConnectorView(ConnectorVersion):
    tools: list[ConnectorTool] = Field(default_factory=list)


async def _view(cv: ConnectorVersion) -> ConnectorView:
    return ConnectorView(**cv.model_dump(), tools=await state.catalog.connector_tools(cv.name, cv.version))


async def _reject_if_active(name: str, version: str) -> None:
    existing = await state.catalog.get_connector(name, version)
    if existing is not None and existing.status == "active":
        raise GatewayError(409, "version_active", "active connector version is immutable; register a new version and activate it")


async def _activate_checked(name: str, version: str) -> ConnectorView:
    """探活 + 写规范校验通过后切为 active（激活与登记共用；探活不合规的版本不可发布）。"""
    cv = await state.catalog.get_connector(name, version)
    if cv is None:
        raise GatewayError(404, "unknown_connector", "unknown connector version")
    cv, tools = await probe(cv)
    if tools is None:
        raise GatewayError(409, "connector_unreachable", f"probe failed: {cv.last_error}")
    bad = [e for t in tools for e in t.errors]
    if bad:
        raise GatewayError(409, "tool_spec_violation", "; ".join(bad))
    return await _view(await state.catalog.activate(name, version))


@router.post("/mcp/servers", status_code=201, dependencies=[Depends(require_gateway_admin)], responses=responses(403, 409))
async def register_mcp(body: McpServerIn) -> ConnectorVersion:
    """登记连接器（inactive）；尚无 active 版本时探活 + 写规范校验通过才激活，否则保持 inactive。"""
    await _reject_if_active(body.name, body.version)
    cv = await state.catalog.put_connector(
        ConnectorVersion(
            name=body.name,
            version=body.version,
            endpoint=body.endpoint,
            protocol=normalize_protocol(body.protocol),
            auth_type=body.auth_type,
            system=body.system,
        )
    )
    if await state.catalog.active_connector(body.name) is None:
        try:
            return await _activate_checked(body.name, body.version)
        except GatewayError:
            return await state.catalog.get_connector(body.name, body.version) or cv  # 不可达 / 不合规：保持 inactive
    return cv


@router.get("/mcp/servers", dependencies=[Depends(require_admin_or_service)], responses=responses(401, 403))
async def list_mcp() -> list[ConnectorVersion]:
    """列出各连接器的 active 版本。"""
    return [c for c in await state.catalog.list_connectors() if c.status == "active"]


@router.get("/mcp/servers/{name}", dependencies=[Depends(require_admin_or_service)], responses=responses(401, 403, 404))
async def get_mcp(name: str) -> list[ConnectorView]:
    """某连接器的全部版本（含最近一次探活的工具与校验结果）。"""
    versions = await state.catalog.list_connectors(name)
    if not versions:
        raise GatewayError(404, "unknown_connector", "unknown connector")
    return [await _view(v) for v in versions]


@router.put("/mcp/servers/{name}/versions/{version}", dependencies=[Depends(require_gateway_admin)], responses=responses(403, 409))
async def put_version(name: str, version: str, body: VersionIn) -> ConnectorVersion:
    """新增或更新一个 inactive 版本（不激活）；active 版本不可修改（409）。"""
    await _reject_if_active(name, version)
    existing = await state.catalog.list_connectors(name)
    system = body.system if body.system is not None else (existing[0].system if existing else "")
    return await state.catalog.put_connector(
        ConnectorVersion(
            name=name,
            version=version,
            endpoint=body.endpoint,
            protocol=normalize_protocol(body.protocol),
            auth_type=body.auth_type,
            system=system,
        )
    )


@router.post(
    "/mcp/servers/{name}/versions/{version}/activate", dependencies=[Depends(require_gateway_admin)], responses=responses(403, 404, 409)
)
async def activate_version(name: str, version: str) -> ConnectorView:
    """探活通过且 manifest 全部合规后切为 active（探活不合规的版本不可发布）。"""
    return await _activate_checked(name, version)


@router.post("/mcp/servers/{name}/probe", dependencies=[Depends(require_gateway_admin)], responses=responses(403, 404))
async def probe_now(name: str) -> ConnectorView:
    """立即探活 active 版本。"""
    cv = await state.catalog.active_connector(name)
    if cv is None:
        raise GatewayError(404, "unknown_connector", "unknown connector or no active version")
    updated, _ = await probe(cv)
    return await _view(updated)

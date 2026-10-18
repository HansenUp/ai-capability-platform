"""两级能力发现的第二级（S8.2，D125）：元工具 ``search_capabilities``。

- 注入：仅当 Agent 版本快照 ``config.discovery.enabled`` 为 true 时由组装器注入（未开启时工具列表、事件日志与此前一致）。
  ``discovery.scope``（检索范围，字符串列表）与 ``discovery.max_tools``（本 Run 动态加入的工具上限，默认 5，1～20）随版本快照
  冻结，发布时校验。
- 拦截：工具代理在闸口前拦截该元工具（pre_tool hook 照常），经能力网关 ``POST /capabilities/search``（契约 4，带服务令牌与
  用户令牌）检索；结果中的新工具加入本 Run 的 ``tools_by_name``（超出上限的丢弃），写 ``CAPABILITIES_DISCOVERED``（工具规格、
  资源属性与哈希）与 TOOL_RESULT。发现的工具此后的调用照常过闸口（策略、风险、审批、幂等键）。
- 重放 / 接管：``restore`` 按事件日志中的 ``CAPABILITIES_DISCOVERED`` 恢复已发现工具，不重新检索；指纹不符的事件不恢复
  （失败关闭：模型再调该工具按 unknown tool 处理）。指纹为带密钥的 ``tools_hmac``（HOOK_DIGEST_KEY，能读写事件日志而无密钥者
  无法伪造）；评审修复前写入的旧事件只有裸 sha256 的 ``tools_hash``，仍按旧算法校验以兼容重放。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import ValidationError

from ai_contract import RunEvent, RunEventType, ToolSpec
from runtime import gateway_client
from runtime.config import settings
from runtime.state.db import AgentVersion

if TYPE_CHECKING:
    from runtime.loop.toolbroker import RunCtx

TOOL_NAME = "search_capabilities"
DEFAULT_MAX_TOOLS = 5  # 本 Run 动态加入工具的默认上限
MAX_TOOLS_CEILING = 20  # discovery.max_tools 的上限（与网关 search limit≤20 一致）
SEARCH_TIMEOUT_S = 10.0  # 一次检索的超时（秒）
MAX_QUERY_CHARS = 500  # 检索文本上限
# 不得被发现结果覆盖或新增的保留名：平台元工具与只在进程内执行的开发工具
RESERVED_NAMES = frozenset({TOOL_NAME, "read_artifact", "request_user_input", "echo"})

TOOL = ToolSpec(
    name=TOOL_NAME,
    description=(
        "按意图检索当前身份可用、但尚未加载的企业能力（第二级发现）。query 为要完成的事情的描述；"
        "检索到的工具会加入本次任务的可用工具，随后可直接调用。"
    ),
    input_schema={
        "type": "object",
        "properties": {"query": {"type": "string", "description": "要完成的事情 / 需要的能力，如“查询客户商机”"}},
        "required": ["query"],
    },
)


def _config(agent: AgentVersion) -> dict[str, Any] | None:
    cfg = (agent.config or {}).get("discovery")
    return cfg if isinstance(cfg, dict) and cfg.get("enabled") is True else None


def enabled(agent: AgentVersion) -> bool:
    """Agent 版本快照是否开启第二级发现（config.discovery.enabled 为 true）。"""
    return _config(agent) is not None


def max_tools(agent: AgentVersion) -> int:
    """本 Run 动态加入工具的上限（discovery.max_tools，默认 5）。"""
    cfg = _config(agent) or {}
    value = cfg.get("max_tools", DEFAULT_MAX_TOOLS)
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_TOOLS_CEILING else DEFAULT_MAX_TOOLS


def scope(agent: AgentVersion) -> list[str]:
    """版本快照冻结的检索范围（discovery.scope，空表示不额外限定，由网关按绑定关系过滤）。"""
    value = (_config(agent) or {}).get("scope") or []
    return [str(x) for x in value] if isinstance(value, list) else []


def validate_config(value: Any) -> str | None:
    """发布时校验 config.discovery（不合规返回原因，合规或未配置返回 None）。"""
    if value is None:
        return None
    if not isinstance(value, dict):
        return "discovery 须为对象 {enabled, scope?, max_tools?}"
    unknown = set(value) - {"enabled", "scope", "max_tools"}
    if unknown:
        return f"discovery 含未知字段：{sorted(unknown)}"
    if not isinstance(value.get("enabled"), bool):
        return "discovery.enabled 须为 true / false"
    sc = value.get("scope", [])
    if not isinstance(sc, list) or not all(isinstance(x, str) and x for x in sc):
        return "discovery.scope 须为非空字符串列表"
    mt = value.get("max_tools", DEFAULT_MAX_TOOLS)
    if not isinstance(mt, int) or isinstance(mt, bool) or not 1 <= mt <= MAX_TOOLS_CEILING:
        return f"discovery.max_tools 须为 1～{MAX_TOOLS_CEILING} 的整数"
    return None


def _canon(tools: list[dict[str, Any]], resources: dict[str, dict[str, Any]]) -> bytes:
    return json.dumps({"tools": tools, "resources": resources}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def tools_hash(tools: list[dict[str, Any]], resources: dict[str, dict[str, Any]]) -> str:
    """旧格式指纹（裸 sha256）：只用于校验评审修复前写入的事件。"""
    return hashlib.sha256(_canon(tools, resources)).hexdigest()


def tools_hmac(tools: list[dict[str, Any]], resources: dict[str, dict[str, Any]]) -> str:
    """发现结果的指纹：工具规格与资源属性规范化 JSON 的 HMAC-SHA256（密钥 HOOK_DIGEST_KEY，与 toolbroker.hmac_json 同一
    规范化与密钥），写入事件，恢复时校验。"""
    return hmac.new(settings.hook_digest_key.encode("utf-8"), _canon(tools, resources), hashlib.sha256).hexdigest()


def _fingerprint_ok(payload: dict[str, Any], tools: list[dict[str, Any]], resources: dict[str, dict[str, Any]]) -> bool:
    if "tools_hmac" in payload:
        return hmac.compare_digest(str(payload["tools_hmac"]), tools_hmac(tools, resources))
    legacy = payload.get("tools_hash")  # 旧事件兼容
    return isinstance(legacy, str) and hmac.compare_digest(legacy, tools_hash(tools, resources))


@dataclass
class Found:
    """一次检索的结论：交还模型的工具结果、要写的事件（无新工具时为 None），以及新加入的工具与其资源属性。"""

    result: dict[str, Any]
    event: dict[str, Any] | None = None
    tools: list[ToolSpec] = field(default_factory=list)
    resources: dict[str, dict[str, Any]] = field(default_factory=dict)


def _spec(item: Any) -> ToolSpec | None:
    """网关检索结果项（契约 4）→ ToolSpec；字段缺失或取值不合规的项丢弃（失败关闭）。"""
    if not isinstance(item, dict) or not item.get("id") or not item.get("name"):
        return None
    try:
        return ToolSpec(
            name=str(item["name"]),
            description=str(item.get("description") or ""),
            input_schema=item.get("input_schema") or {},
            capability_id=str(item["id"]),
            risk_level=item.get("risk_level", "read"),
            data_class=item.get("data_class", "internal"),
            supports_dry_run=bool(item.get("supports_dry_run", False)),
            requires_idempotency_key=bool(item.get("requires_idempotency_key", False)),
        )
    except (ValidationError, TypeError):
        return None


def _summary(spec: ToolSpec, decision: Any) -> dict[str, Any]:
    out = {"name": spec.name, "description": spec.description, "risk_level": spec.risk_level.value}
    if decision in ("allow", "require_approval"):
        out["decision"] = decision
    return out


async def search(rc: RunCtx, tool_input: dict[str, Any]) -> Found:
    """执行一次检索（不写日志）：返回交还模型的结果与要写的 CAPABILITIES_DISCOVERED 负载（不含 call_id）。"""
    query = tool_input.get("query", tool_input.get("intent"))
    if not isinstance(query, str) or not query.strip():
        return Found({"error": "search_capabilities requires a non-empty 'query' string"})
    query = query.strip()[:MAX_QUERY_CHARS]
    limit = max_tools(rc.agent)
    frozen_scope = scope(rc.agent)
    body: dict[str, Any] = {
        "query": query,
        "agent_id": rc.agent.agent_id,
        "agent_version": rc.agent.version,
        "agent_tenant": rc.agent.tenant_id,
        "limit": limit,
        "taint": sorted(rc.gate.taint),
    }
    if frozen_scope:
        body["scope"] = frozen_scope
    try:
        r = await gateway_client.request("POST", "/capabilities/search", user_token=rc.credential, timeout_s=SEARCH_TIMEOUT_S, json=body)
    except httpx.HTTPError as e:
        return Found({"error": f"capability search unavailable: {type(e).__name__}"})
    if r.status_code != 200:
        return Found({"error": f"capability search failed: HTTP {r.status_code}"})
    try:
        data = r.json()
        items = data.get("items", [])
        degraded = bool(data.get("degraded", False))
    except (ValueError, AttributeError):
        return Found({"error": "capability search returned a malformed response"})
    if not isinstance(items, list):
        return Found({"error": "capability search returned a malformed response"})
    room = limit - len(rc.discovered)
    added: list[ToolSpec] = []
    resources: dict[str, dict[str, Any]] = {}
    listed: list[dict[str, Any]] = []
    dropped: list[str] = []
    for item in items:
        spec = _spec(item)
        if spec is None or spec.name in RESERVED_NAMES:
            continue
        if spec.name in rc.tools_by_name or spec.name in resources:
            listed.append(_summary(rc.tools_by_name.get(spec.name, spec), item.get("decision")))  # 已可用：只告知
            continue
        if len(added) >= room:
            dropped.append(spec.name)
            continue
        added.append(spec)
        res: dict[str, Any] = {"system": str(item.get("system") or "")}
        if item.get("tenant"):
            res["tenant"] = str(item["tenant"])
        resources[spec.name] = res
        listed.append(_summary(spec, item.get("decision")))
    result: dict[str, Any] = {"tools": listed, "degraded": degraded}
    if dropped:
        result["limit_reached"] = f"本次任务最多动态加入 {limit} 个工具，已省略：{', '.join(dropped)}"
    if not added:
        return Found(result)
    tools_json = [t.model_dump(mode="json") for t in added]
    event: dict[str, Any] = {
        "query": query,
        "scope": frozen_scope,
        "tools": tools_json,
        "resources": resources,
        "tools_hmac": tools_hmac(tools_json, resources),
        "degraded": degraded,
    }
    if dropped:
        event["dropped"] = dropped
    return Found(result, event, added, resources)


def apply(rc: RunCtx, found: Found) -> None:
    """把检索新加入的工具并入本 Run（工具表、harness 工具列表追加项与闸口资源属性）。"""
    for spec in found.tools:
        if spec.name in rc.tools_by_name:
            continue
        rc.tools_by_name[spec.name] = spec
        rc.discovered.append(spec)
        rc.gate.resources[spec.name] = found.resources.get(spec.name, {})


def restore(rc: RunCtx, prior: list[RunEvent]) -> None:
    """重放 / 接管：按事件日志恢复已发现工具（不重新检索）；指纹不符或规格不合规的事件整条不恢复。"""
    for e in prior:
        if e.type != RunEventType.CAPABILITIES_DISCOVERED:
            continue
        tools_json = e.payload.get("tools") or []
        resources = e.payload.get("resources") or {}
        if not isinstance(tools_json, list) or not isinstance(resources, dict):
            continue
        if not _fingerprint_ok(e.payload, tools_json, resources):
            continue
        try:
            specs = [ToolSpec.model_validate(t) for t in tools_json]
        except ValidationError:
            continue
        apply(rc, Found({}, None, specs, resources))

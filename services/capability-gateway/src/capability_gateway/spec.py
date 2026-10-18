"""写操作规范（S8.5 / D132）：非只读工具"幂等键 + dry-run + 补偿工具"缺一不可。

三处校验：
1. 连接器探活 / 激活 / 登记：manifest 中每个工具过 ``validate_tool``，不合规的工具记录错误、不可发布（激活 409，登记保持 inactive）；
2. ``POST /capabilities``：``validate_capability``——承载连接器须有 active 版本且探活成功过（manifest 存在），工具与补偿工具都须
   在该 manifest 中且合规，不合规 409；
3. ``/invoke``：写操作非 dry-run 缺幂等键 409；工具须在最近一次 manifest 中且合规、manifest 与激活基线一致
   （connectors.require_compliant_tool）。

manifest 中工具的元数据可以放在顶层、``_meta`` 或 ``annotations`` 中：``risk_level``（缺省时看 ``annotations.readOnlyHint``，
都没有按非只读处理，失败关闭）、``supports_dry_run``、``requires_idempotency_key``、``compensate_tool``。
补偿工具本身（被同一 manifest 中其他工具的 compensate_tool 引用）免于再声明补偿工具，但同样需要幂等键与 dry-run。
暴露给模型的能力 input_schema 不得含控制字段 dry_run / idempotency_key（由 Runtime 闸口生成、网关注入）。
"""

from __future__ import annotations

from typing import Any

from capability_gateway.registry import Capability

CONTROL_FIELDS = ("dry_run", "idempotency_key")


def _meta(tool: dict[str, Any], key: str) -> Any:
    for src in (tool, tool.get("_meta") or {}, tool.get("annotations") or {}):
        if isinstance(src, dict) and key in src:
            return src[key]
    return None


def is_read_only(tool: dict[str, Any]) -> bool:
    risk = _meta(tool, "risk_level")
    if risk is not None:
        return bool(risk == "read")
    return _meta(tool, "readOnlyHint") is True


def compensators(manifest: list[dict[str, Any]]) -> set[str]:
    return {str(c) for t in manifest if (c := _meta(t, "compensate_tool"))}


def validate_tool(tool: dict[str, Any], manifest: list[dict[str, Any]]) -> list[str]:
    """校验 manifest 中的一个工具；返回错误列表（空即合规）。"""
    if is_read_only(tool):
        return []
    name = str(tool.get("name") or "")
    names = {str(t.get("name")) for t in manifest}
    errors: list[str] = []
    if _meta(tool, "requires_idempotency_key") is not True:
        errors.append(f"{name}: non-read tool must require an idempotency key")
    if _meta(tool, "supports_dry_run") is not True:
        errors.append(f"{name}: non-read tool must support dry_run")
    comp = _meta(tool, "compensate_tool")
    if comp:
        if comp not in names:
            errors.append(f"{name}: compensate tool '{comp}' is not in the same manifest")
    elif name not in compensators(manifest):
        errors.append(f"{name}: non-read tool must declare a compensate tool")
    return errors


def validate_capability(cap: Capability, manifest: list[dict[str, Any]] | None) -> list[str]:
    """校验一条能力登记；manifest 为承载连接器 active 版本最近一次探活成功的 manifest。
    没有承载连接器或从未探活成功（None）时不合规：不能抢在探活前登记能力绕过写规范。"""
    errors: list[str] = []
    if manifest is None:
        errors.append("no active connector with a probed manifest for this capability (register / activate the connector first)")
    props = cap.input_schema.get("properties") if isinstance(cap.input_schema, dict) else None
    exposed = [f for f in CONTROL_FIELDS if isinstance(props, dict) and f in props]
    if exposed:
        errors.append(f"input_schema must not expose control fields to the model: {', '.join(exposed)}")
    if cap.risk_level != "read":
        if not cap.requires_idempotency_key:
            errors.append("non-read capability must require an idempotency key")
        if not cap.supports_dry_run:
            errors.append("non-read capability must support dry_run")
        if not cap.compensate_tool and not (manifest is not None and cap.name in compensators(manifest)):
            errors.append("non-read capability must declare a compensate tool")
    if manifest is not None:
        names = {str(t.get("name")) for t in manifest}
        if cap.name not in names:
            errors.append(f"tool '{cap.name}' is not in the connector manifest")
        if cap.compensate_tool and cap.compensate_tool not in names:
            errors.append(f"compensate tool '{cap.compensate_tool}' is not in the same connector manifest")
        tool = next((t for t in manifest if t.get("name") == cap.name), None)
        if tool is not None:
            errors += validate_tool(tool, manifest)
    return errors

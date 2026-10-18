"""连接器写规范（方案 S8.5）与工具清单（``GET /tools`` manifest）构造。

写规范：非只读工具（``risk_level`` 为 ``write_low`` / ``write_high``）三项缺一不可——
1. **幂等键**：执行（非 dry-run）必须带 ``idempotency_key``，按（租户, 调用者, 键）去重；同键不同请求 409；
   去重记录与业务写入同一事务提交（见 :mod:`mcp_template.idempotency`）；
2. **dry-run**：``dry_run=true`` 只返回影响预演（``impact_preview`` + ``card``），不写任何数据，也不需要幂等键；
3. **补偿工具**：``compensate_tool`` 指向同一 manifest 里的另一个写工具（补偿工具自身也须满足本规范，
   可用 ``compensates`` 反向声明它补偿的是谁）。

``input_schema`` 只描述业务参数：``idempotency_key`` / ``dry_run`` 两个控制字段由 Runtime 闸口生成、经网关 ``/invoke`` 注入，
不得暴露给模型，因此 :func:`write_tool` 会把它们从 schema 中剔除。

写工具的 manifest 条目形如::

    {
      "name": "ticket.close_batch", "description": "...", "risk_level": "write_high", "data_class": "internal",
      "input_schema": {...不含控制字段...},
      "idempotency": {"required": true, "field": "idempotency_key", "scope": ["tenant", "sub"]},
      "dry_run": {"supported": true, "field": "dry_run"},
      "compensate_tool": "ticket.reopen_batch",
      "compensates": null,
      "supports_dry_run": true, "requires_idempotency_key": true   # 兼容 S4–S7 网关的布尔字段
    }
"""

from __future__ import annotations

import copy
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator

IDEMPOTENCY_FIELD = "idempotency_key"
DRY_RUN_FIELD = "dry_run"
CONTROL_FIELDS = (IDEMPOTENCY_FIELD, DRY_RUN_FIELD)
RiskLevel = Literal["read", "write_low", "write_high"]
DataClass = Literal["public", "internal", "confidential"]


class WriteControl(BaseModel):
    """写工具请求体的控制字段（由网关注入）。业务入参模型继承它即可获得规范校验。"""

    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)  # 只有 dry_run 时可省略
    dry_run: bool = False  # 为真时只返回影响预演，不写任何数据

    @model_validator(mode="after")
    def _key_required_for_writes(self) -> Self:
        """真正执行（非 dry_run）必须带幂等键；影响预演不写数据，不需要。"""
        if not self.dry_run and not self.idempotency_key:
            raise ValueError("idempotency_key is required unless dry_run")
        return self


def strip_control_fields(schema: dict[str, Any]) -> dict[str, Any]:
    """返回去掉控制字段的 JSON Schema 副本（properties 与 required 中都去掉）。"""
    out = copy.deepcopy(schema)
    props = out.get("properties")
    if isinstance(props, dict):
        for f in CONTROL_FIELDS:
            props.pop(f, None)
    if isinstance(out.get("required"), list):
        out["required"] = [r for r in out["required"] if r not in CONTROL_FIELDS]
        if not out["required"]:
            out.pop("required")
    return out


def read_tool(name: str, description: str, input_schema: dict[str, Any], *, data_class: DataClass = "internal") -> dict[str, Any]:
    """只读工具的 manifest 条目。"""
    return {"name": name, "description": description, "risk_level": "read", "data_class": data_class, "input_schema": input_schema}


def write_tool(
    name: str,
    description: str,
    input_schema: dict[str, Any],
    *,
    compensate_tool: str,
    risk_level: Literal["write_low", "write_high"] = "write_high",
    compensates: str | None = None,
    data_class: DataClass = "internal",
) -> dict[str, Any]:
    """写工具的 manifest 条目（声明幂等、dry-run 与补偿工具，schema 去掉控制字段）。"""
    return {
        "name": name,
        "description": description,
        "risk_level": risk_level,
        "data_class": data_class,
        "input_schema": strip_control_fields(input_schema),
        "idempotency": {"required": True, "field": IDEMPOTENCY_FIELD, "scope": ["tenant", "sub"]},
        "dry_run": {"supported": True, "field": DRY_RUN_FIELD},
        "compensate_tool": compensate_tool,
        "compensates": compensates,
        "supports_dry_run": True,
        "requires_idempotency_key": True,
    }


def validate_manifest(manifest: list[dict[str, Any]]) -> list[str]:
    """按写规范检查 manifest，返回问题列表（空表示合规）。与网关 ``spec.py:validate_tool`` 规则一致，供连接器自测。"""
    errors: list[str] = []
    names = [t.get("name") for t in manifest]
    if len(names) != len(set(names)):
        errors.append("duplicate tool names")
    for t in manifest:
        name = t.get("name")
        if not name or not t.get("description"):
            errors.append(f"{name}: name and description are required")
        risk = t.get("risk_level")
        if risk not in ("read", "write_low", "write_high"):
            errors.append(f"{name}: invalid risk_level {risk!r}")
        props = (t.get("input_schema") or {}).get("properties") or {}
        exposed = [f for f in CONTROL_FIELDS if f in props]
        if exposed:
            errors.append(f"{name}: input_schema exposes control fields {exposed}")
        if risk == "read":
            continue
        if not (t.get("idempotency") or {}).get("required"):
            errors.append(f"{name}: write tool must require an idempotency key")
        if not (t.get("dry_run") or {}).get("supported"):
            errors.append(f"{name}: write tool must support dry_run")
        comp = t.get("compensate_tool")
        if not comp:
            errors.append(f"{name}: write tool must declare compensate_tool")
        elif comp not in names or comp == name:
            errors.append(f"{name}: compensate_tool {comp!r} is not another tool in this manifest")
    return errors

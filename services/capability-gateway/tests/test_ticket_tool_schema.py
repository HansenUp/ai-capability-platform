"""工单工具入参契约测试：网关种子数据暴露给模型的 schema 要覆盖 §3.2 契约参数，并与 mcp-ticket 的工具定义一致。
Runtime 本地工具表与网关种子的一致性在 services/runtime/tests/test_local_tool_schema.py 校验（不能在此导入 runtime：
会早于 runtime 的 conftest 读取配置，使 Runtime 测试落到仓库根目录的默认 SQLite 库）。"""

from typing import Any

from capability_gateway.registry import SEED
from mcp_ticket.tools import TOOL_MANIFEST

GATEWAY = {c.name: c.input_schema for c in SEED}
MCP = {t["name"]: t["input_schema"] for t in TOOL_MANIFEST}
INJECTED = {"dry_run", "idempotency_key"}  # 由 Runtime 闸口生成、网关注入的控制参数，不暴露给模型


def _enum(prop: dict[str, Any]) -> list[str] | None:
    """取属性的枚举值（Pydantic 对 Optional[Literal] 生成 anyOf）。"""
    for p in [prop, *prop.get("anyOf", [])]:
        if "enum" in p:
            return list(p["enum"])
    return None


def test_ticket_search_schema_covers_contract_and_matches_mcp():
    """ticket.search 含契约全部入参（q / status / created_from / created_to / cursor / limit），与 MCP SearchIn 一一对应。"""
    props = GATEWAY["ticket.search"]["properties"]
    assert set(props) == {"q", "status", "created_from", "created_to", "cursor", "limit"}
    assert set(props) == set(MCP["ticket.search"]["properties"])
    assert not GATEWAY["ticket.search"].get("required") and not MCP["ticket.search"].get("required")
    mcp_props = MCP["ticket.search"]["properties"]
    assert _enum(props["status"]) == _enum(mcp_props["status"]) == ["open", "processing", "closed"]
    assert (props["limit"]["minimum"], props["limit"]["maximum"]) == (mcp_props["limit"]["minimum"], mcp_props["limit"]["maximum"])
    for k in ("created_from", "created_to"):
        assert "ISO 8601" in props[k]["description"]


def test_ticket_get_and_close_batch_schema_match_mcp():
    """ticket.get 与 MCP 完全一致；close_batch 只暴露业务参数 ids（dry_run / 幂等键由平台注入）。"""
    for name in ("ticket.get", "ticket.close_batch"):
        mcp_props = set(MCP[name]["properties"]) - INJECTED
        assert set(GATEWAY[name]["properties"]) == mcp_props, name
        assert GATEWAY[name].get("required") == MCP[name].get("required"), name
    ids = GATEWAY["ticket.close_batch"]["properties"]["ids"]
    mcp_ids = MCP["ticket.close_batch"]["properties"]["ids"]
    assert (ids["minItems"], ids["maxItems"]) == (mcp_ids["minItems"], mcp_ids["maxItems"])

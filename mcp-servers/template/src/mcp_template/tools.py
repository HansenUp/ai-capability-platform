"""模板强制约束的写工具三条规则：(1) 幂等键（idempotency key）(2) dry_run 影响预演
(3) 声明补偿工具（compensate tool）。具体业务逻辑写在各 `*_impl` 函数中。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

_EXECUTED: dict[str, dict[str, Any]] = {}  # 幂等存储（生产环境应替换为 Redis/DB）


class Identity(BaseModel):
    """调用者身份：从 OBO 令牌解析得到的终端用户及代理其行事的 Agent。"""

    sub: str  # 终端用户（来自 OBO 令牌）
    act: str | None = None  # 代理该用户行事的 Agent 标识
    tenant: str = "default"  # 租户标识


class CloseBatchIn(BaseModel):
    """批量关闭工单的请求体。"""

    ids: list[str]  # 待关闭的工单 ID 列表
    idempotency_key: str = Field(..., min_length=8)  # 幂等键，重复请求以此去重
    dry_run: bool = False  # 为真时仅返回影响预演，不实际执行


def ticket_search_impl(identity: Identity, q: str) -> dict[str, Any]:
    """只读搜索实现（示例数据）。"""
    # 行级权限校验是下游系统的职责：这里按 identity.sub 过滤即可，不在此处重复实现完整鉴权
    return {"tickets": [{"id": "T-1", "owner": identity.sub, "title": f"匹配 {q}"}]}


def ticket_close_batch(identity: Identity, body: CloseBatchIn) -> dict[str, Any]:
    """批量关闭工单实现：优先返回 dry_run 影响预演；否则按幂等键去重后执行并声明补偿工具。"""
    if body.dry_run:
        # dry-run：只返回将产生的影响（预计关闭的工单数、将发送的通知数），不做任何实际变更
        return {"impact_preview": {"tickets": len(body.ids), "notifications": len(body.ids)}, "card": {"card_type": "impact_preview"}}
    if body.idempotency_key in _EXECUTED:
        # 同一幂等键的重复调用直接返回上次结果，避免重复关闭
        return _EXECUTED[body.idempotency_key] | {"deduplicated": True}
    result = {"closed": body.ids, "compensate_tool": "ticket.reopen_batch", "by": identity.act or identity.sub}
    _EXECUTED[body.idempotency_key] = result
    return result


TOOL_MANIFEST = [
    {"name": "ticket.search", "risk_level": "read", "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}},
    {
        "name": "ticket.close_batch",
        "risk_level": "write_high",  # 高风险写操作
        "supports_dry_run": True,  # 支持影响预演
        "requires_idempotency_key": True,  # 强制要求幂等键
        "compensate_tool": "ticket.reopen_batch",  # 声明的补偿工具，供误操作后回滚
        "input_schema": CloseBatchIn.model_json_schema(),
    },
]

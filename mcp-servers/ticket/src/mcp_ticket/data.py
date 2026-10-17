"""mock 工单数据加载与行级权限。

行级权限（租户 + 角色 + 本人）：
1. 租户必须等于调用者租户，跨租户一律不可见；
2. 同租户内，持 realm 角色 ``aftersales`` 的调用者可见全部工单；
3. 其余调用者只可见 ``requester`` 或 ``owner`` 等于自己 sub 的工单。
"无权限"与"不存在"对外不可区分（同一个 404 响应），不泄露工单是否存在。
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from mcp_ticket.auth import Identity

AFTERSALES_ROLE = "aftersales"  # 可查看租户内全部工单的 realm 角色

Ticket = dict[str, Any]


@cache
def load_tickets(path: Path) -> tuple[Ticket, ...]:
    """读取 mock 工单（JSON 数组），按文件路径缓存；调用方不得修改返回的字典。"""
    return tuple(json.loads(path.read_text(encoding="utf-8")))


def can_view(ident: Identity, ticket: Ticket) -> bool:
    """行级权限判定，见模块文档。"""
    if ticket["tenant"] != ident.tenant:
        return False
    if AFTERSALES_ROLE in ident.roles:
        return True
    return ident.sub in (ticket["requester"], ticket["owner"])

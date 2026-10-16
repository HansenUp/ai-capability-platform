"""Console 认证链：登录（OIDC）→ 租户解析 → 角色校验 → Agent 归属校验。

四级角色：USER < ADMIN（只能管理分配给自己的 Agent）< TENANT_ADMIN < PLATFORM_ADMIN（SaaS 预留）。
开发态从 X-Console-User / X-Console-Role / X-Console-Tenant 头读取身份；生产由 APISIX 验签后注入同名头。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from fastapi import Depends, Header, HTTPException


class Role(IntEnum):
    """操作者角色，数值越大权限越高，可直接比较。"""

    USER = 0  # 普通使用者
    ADMIN = 1  # Agent 管理员：只能管理分配给自己的 Agent
    TENANT_ADMIN = 2  # 租户管理员：管理本租户全部 Agent 与节点
    PLATFORM_ADMIN = 3  # 平台管理员：SaaS 多租户场景预留


@dataclass(frozen=True)
class Operator:
    """当前操作者。"""

    user: str
    tenant: str
    role: Role


ASSIGNMENTS: dict[str, set[str]] = {}  # 用户 -> 其被分配的 Agent id 集合（对应 user_agents 表）


async def operator(
    x_console_user: str = Header(default="dev"),
    x_console_role: str = Header(default="TENANT_ADMIN"),
    x_console_tenant: str = Header(default="default"),
) -> Operator:
    """从请求头解析操作者（开发态）；未知角色名返回 401。"""
    try:
        return Operator(x_console_user, x_console_tenant, Role[x_console_role])
    except KeyError as e:
        raise HTTPException(401, "unknown role") from e


def require(min_role: Role):
    """生成一个 FastAPI 依赖：要求操作者角色不低于 min_role，否则 403。"""

    async def dep(op: Operator = Depends(operator)) -> Operator:
        if op.role < min_role:
            raise HTTPException(403, f"requires {min_role.name}")
        return op

    return dep


def assert_assigned(op: Operator, agent_id: str) -> None:
    """归属校验：租户管理员及以上放行；ADMIN 只能操作分配给自己的 Agent。"""
    if op.role >= Role.TENANT_ADMIN:
        return
    if agent_id not in ASSIGNMENTS.get(op.user, set()):
        raise HTTPException(403, "agent not assigned to operator")

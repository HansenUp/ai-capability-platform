"""策略判定客户端（契约 1：策略服务 ``POST /authz/check-batch``）。

请求体：``{requests:[{principal:{sub,tenant,roles}, action, resource:{id,tenant,system,risk_level,data_class},
context:{approved,taint,agent:{id,version,tenant}}}]}``；应答：``{results:[{decision,policy_ids,reason}], engine, policy_version}``。
主体（含租户）取自验签后的终端用户令牌（D128），资源属性取自网关目录。策略服务不可达、超时、应答异常或条数不符时
整批判 deny（reason=policy_unavailable，失败关闭）；不再回退任何本地基线。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from capability_gateway import config
from capability_gateway.registry import Capability

UNAVAILABLE = "policy_unavailable"
TOKEN_HEADER = "X-Policy-Service-Token"  # noqa: S105 —— 头名而非密钥：策略服务的服务间鉴权头


@dataclass(frozen=True)
class Principal:
    """终端用户主体（来自验签令牌；开发态来自 dev 头）。"""

    sub: str
    tenant: str
    roles: tuple[str, ...] = ()
    exp: float | None = None  # 用户令牌过期时间（epoch 秒）
    token: str | None = field(default=None, repr=False)  # 原始用户令牌（换 OBO 用），不进日志


@dataclass(frozen=True)
class AgentRef:
    id: str
    version: str = ""
    tenant: str = ""


@dataclass(frozen=True)
class Decision:
    decision: str  # allow | deny | require_approval
    policy_ids: tuple[str, ...] = ()
    reason: str | None = None
    engine: str | None = None
    policy_version: str | None = None


def build_request(
    principal: Principal, cap: Capability, agent: AgentRef, *, approved: bool = False, taint: list[str] | None = None
) -> dict[str, Any]:
    return {
        "principal": {"sub": principal.sub, "tenant": principal.tenant, "roles": list(principal.roles)},
        "action": "invoke",
        "resource": {"id": cap.id, "tenant": cap.tenant, "system": cap.system, "risk_level": cap.risk_level, "data_class": cap.data_class},
        "context": {
            "approved": approved,
            "taint": list(taint or []),
            "agent": {"id": agent.id, "version": agent.version, "tenant": agent.tenant or principal.tenant},
        },
    }


async def check_batch(requests: list[dict[str, Any]]) -> list[Decision]:
    """批量判定；任何失败整批 deny。"""
    if not requests:
        return []
    deny = [Decision("deny", reason=UNAVAILABLE, engine="unavailable") for _ in requests]
    try:
        async with httpx.AsyncClient(timeout=config.POLICY_TIMEOUT_S, transport=config.transport) as c:
            headers = {TOKEN_HEADER: config.POLICY_SERVICE_TOKEN} if config.POLICY_SERVICE_TOKEN else {}
            r = await c.post(f"{config.POLICY_URL.rstrip('/')}/authz/check-batch", json={"requests": requests}, headers=headers)
        r.raise_for_status()
        body = r.json()
        results = body["results"]
        if not isinstance(results, list) or len(results) != len(requests):
            return deny
        out: list[Decision] = []
        for res in results:
            d = res.get("decision")
            if d not in ("allow", "deny", "require_approval"):
                return deny
            out.append(
                Decision(
                    d,
                    tuple(str(p) for p in res.get("policy_ids") or ()),
                    res.get("reason"),
                    body.get("engine"),
                    body.get("policy_version"),
                )
            )
        return out
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        return deny


async def check(
    principal: Principal, cap: Capability, agent: AgentRef, *, approved: bool = False, taint: list[str] | None = None
) -> Decision:
    (d,) = await check_batch([build_request(principal, cap, agent, approved=approved, taint=taint)])
    return d

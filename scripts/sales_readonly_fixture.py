"""F6.2 synthetic-only owner-scoped sales profile. Never reads CRM configuration."""

from datetime import date, datetime
from math import isfinite
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Query(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    q: str | None = Field(default=None, max_length=100)
    stage: Literal["prospecting", "qualification", "proposal", "negotiation", "won", "lost"] | None = None
    limit: int = Field(default=20, ge=1, le=50)


FIELDS = ("id", "name", "customer", "stage", "amount", "currency", "expected_close", "updated_at")
ROWS = (
    dict(
        id="S-1",
        name="合成设备采购",
        customer="合成企业甲",
        stage="proposal",
        amount=12000,
        currency="CNY",
        expected_close="2026-11-30",
        updated_at="2026-10-29T00:00:00Z",
        tenant="default",
        owner="human:alice",
        notes="NEVER-MODEL",
        email="NEVER-EMAIL",
    ),
    dict(
        id="S-2",
        name="合成续约",
        customer="合成企业乙",
        stage="qualification",
        amount=None,
        currency="USD",
        expected_close="invalid",
        updated_at="invalid",
        tenant="default",
        owner="human:alice",
        phone="NEVER-PHONE",
    ),
    dict(id="OTHER-OWNER", tenant="default", owner="human:bob", name="不可见"),
    dict(id="OTHER-TENANT", tenant="foreign", owner="human:alice", name="不可见"),
)


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def project(row):
    out = {key: row.get(key) for key in FIELDS}
    value = out["amount"]
    if type(value) not in (int, float) or not isfinite(value) or value < 0:
        out["amount"] = None
    out["updated_at"] = timestamp(out["updated_at"])
    out["updated_at"] = out["updated_at"].isoformat() if out["updated_at"] else None
    try:
        out["expected_close"] = date.fromisoformat(out["expected_close"]).isoformat()
    except (TypeError, ValueError):
        out["expected_close"] = None
    for key in ("id", "name", "customer", "stage", "currency"):
        if not isinstance(out[key], str):
            out[key] = None
    return out


def search(rows, tenant, subject, arguments):
    query = Query.model_validate(arguments)
    visible = [project(row) for row in rows if row.get("tenant") == tenant and row.get("owner") == subject]
    visible = [
        row
        for row in visible
        if (not query.q or query.q.casefold() in " ".join(str(row[k] or "") for k in ("id", "name", "customer")).casefold())
        and (not query.stage or row["stage"] == query.stage)
    ]
    visible.sort(
        key=lambda row: (timestamp(row["updated_at"]).timestamp() if timestamp(row["updated_at"]) else float("-inf"), row["id"] or ""),
        reverse=True,
    )
    return {
        "items": visible[: query.limit],
        "returned": min(len(visible), query.limit),
        "truncated": len(visible) > query.limit,
        "scope": "same-tenant-owner-filter",
        "data_class": "confidential",
    }


def summary(output):
    if not isinstance(output, dict) or output.get("scope") != "same-tenant-owner-filter" or "items" not in output:
        return "模拟商机查询不可用，未生成摘要。"
    if not output["items"]:
        return "当前本人授权筛选无结果，不代表零业绩。"
    lines = ["模拟销售摘要：仅当前本人授权筛选返回范围；" + ("已截断。" if output["truncated"] else "未截断。")]
    for item in output["items"]:
        evidence = f"{item['id'] or '未知'} / {item['updated_at'] or '更新时间未知'}"
        amount = str(item["amount"]) if item["amount"] is not None else "金额未知"
        lines.append(
            f"事实 [{evidence}]：{item['name']} / {item['customer']} / {item['stage']} / "
            f"{amount} {item['currency'] or '币种未知'} / 预计日期 {item['expected_close'] or '未知'}。"
        )
        lines.append(f"建议（非执行）[{evidence}]：人工核实当前阶段与下一步安排。")
    return "\n".join(lines)


async def read(rc, arguments, rows=ROWS):
    from runtime.api.auth import principal_from_claims, verify_jwt

    if rc is None or not rc.credential:
        raise ValueError("signed direct user required")
    claims = await verify_jwt(rc.credential)
    principal = principal_from_claims(claims)
    if (
        "act" in claims
        or principal.auth_method != "user"
        or claims.get("principal_type", "user") != "user"
        or principal.tenant_id != rc.gate.tenant_id
        or principal.subject != rc.gate.subject
    ):
        raise ValueError("sales identity mismatch")
    result = search(rows, principal.tenant_id, principal.subject, arguments)
    await verify_jwt(rc.credential)
    return result


def install():
    from ai_contract import ToolSpec
    from runtime.assemble.assembler import LOCAL_TOOLS
    from runtime.loop import toolbroker

    LOCAL_TOOLS["crm.opportunity.search"] = ToolSpec(
        name="crm.opportunity.search",
        description="合成本人商机只读查询",
        capability_id="cap.crm.opportunity.search",
        data_class="confidential",
        input_schema=Query.model_json_schema(),
    )
    previous = toolbroker.execute_tool

    async def execute(tool, tool_input, dry_run=False, rc=None, call_id=None, idempotency_key=None):
        if tool.name != "crm.opportunity.search":
            return await previous(tool, tool_input, dry_run, rc, call_id, idempotency_key)
        if dry_run or not call_id:
            return {"error": "synthetic sales unavailable"}
        try:
            if tool_input == {"q": "SYNTHETIC-FAIL"}:
                raise ValueError("synthetic upstream failure")
            return await read(rc, tool_input)
        except Exception:
            return {"error": "synthetic sales unavailable"}

    toolbroker.execute_tool = execute

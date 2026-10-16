"""授权判定服务（Policy Service，默认端口 8083）。

平台"策略即代码"的判定入口，属于纵向的"身份 · 权限 · 审计"能力：
- 生产：用 Cedar 引擎（Rust CLI / sidecar，见 ADR-0001）评估 `cedar/policies.cedar`。
- 开发回退：`baseline.evaluate` 用 Python 镜像同一套策略，使平台在没有 Cedar 时也能运行。
两者都必须通过 `cedar/tests.json` 的用例。每次判定都会生成 decision_id 并记录，
Runtime 的工具闸口会把该 id 写入 Run 事件日志（tool.decision），实现判定可追溯。
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import FastAPI
from pydantic import BaseModel, Field

from policy_service.baseline import evaluate

app = FastAPI(title="AI Capability Platform · Policy Service", version="0.1.0")
DECISION_LOG: list[dict[str, Any]] = []  # 判定记录（内存实现；生产写入审计库）


class CheckRequest(BaseModel):
    """一次授权判定请求，由 Runtime 闸口或能力网关发起。"""

    subject: str  # 调用主体标识，如 human:123 / agent:demo
    agent: str | None = None  # 代理执行的 Agent（OBO 场景下的 act 声明）
    capability: str  # 目标能力 id，如 cap.ticket.close_batch
    action: str = "invoke"  # 动作，对应 Cedar Schema 中的 action
    principal: dict[str, Any] = Field(default_factory=lambda: {"role": "user", "tenant": "default"})  # 主体属性
    resource: dict[str, Any] = Field(default_factory=dict)  # 能力属性：risk_level / tenant / system / data_class
    context: dict[str, Any] = Field(default_factory=lambda: {"approved": False, "taint": []})  # 审批状态与数据污点


class CheckResponse(BaseModel):
    """判定结果。"""

    decision: Literal["allow", "deny", "require_approval"]  # 放行 / 拒绝 / 需审批
    decision_id: str  # 判定编号，供事件日志与审计关联
    matched: str  # 命中的规则名


@app.post("/authz/check", response_model=CheckResponse)
async def check(req: CheckRequest) -> CheckResponse:
    """执行一次判定并记录到判定日志。"""
    decision, matched = evaluate(req.principal, req.resource, req.context)
    did = "dec_" + uuid.uuid4().hex[:12]
    DECISION_LOG.append({"id": did, "req": req.model_dump(), "decision": decision, "matched": matched})
    return CheckResponse(decision=decision, decision_id=did, matched=matched)


@app.get("/authz/decisions")
async def decisions(limit: int = 50) -> list[dict[str, Any]]:
    """返回最近的判定记录（审计 / 调试用）。"""
    return DECISION_LOG[-limit:]


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """健康检查。"""
    return {"status": "ok"}

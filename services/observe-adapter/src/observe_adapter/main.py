"""观测适配器（Observe Adapter）。

纵向"观测 · 运营 · 评测"能力的开放上报入口：
- `/observe/*`：供无法直接输出 OTLP 的外部 Agent / 应用上报用量、日志、状态；
- `/feedback`：触点（门户、组件）上报用户反馈与纠正。
配置了 LANGFUSE_HOST 时转发到 Langfuse（Trace、反馈、数据集的事实源，ADR-0002）；
未配置时写入内存缓冲，便于本地开发与测试。OTel 直连路径由 S4 工作包补齐。
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI(title="AI Capability Platform · Observe Adapter", version="0.1.0")
LANGFUSE_HOST = os.environ.get("LANGFUSE_HOST")  # 例：http://localhost:3000
LANGFUSE_AUTH = (os.environ.get("LANGFUSE_PUBLIC_KEY", ""), os.environ.get("LANGFUSE_SECRET_KEY", ""))  # Basic Auth
BUFFER: list[dict[str, Any]] = []  # 未配置 Langfuse 时的开发态落点


async def _forward(path: str, body: dict[str, Any]) -> dict[str, Any]:
    """转发到 Langfuse 公共 API；未配置时缓冲并返回缓冲长度。"""
    if not LANGFUSE_HOST:
        BUFFER.append({"path": path, "body": body})
        return {"forwarded": False, "buffered": len(BUFFER)}
    async with httpx.AsyncClient(timeout=5.0, auth=LANGFUSE_AUTH) as c:
        r = await c.post(f"{LANGFUSE_HOST}{path}", json=body)
        return {"forwarded": r.status_code < 300, "status": r.status_code}


class Feedback(BaseModel):
    """用户反馈。"""

    trace_id: str  # 对应的 Trace（Run）编号
    subject: str  # 反馈人
    score: float  # +1 / -1 或 0..1
    comment: str = ""
    correction: str | None = None  # 用户给出的正确答案，可沉淀为评测样本


@app.post("/feedback")
async def feedback(body: Feedback) -> dict[str, Any]:
    """把反馈写成 Langfuse score（名称 user_feedback）。"""
    return await _forward(
        "/api/public/scores",
        {"traceId": body.trace_id, "name": "user_feedback", "value": body.score, "comment": body.comment or body.correction or ""},
    )


class Usage(BaseModel):
    """模型用量上报。"""

    agent_id: str
    model: str
    input_tokens: int
    output_tokens: int
    cost: float = 0
    trace_id: str | None = None


@app.post("/observe/usage")
async def usage(body: Usage) -> dict[str, Any]:
    """用量以 generation-create 事件写入 Langfuse ingestion。"""
    return await _forward("/api/public/ingestion", {"batch": [{"type": "generation-create", "body": body.model_dump()}]})


class Log(BaseModel):
    """结构化日志上报。"""

    agent_id: str
    level: str = "info"
    message: str
    attrs: dict[str, Any] = Field(default_factory=dict)


@app.post("/observe/logs")
async def logs(body: Log) -> dict[str, Any]:
    """日志以 event-create 事件写入 Langfuse ingestion。"""
    return await _forward("/api/public/ingestion", {"batch": [{"type": "event-create", "body": body.model_dump()}]})


class Status(BaseModel):
    """外部 Agent 实例状态。"""

    agent_id: str
    instances: int = 1
    healthy: bool = True


@app.post("/observe/status")
async def status(body: Status) -> dict[str, Any]:
    """状态只在本地记录（供 Console 展示），不进 Langfuse。"""
    BUFFER.append({"path": "/status", "body": body.model_dump()})
    return {"ok": True}


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """健康检查。"""
    return {"status": "ok"}

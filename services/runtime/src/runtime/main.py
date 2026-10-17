"""Agent Runtime 节点入口（FastAPI 应用）。负责启动时初始化数据库、预热默认 harness，
并挂载 Messages/Run API 与管理 API 两组路由。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from runtime.api import admin, messages
from runtime.config import settings
from runtime.harness import registry
from runtime.observe import tracing
from runtime.state.db import init_db
from runtime.state.eventlog import RunNotFound


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期钩子：启动时安装 TracerProvider、建表并确保默认 harness 可加载，快速失败于启动阶段而非首个请求；
    关停时把缓冲中的 span 立即导出，避免最后几条 Trace 丢失。"""
    tracing.setup_tracing()
    await init_db()
    registry.get(settings.harness)  # fail fast if the default harness cannot be loaded（默认 harness 加载失败则立即报错）
    yield
    tracing.force_flush()


app = FastAPI(title="AI Capability Platform · Agent Runtime", version="0.1.0", lifespan=lifespan)
app.include_router(messages.router)
app.include_router(admin.router)


@app.exception_handler(RunNotFound)
async def run_not_found(request: Request, exc: RunNotFound) -> JSONResponse:
    """事件日志找不到目标 Run 时统一映射为 404，避免以 500 暴露给调用方。"""
    return JSONResponse(status_code=404, content={"detail": f"run '{exc.args[0]}' not found"})


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """健康检查端点，返回节点状态与节点标识。"""
    return {"status": "ok", "node": settings.node_id}

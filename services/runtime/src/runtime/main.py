"""Agent Runtime 节点入口（FastAPI 应用）。负责启动时初始化数据库、预热默认 harness，
并挂载 Messages/Run API 与管理 API 两组路由。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from runtime.api import admin, messages, runs
from runtime.api.audit import AuditMiddleware
from runtime.config import settings, validate_for_production
from runtime.harness import registry
from runtime.observe import tracing
from runtime.state.db import init_db
from runtime.state.eventlog import RunNotFound, StaleWriter
from runtime.worker import settings as worker_settings_mod
from runtime.worker import worker as worker_mod


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期钩子：启动时安装 TracerProvider、建表并确保默认 harness 可加载，快速失败于启动阶段而非首个请求，
    再按 RUNTIME_ROLE 启动 Run Worker 等后台组件；关停时先停后台组件（在途 Run 释放租约），再把缓冲中的 span 立即导出。"""
    validate_for_production(settings)  # RUNTIME_ENV=prod 时安全基线不满足即拒绝启动
    tracing.setup_tracing()
    await init_db()
    registry.get(settings.harness)  # fail fast if the default harness cannot be loaded（默认 harness 加载失败则立即报错）
    # S7：按 RUNTIME_ROLE 启停后台组件——worker / all 启动 Worker（含 XAUTOCLAIM、reaper），配了 Redis 时订阅跨节点取消
    worker_settings_mod.validate(worker_settings_mod.worker_settings, settings.runtime_env)
    bg = await worker_mod.start_background()
    try:
        yield
    finally:
        await bg.stop()
        tracing.force_flush()


app = FastAPI(title="AI Capability Platform · Agent Runtime", version="0.1.0", lifespan=lifespan)
app.include_router(messages.router)
app.include_router(runs.router)  # S7：Run 列表与事件流
app.include_router(admin.router)
app.add_middleware(AuditMiddleware)  # 每个 /v1、/admin 请求响应结束后写一行 t_audit（S2.3）


@app.exception_handler(RunNotFound)
async def run_not_found(request: Request, exc: RunNotFound) -> JSONResponse:
    """事件日志找不到目标 Run 时统一映射为 404，避免以 500 暴露给调用方。"""
    return JSONResponse(status_code=404, content={"detail": f"run '{exc.args[0]}' not found"})


@app.exception_handler(StaleWriter)
async def stale_writer(request: Request, exc: StaleWriter) -> JSONResponse:
    """写入前提已被并发写者改变（SeqConflict / LeaseLost，如两人同时审批 / 拒绝同一 Run）：统一映射为 409（可重读后重试），
    不以 500 暴露。各接口已有的专门处理（取消重试一次、/input 的 409 文案）优先。"""
    return JSONResponse(status_code=409, content={"detail": f"run was updated concurrently; reload and retry ({type(exc).__name__})"})


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """健康检查端点，返回节点状态与节点标识。"""
    return {"status": "ok", "node": settings.node_id}

"""Agent Runtime 节点入口（FastAPI 应用）。负责启动时初始化数据库、预热默认 harness，
并挂载 Messages/Run API 与管理 API 两组路由。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from runtime.api import admin, feedback_metrics, feedback_writes, messages, runs, workbench
from runtime.api.audit import AuditMiddleware
from runtime.api.views import error_code
from runtime.config import settings, validate_for_production
from runtime.harness import registry
from runtime.node import agent as node_agent_mod
from runtime.node.lifecycle import AdmissionMiddleware, NodeLifecycle
from runtime.observe import tracing
from runtime.state.db import init_db
from runtime.state.eventlog import RunNotFound, StaleWriter
from runtime.worker import settings as worker_settings_mod
from runtime.worker import worker as worker_mod


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期钩子：启动时安装 TracerProvider、建表并确保默认 harness 可加载，快速失败于启动阶段而非首个请求，
    再按 RUNTIME_ROLE 启动 Run Worker 等后台组件，配置了 CONSOLE_URL 时启动节点代理（S9.8，契约 ⑤ runtime.node.agent.start）；
    关停时先停节点代理与后台组件（在途 Run 释放租约），再把缓冲中的 span 立即导出。"""
    validate_for_production(settings)  # RUNTIME_ENV=prod 时安全基线不满足即拒绝启动
    tracing.setup_tracing()
    await init_db()
    registry.get(settings.harness)  # fail fast if the default harness cannot be loaded（默认 harness 加载失败则立即报错）
    # S7：按 RUNTIME_ROLE 启停后台组件——worker / all 启动 Worker（含 XAUTOCLAIM、reaper），配了 Redis 时订阅跨节点取消
    worker_settings_mod.validate(worker_settings_mod.worker_settings, settings.runtime_env)
    bg = await worker_mod.start_background()
    app.state.node_shutdown_complete = False
    app.state.node_lifecycle = NodeLifecycle(
        bg, getattr(app.state, "node_request_exit", None), durable=bool(worker_settings_mod.worker_settings.redis_url)
    )
    node_agent = await node_agent_mod.start(app)  # 未配置 CONSOLE_URL（单机开发）时为 None
    try:
        yield
    finally:
        if node_agent is not None:
            await node_agent.stop()
        await bg.stop()
        if getattr(app.state, "node_restart_requested", False):
            await registry.shutdown_pools()
        tracing.force_flush()
        app.state.node_shutdown_complete = True


app = FastAPI(title="AI Capability Platform · Agent Runtime", version="0.1.0", lifespan=lifespan)
app.include_router(messages.router)
app.include_router(runs.router)  # S7：Run 列表与事件流
app.include_router(workbench.router)
app.include_router(feedback_metrics.router)
app.include_router(feedback_writes.router)
if settings.runtime_admin_enabled:  # 节点只出不进（S9.8）：RUNTIME_ADMIN_ENABLED=false 时不挂 /admin 管理接口
    app.include_router(admin.router)
app.add_middleware(AuditMiddleware)  # 每个 /v1、/admin 请求响应结束后写一行 t_audit（S2.3）
app.add_middleware(AdmissionMiddleware)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """所有 HTTPException 统一输出 ErrorDetail{detail, code}（Contract V1.3，D135），保留原响应头（如 WWW-Authenticate）。"""
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    body = {"detail": detail, "code": error_code(exc.status_code, detail, getattr(exc, "code", None))}
    return JSONResponse(status_code=exc.status_code, content=body, headers=getattr(exc, "headers", None))


@app.exception_handler(RunNotFound)
async def run_not_found(request: Request, exc: RunNotFound) -> JSONResponse:
    """事件日志找不到目标 Run 时统一映射为 404，避免以 500 暴露给调用方。"""
    return JSONResponse(status_code=404, content={"detail": f"run '{exc.args[0]}' not found", "code": "NOT_FOUND"})


@app.exception_handler(StaleWriter)
async def stale_writer(request: Request, exc: StaleWriter) -> JSONResponse:
    """写入前提已被并发写者改变（SeqConflict / LeaseLost，如两人同时审批 / 拒绝同一 Run）：统一映射为 409（可重读后重试），
    不以 500 暴露。各接口已有的专门处理（取消重试一次、/input 的 409 文案）优先。"""
    detail = f"run was updated concurrently; reload and retry ({type(exc).__name__})"
    return JSONResponse(status_code=409, content={"detail": detail, "code": "CONCURRENT_UPDATE"})


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """健康检查端点，返回节点状态与节点标识。"""
    if hasattr(app.state, "node_lifecycle") and not app.state.node_lifecycle.accepting:
        from fastapi import HTTPException

        raise HTTPException(503, "node is draining")
    return {"status": "ok", "node": settings.node_id}

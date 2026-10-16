"""Agent Runtime 节点入口（FastAPI 应用）。负责启动时初始化数据库、预热默认 harness，
并挂载 Messages/Run API 与管理 API 两组路由。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from runtime.api import admin, messages
from runtime.config import settings
from runtime.harness import registry
from runtime.state.db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期钩子：启动时建表并确保默认 harness 可加载，快速失败于启动阶段而非首个请求。"""
    await init_db()
    registry.get(settings.harness)  # fail fast if the default harness cannot be loaded（默认 harness 加载失败则立即报错）
    yield


app = FastAPI(title="AI Capability Platform · Agent Runtime", version="0.1.0", lifespan=lifespan)
app.include_router(messages.router)
app.include_router(admin.router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """健康检查端点，返回节点状态与节点标识。"""
    return {"status": "ok", "node": settings.node_id}

"""Runtime HTTP API 的响应视图模型（只用于 OpenAPI 文档与响应序列化，不进入 ai_contract）。

约定：业务规则拒绝统一返回 409，响应体为 ErrorDetail；422 只保留给 FastAPI 的参数校验（HTTPValidationError）。
各视图字段与原先返回的字典保持一致，挂上 response_model 不改变返回内容。
"""

from __future__ import annotations

from pydantic import BaseModel, RootModel

from ai_contract import RunEvent, RunState


class ErrorDetail(BaseModel):
    """HTTPException 的响应体：detail 为可读的错误说明。"""

    detail: str


class RunView(BaseModel):
    """GET /v1/runs/{run_id}：单个 Run 的当前状态与元信息。"""

    run_id: str  # Run 标识
    state: RunState  # 当前状态（A2A 命名）
    agent_id: str  # Agent 标识
    agent_version: str  # Agent 版本
    model_alias: str  # 模型别名
    subject: str  # 发起主体
    last_seq: int  # 事件日志最新 seq
    created_at: str  # 创建时间（ISO 8601 字符串）


class RunStateView(BaseModel):
    """创建 / 拒绝 / 取消 Run 后返回的状态快照。"""

    run_id: str
    state: RunState


class ApprovalView(RunStateView):
    """批准后返回：Run 回到 working，需调用 resume 指向的地址继续执行。"""

    resume: str  # 续跑接口路径


class RunEventView(RunEvent):
    """事件日志中的一行，字段与契约 RunEvent 完全相同。

    单独起名是为了避开 OpenAPI 组件重名：FastAPI 生成响应 schema 时会省略 `default: null`，
    与导出脚本合并进来的契约 RunEvent schema 不再逐字相同，同名会被导出脚本判为冲突。
    """


class RunEventsView(RootModel[list[RunEventView]]):
    """GET /v1/runs/{run_id}/events：按 seq 升序的 Run 事件日志。"""

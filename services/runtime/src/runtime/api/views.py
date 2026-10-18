"""Runtime HTTP API 的响应视图模型（只用于 OpenAPI 文档与响应序列化，不进入 ai_contract）。

约定（S8 D135，Contract V1.3 统一错误体）：业务规则拒绝统一返回 400（请求内容不被接受）或 409（与当前状态冲突），
所有 HTTPException 的响应体都是 ai_contract.ErrorDetail{detail, code}（由 main 的异常处理器统一生成）；422 只保留给
FastAPI 的参数校验（HTTPValidationError）。各视图字段与原先返回的字典保持一致，挂上 response_model 不改变返回内容。
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, RootModel

from ai_contract import ErrorDetail, RunEvent, RunState

__all__ = ["ErrorDetail", "ApiError", "error_code"]

# 未显式给出错误码时按状态码取默认值
DEFAULT_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    500: "INTERNAL_ERROR",
    502: "BAD_GATEWAY",
    503: "SERVICE_UNAVAILABLE",
}
_CODE_PREFIX = re.compile(r"^([A-Z][A-Z0-9_]{2,}): ")  # 说明以 "CODE: " 开头时（如 ATTACHMENT_TOO_MANY: ...）取其为错误码


class ApiError(HTTPException):
    """带稳定错误码的 HTTPException：响应体为 ErrorDetail{detail, code}。"""

    def __init__(self, status_code: int, code: str, detail: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(status_code, detail, headers)
        self.code = code


def error_code(status_code: int, detail: Any, explicit: str | None = None) -> str:
    """错误码：显式给出 > 说明里的 "CODE: " 前缀 > 按状态码的默认值。"""
    if explicit:
        return explicit
    m = _CODE_PREFIX.match(detail) if isinstance(detail, str) else None
    if m:
        return m.group(1)
    return DEFAULT_CODES.get(status_code, f"HTTP_{status_code}")


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


class RunListItem(RunView):
    """GET /v1/runs 列表项：RunView 加关联会话标识（S7）。"""

    session_id: str | None = None


class RunListView(BaseModel):
    """GET /v1/runs：按 (created_at, run_id) 倒序的键集分页；next_cursor 为不透明游标，无更多时为 null。"""

    items: list[RunListItem]
    next_cursor: str | None = None


class InputView(BaseModel):
    """POST /v1/runs/{run_id}/input：补充输入已记录，Run 回到 working 并已入队续跑。"""

    run_id: str
    state: RunState
    kind: str  # answer | reconcile | credential
    call_id: str | None = None  # 对应的工具调用（credential 且非写工具前挂起时为空）


class RunStateView(BaseModel):
    """创建 / 拒绝 / 取消 Run 后返回的状态快照。"""

    run_id: str
    state: RunState


class ApprovalView(RunStateView):
    """批准后返回：Run 回到 working。持久化执行的 Run（S7，D108）已入队由 Worker 以发起人凭据自动续跑；发起人也可调用
    resume 指向的地址（抢到租约则内联执行，否则尾随事件流）。"""

    resume: str  # 续跑接口路径


class RunEventView(RunEvent):
    """事件日志中的一行，字段与契约 RunEvent 完全相同。

    单独起名是为了避开 OpenAPI 组件重名：FastAPI 生成响应 schema 时会省略 `default: null`，
    与导出脚本合并进来的契约 RunEvent schema 不再逐字相同，同名会被导出脚本判为冲突。
    """


class RunEventsView(RootModel[list[RunEventView]]):
    """GET /v1/runs/{run_id}/events：按 seq 升序的 Run 事件日志。"""

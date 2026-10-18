"""统一错误体（S7 #4 / D135）：网关的业务拒绝一律返回 ``{detail, code}``，422 只留给 FastAPI 参数校验。

``ErrorDetail`` 与 ai_contract 中定义的同名模型同形（WP-C），后续可直接替换为契约包导入。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

DEFAULT_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    502: "upstream_error",
    503: "unavailable",
}


class ErrorDetail(BaseModel):
    """业务错误响应体：detail 为人读描述，code 为稳定的机器可读错误码。"""

    detail: str
    code: str


class GatewayError(HTTPException):
    """带错误码的 HTTPException。"""

    def __init__(self, status_code: int, code: str, detail: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(status_code, detail, headers)
        self.code = code


def responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI 的 responses 声明：给定状态码都以 ErrorDetail 为响应体。"""
    return {c: {"model": ErrorDetail} for c in codes}


async def _handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = getattr(exc, "code", None) or DEFAULT_CODES.get(exc.status_code, "error")
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return JSONResponse({"detail": detail, "code": code}, status_code=exc.status_code, headers=getattr(exc, "headers", None))


def install(app: FastAPI) -> None:
    """把所有 HTTPException（含 404 路由不存在）渲染为 ErrorDetail。"""
    app.add_exception_handler(StarletteHTTPException, _handler)

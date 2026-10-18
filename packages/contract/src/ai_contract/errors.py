"""统一错误体（Contract V1.3，S8 D135）：平台 HTTP 接口的业务拒绝响应。

约定：业务规则拒绝统一返回 400（请求内容不被接受）或 409（与资源当前状态冲突），响应体为 ``ErrorDetail``；
422 只保留给参数校验（FastAPI 的 ``HTTPValidationError``）。``code`` 为稳定的大写机器码（如
``ATTACHMENT_TOO_MANY``、``CONFLICT``），客户端按它分支；``detail`` 为面向人的说明，不承诺稳定。
"""

from __future__ import annotations

from pydantic import BaseModel


class ErrorDetail(BaseModel):
    """业务拒绝的响应体：``{"detail": 说明, "code": 机器码}``。"""

    detail: str  # 面向人的错误说明
    code: str  # 稳定的机器可读错误码（大写下划线）

# 由 scripts/gen_sdk_types.sh 根据 docs/api/gateway-openapi.json 自动生成，请勿手改。

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class AuthzIn(BaseModel):
    """
    授权判定请求。
    """

    agent: str = Field(..., title="Agent")
    approved: bool | None = Field(False, title="Approved")
    capability: str = Field(..., title="Capability")
    subject: str = Field(..., title="Subject")
    taint: list[str] | None = Field(None, title="Taint")


class DataClass(StrEnum):
    public = "public"
    internal = "internal"
    confidential = "confidential"


class RiskLevel(StrEnum):
    read = "read"
    write_low = "write_low"
    write_high = "write_high"


class Status(StrEnum):
    draft = "draft"
    published = "published"
    retired = "retired"


class Type(StrEnum):
    mcp_tool = "mcp_tool"
    skill = "skill"
    knowledge = "knowledge"
    dataset = "dataset"
    model = "model"
    composite = "composite"


class Capability(BaseModel):
    """
    一条能力登记记录；`to_tool_spec()` 输出 Runtime 需要的 ToolSpec 字段。
    """

    core_for_agents: list[str] | None = Field(None, title="Core For Agents")
    data_class: DataClass | None = Field("internal", title="Data Class")
    description: str = Field(..., title="Description")
    id: str = Field(..., title="Id")
    input_schema: dict[str, Any] | None = Field(None, title="Input Schema")
    name: str = Field(..., title="Name")
    owner: str | None = Field("", title="Owner")
    requires_idempotency_key: bool | None = Field(False, title="Requires Idempotency Key")
    risk_level: RiskLevel | None = Field("read", title="Risk Level")
    status: Status | None = Field("published", title="Status")
    supports_dry_run: bool | None = Field(False, title="Supports Dry Run")
    system: str | None = Field("", title="System")
    tenant: str | None = Field("default", title="Tenant")
    type: Type | None = Field("mcp_tool", title="Type")
    version: str | None = Field("1.0.0", title="Version")


class ErrorDetail(BaseModel):
    """
    HTTPException 的响应体。业务规则拒绝统一用 409 + ErrorDetail，422 只保留给 FastAPI 参数校验。
    """

    detail: str = Field(..., title="Detail")


class ExchangeIn(BaseModel):
    """
    令牌交换请求。执行者身份不在此声明：请求体里即使混入 actor 等字段也会被忽略。
    """

    agent_id: str = Field(..., title="Agent Id")
    agent_version: str = Field(..., title="Agent Version")
    audience: str = Field(..., title="Audience")
    subject_token: str = Field(..., title="Subject Token")


class McpServerIn(BaseModel):
    """
    MCP Server 登记信息。
    """

    auth_type: str | None = Field("inherit", title="Auth Type")
    endpoint: str = Field(..., title="Endpoint")
    name: str = Field(..., title="Name")
    protocol: str | None = Field("streamable-http", title="Protocol")
    system: str | None = Field("", title="System")


class SearchIn(BaseModel):
    """
    第二级发现请求，由 Runtime 的 search_capabilities 元工具发起。
    """

    agent: str = Field(..., title="Agent")
    intent: str = Field(..., title="Intent")
    limit: int | None = Field(5, title="Limit")
    subject: str = Field(..., title="Subject")


class SkillIn(BaseModel):
    """
    Skill 上架申请。
    """

    capabilities_declared: list[str] | None = Field(None, title="Capabilities Declared")
    eval_dataset_ref: str | None = Field(None, title="Eval Dataset Ref")
    name: str = Field(..., title="Name")
    owner: str = Field(..., title="Owner")
    package_uri: str = Field(..., title="Package Uri")
    risk_level: str | None = Field("read", title="Risk Level")
    scan_passed: bool | None = Field(False, title="Scan Passed")
    signature: str = Field(..., title="Signature")
    version: str = Field(..., title="Version")


class ValidationError(BaseModel):
    ctx: dict[str, Any] | None = Field(None, title="Context")
    input: Any | None = Field(None, title="Input")
    loc: list[str | int] = Field(..., title="Location")
    msg: str = Field(..., title="Message")
    type: str = Field(..., title="Error Type")


class HTTPValidationError(BaseModel):
    detail: list[ValidationError] | None = Field(None, title="Detail")

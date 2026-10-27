# 由 scripts/gen_sdk_types.sh 根据 docs/api/gateway-openapi.json 自动生成，请勿手改。

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, confloat, conint, constr


class AuthzIn(BaseModel):
    """
    授权判定请求；主体取自 X-AI-User-Token（开发态可用 subject / X-AI-Subject）。
    """

    agent: str = Field(..., title="Agent")
    agent_tenant: str | None = Field(None, title="Agent Tenant")
    agent_version: str | None = Field("", title="Agent Version")
    approved: bool | None = Field(False, title="Approved")
    capability: str = Field(..., title="Capability")
    subject: str | None = Field("", title="Subject")
    taint: list[str] | None = Field(None, title="Taint")


class Decision(StrEnum):
    allow = "allow"
    deny = "deny"
    require_approval = "require_approval"


class AuthzOut(BaseModel):
    decision: Decision = Field(..., title="Decision")
    engine: str | None = Field(None, title="Engine")
    policy_ids: list[str] | None = Field(None, title="Policy Ids")
    reason: str | None = Field(None, title="Reason")


class Kind(StrEnum):
    core = "core"
    discoverable = "discoverable"


class Binding(BaseModel):
    """
    Agent 与能力的绑定（替代旧的 core_for_agents）：core 为第一级预载候选，discoverable 为第二级可检索范围。
    agent_id 为 "*" 表示对所有 Agent 生效。
    """

    agent_id: constr(min_length=1, max_length=64) = Field(..., title="Agent Id")
    capability_id: constr(min_length=1, max_length=128) = Field(..., title="Capability Id")
    kind: Kind | None = Field("core", title="Kind")


class BodySubmitSkillSkillsPost(BaseModel):
    package: str = Field(..., json_schema_extra={"contentMediaType": "application/octet-stream"}, title="Package")


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

    compensate_tool: constr(max_length=128) | None = Field(None, title="Compensate Tool")
    connector: constr(max_length=128) | None = Field(None, title="Connector")
    data_class: DataClass | None = Field("internal", title="Data Class")
    description: str = Field(..., title="Description")
    id: constr(min_length=1, max_length=128) = Field(..., title="Id")
    input_schema: dict[str, Any] | None = Field(None, title="Input Schema")
    name: constr(min_length=1, max_length=128) = Field(..., title="Name")
    owner: constr(max_length=128) | None = Field("", title="Owner")
    requires_idempotency_key: bool | None = Field(False, title="Requires Idempotency Key")
    risk_level: RiskLevel | None = Field("read", title="Risk Level")
    row_version: int | None = Field(0, title="Row Version")
    status: Status | None = Field("published", title="Status")
    supports_dry_run: bool | None = Field(False, title="Supports Dry Run")
    system: constr(max_length=64) | None = Field("", title="System")
    tenant: constr(max_length=64) | None = Field("default", title="Tenant")
    type: Type | None = Field("mcp_tool", title="Type")
    version: constr(max_length=32) | None = Field("1.0.0", title="Version")


class ConnectorTool(BaseModel):
    """
    探活时从连接器 manifest 读到的一个工具及其写规范校验结果。
    """

    connector: str = Field(..., title="Connector")
    errors: list[str] | None = Field(None, title="Errors")
    name: str = Field(..., title="Name")
    read_only: bool | None = Field(True, title="Read Only")
    spec: dict[str, Any] | None = Field(None, title="Spec")
    version: str = Field(..., title="Version")


class AuthType(StrEnum):
    inherit = "inherit"
    service = "service"


class Health(StrEnum):
    unknown = "unknown"
    up = "up"
    down = "down"


class Protocol(StrEnum):
    rest = "rest"
    mcp = "mcp"


class Status1(StrEnum):
    inactive = "inactive"
    active = "active"


class ConnectorVersion(BaseModel):
    """
    连接器（MCP Server / REST 工具服务）的一个版本；同名连接器同一时刻只有一个 active 版本。
    """

    activated_manifest_hash: str | None = Field(None, title="Activated Manifest Hash")
    auth_type: AuthType | None = Field("inherit", title="Auth Type")
    consecutive_failures: int | None = Field(0, title="Consecutive Failures")
    endpoint: constr(max_length=512) = Field(..., title="Endpoint")
    health: Health | None = Field("unknown", title="Health")
    last_error: str | None = Field(None, title="Last Error")
    last_probe_at: float | None = Field(None, title="Last Probe At")
    manifest_hash: str | None = Field(None, title="Manifest Hash")
    name: constr(min_length=1, max_length=128) = Field(..., title="Name")
    protocol: Protocol | None = Field("rest", title="Protocol")
    status: Status1 | None = Field("inactive", title="Status")
    system: constr(max_length=64) | None = Field("", title="System")
    version: constr(min_length=1, max_length=32) | None = Field("1", title="Version")


class ConnectorView(BaseModel):
    activated_manifest_hash: str | None = Field(None, title="Activated Manifest Hash")
    auth_type: AuthType | None = Field("inherit", title="Auth Type")
    consecutive_failures: int | None = Field(0, title="Consecutive Failures")
    endpoint: constr(max_length=512) = Field(..., title="Endpoint")
    health: Health | None = Field("unknown", title="Health")
    last_error: str | None = Field(None, title="Last Error")
    last_probe_at: float | None = Field(None, title="Last Probe At")
    manifest_hash: str | None = Field(None, title="Manifest Hash")
    name: constr(min_length=1, max_length=128) = Field(..., title="Name")
    protocol: Protocol | None = Field("rest", title="Protocol")
    status: Status1 | None = Field("inactive", title="Status")
    system: constr(max_length=64) | None = Field("", title="System")
    tools: list[ConnectorTool] | None = Field(None, title="Tools")
    version: constr(min_length=1, max_length=32) | None = Field("1", title="Version")


class ErrorDetail(BaseModel):
    """
    业务错误响应体：detail 为人读描述，code 为稳定的机器可读错误码。
    """

    code: str = Field(..., title="Code")
    detail: str = Field(..., title="Detail")


class EvalItem(BaseModel):
    id: constr(min_length=1, max_length=128) = Field(..., title="Id")
    passed: bool = Field(..., title="Passed")


class EvalOut(BaseModel):
    pass_rate: float | None = Field(..., title="Pass Rate")
    run_ref: str | None = Field(..., title="Run Ref")


class EvalResultIn(BaseModel):
    items: list[EvalItem] = Field(..., max_length=1000, title="Items")
    pass_rate: confloat(ge=0.0, le=1.0) = Field(..., title="Pass Rate")
    run_ref: constr(min_length=1, max_length=256) = Field(..., title="Run Ref")
    tenant: constr(min_length=1, max_length=64) | None = Field(None, title="Tenant")


class EvalSummary(BaseModel):
    items: int | None = Field(..., title="Items")
    pass_rate: float | None = Field(..., title="Pass Rate")


class ExchangeIn(BaseModel):
    """
    令牌交换请求。执行者身份不在此声明：请求体里即使混入 actor 等字段也会被忽略。
    已配置 Keycloak 时用户令牌只取 X-AI-User-Token（已验签），subject_token 被忽略。
    """

    agent_id: str = Field(..., title="Agent Id")
    agent_version: str = Field(..., title="Agent Version")
    audience: str = Field(..., title="Audience")
    subject_token: str | None = Field(None, title="Subject Token")


class InvokeIn(BaseModel):
    """
    工具调用请求（由 Runtime 的工具代理在闸口放行后发出）。
    """

    agent_id: constr(max_length=64) = Field(..., title="Agent Id")
    agent_tenant: constr(max_length=64) | None = Field(None, title="Agent Tenant")
    agent_version: constr(max_length=32) = Field(..., title="Agent Version")
    approved: bool | None = Field(False, title="Approved")
    call_id: constr(max_length=128) | None = Field(None, title="Call Id")
    dry_run: bool | None = Field(False, title="Dry Run")
    idempotency_key: constr(max_length=128) | None = Field(None, title="Idempotency Key")
    input: dict[str, Any] | None = Field(None, title="Input")
    run_id: constr(max_length=40) | None = Field(None, title="Run Id")
    subject: constr(max_length=128) | None = Field("", title="Subject")
    taint: list[str] | None = Field(None, max_length=20, title="Taint")
    tool: constr(max_length=128) = Field(..., title="Tool")


class LockEntry(BaseModel):
    manifest_sha256: str = Field(..., title="Manifest Sha256")
    name: str = Field(..., title="Name")
    version: str = Field(..., title="Version")


class Protocol2(StrEnum):
    rest = "rest"
    http = "http"
    mcp = "mcp"
    streamable_http = "streamable-http"


class McpServerIn(BaseModel):
    """
    连接器登记信息。
    """

    auth_type: AuthType | None = Field("inherit", title="Auth Type")
    endpoint: constr(min_length=1, max_length=512) = Field(..., title="Endpoint")
    name: constr(min_length=1, max_length=128) = Field(..., title="Name")
    protocol: Protocol2 | None = Field("streamable-http", title="Protocol")
    system: constr(max_length=64) | None = Field("", title="System")
    version: constr(min_length=1, max_length=32) | None = Field("1", title="Version")


class ResolveOut(BaseModel):
    lock: list[LockEntry] = Field(..., title="Lock")
    risk_level: str = Field(..., title="Risk Level")


class RevokeIn(BaseModel):
    reason: constr(max_length=512) | None = Field("", title="Reason")


class Revoked(BaseModel):
    manifest_sha256: str | None = Field(..., title="Manifest Sha256")
    name: str = Field(..., title="Name")
    revoked_at: datetime | None = Field(..., title="Revoked At")
    version: str = Field(..., title="Version")


class ScanSummary(BaseModel):
    findings_count: int = Field(..., title="Findings Count")
    passed: bool = Field(..., title="Passed")
    rules_version: str | None = Field(..., title="Rules Version")


class SearchIn(BaseModel):
    """
    第二级发现请求（Runtime 的 search_capabilities 元工具发起）；用户由 X-AI-User-Token 确定。
    """

    agent_id: constr(min_length=1, max_length=64) = Field(..., title="Agent Id")
    agent_tenant: constr(max_length=64) | None = Field(None, title="Agent Tenant")
    agent_version: constr(max_length=32) | None = Field("", title="Agent Version")
    limit: conint(ge=1, le=20) | None = Field(5, title="Limit")
    query: constr(min_length=1, max_length=1000) = Field(..., title="Query")
    scope: list[str] | None = Field(None, title="Scope")
    taint: list[str] | None = Field(None, max_length=20, title="Taint")


class Decision1(StrEnum):
    allow = "allow"
    require_approval = "require_approval"


class SearchItem(BaseModel):
    data_class: str = Field(..., title="Data Class")
    decision: Decision1 = Field(..., title="Decision")
    description: str = Field(..., title="Description")
    id: str = Field(..., title="Id")
    input_schema: dict[str, Any] = Field(..., title="Input Schema")
    name: str = Field(..., title="Name")
    requires_idempotency_key: bool | None = Field(False, title="Requires Idempotency Key")
    risk_level: str = Field(..., title="Risk Level")
    score: float = Field(..., title="Score")
    supports_dry_run: bool | None = Field(False, title="Supports Dry Run")
    system: str = Field(..., title="System")
    tenant: str = Field(..., title="Tenant")


class SearchOut(BaseModel):
    degraded: bool = Field(..., title="Degraded")
    items: list[SearchItem] = Field(..., title="Items")


class SkillListItem(BaseModel):
    eval: EvalSummary | None
    listed_at: datetime | None = Field(..., title="Listed At")
    name: str = Field(..., title="Name")
    reviewed_by: str | None = Field(..., title="Reviewed By")
    revoked_at: datetime | None = Field(..., title="Revoked At")
    scan: ScanSummary | None
    status: str = Field(..., title="Status")
    submitted_by: str = Field(..., title="Submitted By")
    version: str = Field(..., title="Version")


class SkillOut(BaseModel):
    eval: EvalOut | None
    eval_cases: list[dict[str, Any]] | None = Field(None, title="Eval Cases")
    eval_min_pass_rate: float | None = Field(None, title="Eval Min Pass Rate")
    listed_at: datetime | None = Field(..., title="Listed At")
    manifest: dict[str, Any] = Field(..., title="Manifest")
    manifest_sha256: str | None = Field(..., title="Manifest Sha256")
    name: str = Field(..., title="Name")
    reviewed_by: str | None = Field(..., title="Reviewed By")
    revoked_at: datetime | None = Field(..., title="Revoked At")
    scan_report: dict[str, Any] | None = Field(..., title="Scan Report")
    skill_md: str | None = Field(None, title="Skill Md")
    status: str = Field(..., title="Status")
    submitted_by: str = Field(..., title="Submitted By")
    tenant: str = Field(..., title="Tenant")
    version: str = Field(..., title="Version")


class SkillRef(BaseModel):
    name: constr(min_length=1, max_length=64) = Field(..., title="Name")
    version: constr(min_length=1, max_length=32) = Field(..., title="Version")


class SubmitOut(BaseModel):
    name: str = Field(..., title="Name")
    status: str = Field(..., title="Status")
    version: str = Field(..., title="Version")


class ValidationError(BaseModel):
    ctx: dict[str, Any] | None = Field(None, title="Context")
    input: Any | None = Field(None, title="Input")
    loc: list[str | int] = Field(..., title="Location")
    msg: str = Field(..., title="Message")
    type: str = Field(..., title="Error Type")


class VersionIn(BaseModel):
    auth_type: AuthType | None = Field("inherit", title="Auth Type")
    endpoint: constr(min_length=1, max_length=512) = Field(..., title="Endpoint")
    protocol: Protocol2 | None = Field("streamable-http", title="Protocol")
    system: constr(max_length=64) | None = Field(None, title="System")


class HTTPValidationError(BaseModel):
    detail: list[ValidationError] | None = Field(None, title="Detail")


class ResolveIn(BaseModel):
    agent_id: constr(min_length=1, max_length=64) = Field(..., title="Agent Id")
    discovery_scope: list[str] | None = Field(None, max_length=500, title="Discovery Scope")
    skills: list[SkillRef] | None = Field(None, max_length=50, title="Skills")
    tenant: constr(min_length=1, max_length=64) = Field(..., title="Tenant")
    tools: list[str] | None = Field(None, max_length=500, title="Tools")


class RevocationsOut(BaseModel):
    generated_at: datetime = Field(..., title="Generated At")
    revoked: list[Revoked] = Field(..., title="Revoked")


class SkillList(BaseModel):
    items: list[SkillListItem] = Field(..., title="Items")

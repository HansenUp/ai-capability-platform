# 由 scripts/gen_sdk_types.sh 根据 docs/api/runtime-openapi.json 自动生成，请勿手改。

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, confloat, conint, constr


class AgentVersionIn(BaseModel):
    """
    创建/更新一个 Agent 版本快照的请求体。
    """

    agent_id: constr(min_length=1, max_length=64) = Field(..., title="Agent Id")
    config: dict[str, Any] | None = Field(None, title="Config")
    harness: constr(max_length=32) | None = Field(None, title="Harness")
    model_alias: constr(min_length=1, max_length=64) | None = Field("default-chat", title="Model Alias")
    system_prompt: str | None = Field("", title="System Prompt")
    tenant_id: constr(min_length=1, max_length=64) | None = Field("default", title="Tenant Id")
    version: constr(min_length=1, max_length=32) = Field(..., title="Version")


class ApprovalCall(BaseModel):
    """
    批量审批中的一个调用：调用标识与卡片上展示的参数指纹。
    """

    call_id: constr(max_length=128) = Field(..., title="Call Id")
    params_hash: constr(max_length=64) = Field(..., title="Params Hash")


class CardType(StrEnum):
    """
    卡片类型枚举（Contract V1 冻结范围内的全部取值），序列化为字符串。
    """

    impact_preview = "impact_preview"
    form = "form"
    choice = "choice"
    reconciliation_required = "reconciliation_required"


class StopReason(StrEnum):
    end_turn = "end_turn"
    tool_use = "tool_use"
    max_tokens = "max_tokens"
    cancelled = "cancelled"
    error = "error"
    awaiting = "awaiting"


class DoneEvent(BaseModel):
    """
    标记一次 turn（或整个 run）结束，携带停止原因。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    run_id: str = Field(..., title="Run Id")
    stop_reason: StopReason | None = Field("end_turn", title="Stop Reason")
    trace_id: str | None = Field(None, title="Trace Id")
    type: Literal["done"] = Field("done", title="Type")


class ErrorDetail(BaseModel):
    """
    业务拒绝的响应体：``{"detail": 说明, "code": 机器码}``。
    """

    code: str = Field(..., title="Code")
    detail: str = Field(..., title="Detail")


class ErrorEvent(BaseModel):
    """
    运行期错误事件。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    code: str = Field(..., title="Code")
    message: str = Field(..., title="Message")
    retryable: bool | None = Field(False, title="Retryable")
    type: Literal["error"] = Field("error", title="Type")


class FeedbackBody(BaseModel):
    """
    对一次 Run 的用户反馈：点赞 +1 / 点踩 -1，可附说明或修正后的正确答案（可沉淀为评测样本）。
    """

    comment: constr(max_length=2000) | None = Field("", title="Comment")
    correction: constr(max_length=8000) | None = Field(None, title="Correction")
    score: confloat(ge=-1.0, le=1.0) = Field(..., title="Score")


class Action(StrEnum):
    done = "done"
    skip = "skip"
    retry = "retry"


class Kind(StrEnum):
    answer = "answer"
    reconcile = "reconcile"
    credential = "credential"


class InputBody(BaseModel):
    """
    补充输入（S7 §3，D112）。

    - answer：回答 request_user_input，call_id 为该调用，value 为回答（按其 schema 校验，不符 400）；
    - reconcile：结果未知的写工具对账，call_id 为该调用，action ∈ done / skip / retry（retry 只允许下游声明幂等的工具），
      done 时 value 可选，作为补记的结果；
    - credential：用本次请求的令牌（须为发起人）刷新存储的凭据，不从请求体取令牌。
    """

    action: Action | None = Field(None, title="Action")
    call_id: constr(max_length=128) | None = Field(None, title="Call Id")
    kind: Kind = Field(..., title="Kind")
    value: Any | None = Field(None, title="Value")


class InputRequiredEvent(BaseModel):
    """
    Run 需要用户补充输入（对应 RunState.INPUT_REQUIRED）。

    协议字段名为 schema；Python 侧属性名用 input_schema，避免遮蔽 BaseModel 自带的 schema 属性。
    构造时两种名字都接受，序列化时统一输出 schema。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    call_id: str | None = Field(None, title="Call Id")
    prompt: str = Field(..., title="Prompt")
    schema_: dict[str, Any] | None = Field(None, alias="schema", title="Schema")
    type: Literal["input_required"] = Field("input_required", title="Type")


class MessagesRequest(BaseModel):
    """
    /v1/messages 的请求体：类似通用对话补全接口，但绑定到具体 agent_id/agent_version。
    """

    agent_id: str = Field(..., title="Agent Id")
    agent_version: str | None = Field(None, title="Agent Version")
    attachments: list[str] | None = Field(None, max_length=10, title="Attachments")
    messages: list[dict[str, Any]] = Field(..., title="Messages")
    metadata: dict[str, Any] | None = Field(None, title="Metadata")
    session_id: constr(max_length=40) | None = Field(None, title="Session Id")
    stream: bool | None = Field(True, title="Stream")


class RolloutAllowlistItem(RootModel[constr(max_length=128)]):
    root: constr(max_length=128)


class PublishIn(BaseModel):
    """
    发布一个 Agent 版本的请求体，携带评测网关的评测结果引用。
    """

    eval_passed: bool | None = Field(None, title="Eval Passed")
    eval_run_ref: str | None = Field(None, title="Eval Run Ref")
    rollout_allowlist: list[RolloutAllowlistItem] | None = Field(None, max_length=1000, title="Rollout Allowlist")
    rollout_percent: conint(ge=0, le=100) | None = Field(100, title="Rollout Percent")


class ResumeBody(BaseModel):
    """
    续跑请求体（S7 起 messages 可选，D109）：缺省时用服务端存储的初始消息（t_run_input）；传了仍须与 RUN_CREATED
    记录的指纹一致（带附件的 Run 须连同首次驱动时追加的附件说明消息），否则 409。S7 之前创建的 Run 服务端没有存储，
    须回传初始消息。
    """

    messages: list[dict[str, Any]] | None = Field(None, title="Messages")


class AllowlistItem(RootModel[constr(max_length=128)]):
    root: constr(max_length=128)


class RolloutIn(BaseModel):
    """
    调整已发布版本的灰度：放量比例与白名单。
    """

    allowlist: list[AllowlistItem] | None = Field(None, max_length=1000, title="Allowlist")
    percent: conint(ge=0, le=100) = Field(..., title="Percent")


class RunCreate(BaseModel):
    """
    /v1/runs 的创建请求体：面向 A2A 风格的任务提交场景。
    """

    agent_id: str = Field(..., title="Agent Id")
    agent_version: str | None = Field(None, title="Agent Version")
    attachments: list[str] | None = Field(None, max_length=10, title="Attachments")
    input: list[dict[str, Any]] = Field(..., title="Input")
    mode: str | None = Field("async", title="Mode")
    session_id: constr(max_length=40) | None = Field(None, title="Session Id")


class RunEventType(StrEnum):
    """
    Run 事件日志中每一行记录的事件类型，覆盖 Run 生命周期的全部关键节点。
    """

    run_created = "run.created"
    run_state_changed = "run.state_changed"
    context_assembled = "context.assembled"
    model_request = "model.request"
    model_response = "model.response"
    tool_requested = "tool.requested"
    tool_decision = "tool.decision"
    tool_approved = "tool.approved"
    tool_rejected = "tool.rejected"
    tool_executing = "tool.executing"
    tool_executed = "tool.executed"
    tool_result = "tool.result"
    context_compacted = "context.compacted"
    input_requested = "input.requested"
    input_provided = "input.provided"
    capabilities_discovered = "capabilities.discovered"
    run_error = "run.error"
    run_done = "run.done"


class RunEventView(BaseModel):
    """
    事件日志中的一行，字段与契约 RunEvent 完全相同。

    单独起名是为了避开 OpenAPI 组件重名：FastAPI 生成响应 schema 时会省略 `default: null`，
    与导出脚本合并进来的契约 RunEvent schema 不再逐字相同，同名会被导出脚本判为冲突。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    idempotency_key: str | None = Field(None, title="Idempotency Key")
    payload: dict[str, Any] | None = Field(None, title="Payload")
    run_id: str = Field(..., title="Run Id")
    seq: int = Field(..., title="Seq")
    ts: datetime | None = Field(None, title="Ts")
    type: RunEventType


class RunEventsView(RootModel[list[RunEventView]]):
    """
    GET /v1/runs/{run_id}/events：按 seq 升序的 Run 事件日志。
    """

    root: list[RunEventView] = Field(..., description="GET /v1/runs/{run_id}/events：按 seq 升序的 Run 事件日志。", title="RunEventsView")


class RunState(StrEnum):
    """
    States named after the A2A task lifecycle. Naming alignment only; A2A protocol compatibility
    still requires a separate adapter layer for messages, artifacts and stream events.
    （状态命名与 A2A 任务生命周期对齐；仅命名对齐，协议兼容仍需独立适配层。）
    （状态命名对齐 A2A task 生命周期，使 Run API 同时可作为 A2A task 端点使用。）
    """

    submitted = "submitted"
    working = "working"
    auth_required = "auth-required"
    input_required = "input-required"
    completed = "completed"
    failed = "failed"
    canceled = "canceled"


class RunStateView(BaseModel):
    """
    创建 / 拒绝 / 取消 Run 后返回的状态快照。
    """

    run_id: str = Field(..., title="Run Id")
    state: RunState


class RunView(BaseModel):
    """
    GET /v1/runs/{run_id}：单个 Run 的当前状态与元信息。
    """

    agent_id: str = Field(..., title="Agent Id")
    agent_version: str = Field(..., title="Agent Version")
    created_at: str = Field(..., title="Created At")
    last_seq: int = Field(..., title="Last Seq")
    model_alias: str = Field(..., title="Model Alias")
    run_id: str = Field(..., title="Run Id")
    state: RunState
    subject: str = Field(..., title="Subject")


class TextEvent(BaseModel):
    """
    模型文本输出的增量片段（流式追加）。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    delta: str = Field(..., title="Delta")
    type: Literal["text"] = Field("text", title="Type")


class Visibility(StrEnum):
    internal = "internal"
    summary = "summary"
    full = "full"


class ThinkingEvent(BaseModel):
    """
    模型思维链/推理过程的增量片段（流式追加）。

    visibility 默认 internal（失败关闭）：runtime 按 Agent 版本配置的 thinking_visibility 覆写该字段，
    并在 SSE 输出处丢弃 internal 级别的事件，使其只保留在事件日志中。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    delta: str = Field(..., title="Delta")
    type: Literal["thinking"] = Field("thinking", title="Type")
    visibility: Visibility | None = Field("internal", title="Visibility")


class Status(StrEnum):
    ok = "ok"
    error = "error"
    denied = "denied"
    pending_approval = "pending_approval"


class ToolResultEvent(BaseModel):
    """
    工具执行结果（或被拒绝/待审批的状态），回传给模型继续对话。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    call_id: str = Field(..., title="Call Id")
    output: Any | None = Field(None, title="Output")
    output_ref: str | None = Field(None, title="Output Ref")
    status: Status | None = Field("ok", title="Status")
    type: Literal["tool_result"] = Field("tool_result", title="Type")


class ToolUseEvent(BaseModel):
    """
    模型请求调用某个工具（harness 产出，经由 gate 判定后由 runtime 执行）。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    call_id: str = Field(..., title="Call Id")
    input: dict[str, Any] | None = Field(None, title="Input")
    tool_name: str = Field(..., title="Tool Name")
    type: Literal["tool_use"] = Field("tool_use", title="Type")


class UsageEvent(BaseModel):
    """
    一轮模型调用的 token 用量与成本统计，用于计费和观测。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    cache_read_tokens: int | None = Field(0, title="Cache Read Tokens")
    cost: float | None = Field(0.0, title="Cost")
    input_tokens: int | None = Field(0, title="Input Tokens")
    model: str | None = Field(None, title="Model")
    output_tokens: int | None = Field(0, title="Output Tokens")
    type: Literal["usage"] = Field("usage", title="Type")


class ValidationError(BaseModel):
    ctx: dict[str, Any] | None = Field(None, title="Context")
    input: Any | None = Field(None, title="Input")
    loc: list[str | int] = Field(..., title="Location")
    msg: str = Field(..., title="Message")
    type: str = Field(..., title="Error Type")


class ApprovalBody(BaseModel):
    """
    审批/拒绝工具调用请求体：单个调用给 call_id + params_hash；并行批次的影响预览卡（payload.calls[]）可用 calls 一次批多个
    （S9，D149），两种写法二选一。

    params_hash 是必填项：审批附着于具体参数与资源，审批人必须回传卡片上展示的那一份参数指纹，
    否则无从判断"批的到底是哪一组参数"。
    """

    call_id: str | None = Field(None, title="Call Id")
    calls: list[ApprovalCall] | None = Field(None, title="Calls")
    comment: str | None = Field("", title="Comment")
    params_hash: str | None = Field(None, title="Params Hash")
    ttl_seconds: int | None = Field(900, title="Ttl Seconds")


class ApprovalView(BaseModel):
    """
    批准后返回：Run 回到 working。持久化执行的 Run（S7，D108）已入队由 Worker 以发起人凭据自动续跑；发起人也可调用
    resume 指向的地址（抢到租约则内联执行，否则尾随事件流）。
    """

    resume: str = Field(..., title="Resume")
    run_id: str = Field(..., title="Run Id")
    state: RunState


class CardEvent(BaseModel):
    """
    富交互卡片事件，用于向客户端展示结构化内容（如高风险操作的影响预览）。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    card_type: CardType
    payload: dict[str, Any] | None = Field(None, title="Payload")
    type: Literal["card"] = Field("card", title="Type")


class Event(
    RootModel[
        TextEvent | ThinkingEvent | ToolUseEvent | ToolResultEvent | CardEvent | InputRequiredEvent | UsageEvent | ErrorEvent | DoneEvent
    ]
):
    root: (
        TextEvent | ThinkingEvent | ToolUseEvent | ToolResultEvent | CardEvent | InputRequiredEvent | UsageEvent | ErrorEvent | DoneEvent
    ) = Field(..., discriminator="type", title="Event")


class HTTPValidationError(BaseModel):
    detail: list[ValidationError] | None = Field(None, title="Detail")


class InputView(BaseModel):
    """
    POST /v1/runs/{run_id}/input：补充输入已记录，Run 回到 working 并已入队续跑。
    """

    call_id: str | None = Field(None, title="Call Id")
    kind: str = Field(..., title="Kind")
    run_id: str = Field(..., title="Run Id")
    state: RunState


class RunEvent(BaseModel):
    """
    One row of the append-only event log. (run_id, seq) is the primary key.
    （追加写事件日志中的一行记录；(run_id, seq) 组合为主键，seq 在同一 run 内严格递增，
    是重放/断点续跑与幂等判断的基础。）
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    idempotency_key: str | None = Field(None, title="Idempotency Key")
    payload: dict[str, Any] | None = Field(None, title="Payload")
    run_id: str = Field(..., title="Run Id")
    seq: int = Field(..., title="Seq")
    ts: datetime | None = Field(None, title="Ts")
    type: RunEventType


class RunListItem(BaseModel):
    """
    GET /v1/runs 列表项：RunView 加关联会话标识（S7）。
    """

    agent_id: str = Field(..., title="Agent Id")
    agent_version: str = Field(..., title="Agent Version")
    created_at: str = Field(..., title="Created At")
    last_seq: int = Field(..., title="Last Seq")
    model_alias: str = Field(..., title="Model Alias")
    run_id: str = Field(..., title="Run Id")
    session_id: str | None = Field(None, title="Session Id")
    state: RunState
    subject: str = Field(..., title="Subject")


class RunListView(BaseModel):
    """
    GET /v1/runs：按 (created_at, run_id) 倒序的键集分页；next_cursor 为不透明游标，无更多时为 null。
    """

    items: list[RunListItem] = Field(..., title="Items")
    next_cursor: str | None = Field(None, title="Next Cursor")

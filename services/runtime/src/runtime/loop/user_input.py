"""input-required（S7 §3，D112）：平台工具 request_user_input、凭据刷新请求，以及供 /input 接口调用的 provide_input。

- ``request_user_input{prompt, schema?}``：Agent 版本配置 ``user_input: true`` 时由 runner 注入工具列表；工具代理在闸口前拦截
  （pre_tool hook 照常），写 TOOL_REQUESTED、INPUT_REQUESTED{kind:answer, call_id, prompt, schema}，CAS 迁 input-required，
  下发 InputRequiredEvent + done(awaiting)。发起人经 ``provide_input(kind=answer)`` 回答后写 INPUT_PROVIDED + TOOL_RESULT(ok)
  （post_tool hook 照常），CAS 回 working，由调用方入队续跑。
- 结果未知（有 TOOL_EXECUTING 无 TOOL_EXECUTED / TOOL_RESULT）的对账：``kind=reconcile``，action ∈ done / skip / retry；
  done 补写 EXECUTED + RESULT，skip 补写 RESULT(error)，retry 只允许下游声明幂等（requires_idempotency_key）的工具——
  记 INPUT_PROVIDED{action:retry} 并把 t_tool_operation 置 retry，续跑时工具代理认领后以同一幂等键重做。
- 凭据：drive 开始或写工具执行前令牌缺失 / 剩余有效期不足时写 INPUT_REQUESTED{kind:credential}；``kind=credential`` 时
  在同一事务里覆盖存储的发起人令牌（或由 API 层先行存储）后 CAS 回 working。

本模块只负责事件与状态，入队由调用方（API 层）完成。只允许 Run 发起人提供输入（D112）。
"""

from __future__ import annotations

import json
import math
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_contract import HookPoint, InputRequiredEvent, RiskLevel, RunEvent, RunEventType, RunState, ToolSpec
from runtime import credentials
from runtime.config import settings
from runtime.context import budget as ctx
from runtime.guard.hooks import HookChain, HookConfigError
from runtime.state.db import AgentVersion, Run, SessionLocal
from runtime.state.eventlog import EventLog, RunNotFound

TOOL_NAME = "request_user_input"
TOOL = ToolSpec(
    name=TOOL_NAME,
    description=(
        "向本次任务的发起人提问并等待回答（任务挂起，回答后继续）。仅在缺少继续所必需、且无法通过其他工具获得的信息时使用。"
        "prompt 为要问的问题；schema 可选，为期望回答的 JSON Schema（不给则接受自由文本）。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "要向发起人提出的问题"},
            "schema": {"type": "object", "description": "可选：期望回答的 JSON Schema"},
        },
        "required": ["prompt"],
    },
)

KIND_ANSWER, KIND_RECONCILE, KIND_CREDENTIAL = "answer", "reconcile", "credential"
KINDS = (KIND_ANSWER, KIND_RECONCILE, KIND_CREDENTIAL)
RECONCILE_ACTIONS = ("done", "skip", "retry")

# t_tool_operation.status 的约定取值（工具代理与对账共用）
OP_EXECUTING, OP_EXECUTED, OP_UNKNOWN, OP_SKIPPED, OP_RETRY = "executing", "executed", "unknown", "skipped", "retry"

MAX_PROMPT_CHARS = 4000
MAX_SCHEMA_BYTES = 16 * 1024  # request_user_input 的 schema 序列化后上限
MAX_SCHEMA_DEPTH = 8  # schema 的对象 / 数组嵌套层数上限（校验回答时按 schema 递归，限制深度即限制递归）
MAX_ANSWER_DEPTH = 32  # 回答本身的嵌套层数上限（无 schema 时也适用；回答原样进事件日志）


class InputForbidden(PermissionError):
    """提供输入者不是 Run 发起人（API 映射 403）。"""


class InputNotPending(RuntimeError):
    """Run 不在 input-required，或没有与请求匹配的待补充输入（API 映射 409）。"""


def enabled(agent: AgentVersion) -> bool:
    """Agent 版本快照是否开启 request_user_input（config.user_input 为 true）。"""
    return (agent.config or {}).get("user_input") is True


def parse_request(tool_input: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """校验 request_user_input 的入参，返回 (prompt, schema)；不合规抛 ValueError（作为工具错误结果交还模型）。"""
    prompt = tool_input.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("request_user_input: prompt must be a non-empty string")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f"request_user_input: prompt longer than {MAX_PROMPT_CHARS} characters")
    schema = tool_input.get("schema")
    if schema is not None and not isinstance(schema, dict):
        raise ValueError("request_user_input: schema must be a JSON Schema object")
    if schema is not None:
        if json_depth(schema) > MAX_SCHEMA_DEPTH:
            raise ValueError(f"request_user_input: schema nested deeper than {MAX_SCHEMA_DEPTH} levels")
        if len(json.dumps(schema, ensure_ascii=False).encode()) > MAX_SCHEMA_BYTES:
            raise ValueError(f"request_user_input: schema larger than {MAX_SCHEMA_BYTES} bytes")
    return prompt, schema


def json_depth(value: Any) -> int:
    """JSON 值的对象 / 数组嵌套层数（标量为 0）；迭代实现，任意深度都不会触发 RecursionError。"""
    deepest, stack = 0, [(value, 0)]
    while stack:
        v, d = stack.pop()
        if isinstance(v, dict | list):
            d += 1
            deepest = max(deepest, d)
            stack.extend((x, d) for x in (v.values() if isinstance(v, dict) else v))
    return deepest


async def request_answer(log: EventLog, run_id: str, call_id: str, prompt: str, schema: dict[str, Any] | None) -> InputRequiredEvent:
    """写 INPUT_REQUESTED{kind:answer} 并迁 input-required（调用方提交），返回要下发的 InputRequiredEvent。"""
    await log.append(run_id, RunEventType.INPUT_REQUESTED, {"kind": KIND_ANSWER, "call_id": call_id, "prompt": prompt, "schema": schema})
    await log.transition(run_id, RunState.INPUT_REQUIRED, "awaiting user input")
    return InputRequiredEvent.model_validate({"prompt": prompt, "schema": schema, "call_id": call_id})


async def request_credential(log: EventLog, run_id: str, call_id: str | None, reason: str) -> InputRequiredEvent:
    """令牌缺失（missing）或剩余有效期不足（expiring）：写 INPUT_REQUESTED{kind:credential} 并迁 input-required
    （调用方提交；Run 须已在 working）。不记令牌本身。"""
    await log.append(run_id, RunEventType.INPUT_REQUESTED, {"kind": KIND_CREDENTIAL, "call_id": call_id, "reason": reason})
    await log.transition(run_id, RunState.INPUT_REQUIRED, f"credential {reason}")
    return InputRequiredEvent(prompt=f"credential {reason}: re-authenticate to continue this run", call_id=call_id)


def pending_reconciles(prior: list[RunEvent]) -> list[dict[str, Any]]:
    """结果未知窗口：有 TOOL_EXECUTING 却没有对应 TOOL_EXECUTED / TOOL_RESULT 的**写**调用（按日志顺序，可能多个——并行批次）。
    发起人对账选择 retry（INPUT_PROVIDED{kind:reconcile, action:retry}）之后、重做写下新 TOOL_EXECUTING 之前，该调用不算未知。

    只读调用（TOOL_REQUESTED.risk_level=read）的孤儿 TOOL_EXECUTING 不算未知（S9，D149）：只读没有副作用，接管 / 续跑时
    作为尚无结果的调用直接重执行，不转人工对账。"""
    settled = {e.payload["call_id"] for e in prior if e.type in (RunEventType.TOOL_EXECUTED, RunEventType.TOOL_RESULT)}
    reads = {
        str(e.payload.get("call_id"))
        for e in prior
        if e.type == RunEventType.TOOL_REQUESTED and str(e.payload.get("risk_level")) == RiskLevel.READ.value
    }
    unknown: dict[str, dict[str, Any]] = {}
    for e in prior:
        cid = str(e.payload.get("call_id"))
        if e.type == RunEventType.TOOL_EXECUTING and cid not in settled and cid not in reads:
            unknown.pop(cid, None)
            unknown[cid] = {"call_id": cid, "tool": e.payload.get("tool"), "operation_id": e.payload.get("operation_id")}
        elif e.type == RunEventType.INPUT_PROVIDED and e.payload.get("kind") == KIND_RECONCILE and e.payload.get("action") == "retry":
            unknown.pop(cid, None)
    return list(unknown.values())


def pending_reconcile(prior: list[RunEvent]) -> dict[str, Any] | None:
    """结果未知的写调用中日志里最后一个（单个调用时即唯一的那个）；没有返回 None。全部见 pending_reconciles。"""
    unknown = pending_reconciles(prior)
    return unknown[-1] if unknown else None


# ---------------- 最小 JSON Schema 校验（runtime 不依赖 jsonschema） ----------------

_TYPES: dict[str, Any] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "null": lambda v: v is None,
}


def validate_answer(schema: dict[str, Any] | None, value: Any, path: str = "$") -> None:
    """按 JSON Schema 的常用子集校验回答（type / enum / const / required / properties / additionalProperties=false /
    items / min/maxLength / minimum / maximum / min/maxItems），不符抛 ValueError（API 映射 400）。schema 为空时只要求有值。"""
    if schema is None:
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError(f"{path}: an answer is required")
        return
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_TYPES.get(x, lambda v: False)(value) for x in types):
            raise ValueError(f"{path}: expected type {t}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: must be one of {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"{path}: must equal {schema['const']!r}")
    if isinstance(value, str):
        if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
            raise ValueError(f"{path}: shorter than {schema['minLength']}")
        if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
            raise ValueError(f"{path}: longer than {schema['maxLength']}")
    if _TYPES["number"](value):
        if isinstance(schema.get("minimum"), int | float) and value < schema["minimum"]:
            raise ValueError(f"{path}: less than {schema['minimum']}")
        if isinstance(schema.get("maximum"), int | float) and value > schema["maximum"]:
            raise ValueError(f"{path}: greater than {schema['maximum']}")
    if isinstance(value, dict):
        props: dict[str, Any] = schema["properties"] if isinstance(schema.get("properties"), dict) else {}
        for key in schema.get("required") or []:
            if key not in value:
                raise ValueError(f"{path}.{key}: is required")
        if schema.get("additionalProperties") is False:
            extra = sorted(k for k in value if k not in props)
            if extra:
                raise ValueError(f"{path}: unexpected properties {extra}")
        for key, sub in props.items():
            if key in value and isinstance(sub, dict):
                validate_answer(sub, value[key], f"{path}.{key}")
    if isinstance(value, list):
        if isinstance(schema.get("minItems"), int) and len(value) < schema["minItems"]:
            raise ValueError(f"{path}: fewer than {schema['minItems']} items")
        if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
            raise ValueError(f"{path}: more than {schema['maxItems']} items")
        if isinstance(schema.get("items"), dict):
            for i, item in enumerate(value):
                validate_answer(schema["items"], item, f"{path}[{i}]")


# ---------------- provide_input（供 WP-D 的 POST /v1/runs/{id}/input） ----------------


def _pending_request(prior: list[RunEvent], kind: str) -> RunEvent | None:
    """最近一条尚未被 INPUT_PROVIDED 回应的 INPUT_REQUESTED（且种类为 kind）。"""
    pending: RunEvent | None = None
    for e in prior:
        if e.type == RunEventType.INPUT_REQUESTED:
            pending = e
        elif e.type == RunEventType.INPUT_PROVIDED and pending is not None and e.payload.get("kind") == pending.payload.get("kind"):
            pending = None
    return pending if pending is not None and pending.payload.get("kind") == kind else None


async def _agent_of(s: AsyncSession, run: Run) -> AgentVersion | None:
    q = select(AgentVersion).where(
        AgentVersion.agent_id == run.agent_id, AgentVersion.version == run.agent_version, AgentVersion.tenant_id == run.tenant_id
    )
    return (await s.execute(q)).scalars().first()


async def provide_input(
    run_id: str,
    subject: str,
    kind: str,
    call_id: str | None = None,
    value: Any = None,
    action: str | None = None,
    user_token: str | None = None,
) -> dict[str, Any]:
    """发起人为 input-required 的 Run 补充输入，同一事务写事件并 CAS 回 working；返回 ``{run_id, state, kind, call_id}``。
    对账（reconcile）时若还有其他结果未知的调用（并行批次），Run 保持 input-required（返回的 state 即如此），全部处置后才回 working。

    - answer：call_id 须为待回答的 request_user_input 调用；value 按其 schema 校验（不符 ValueError）；
    - reconcile：call_id 须为结果未知的调用；action ∈ done / skip / retry（done 时 value 可选，作为补记的结果）；retry 时以
      user_token（发起人本次请求的令牌）向能力网关查询工具元数据——服务态网关的发现接口必须带用户令牌；
    - credential：value 为新令牌时在同一事务内覆盖存储（剩余有效期不足 ValueError）；为 None 时要求 API 层已存入有效凭据。

    异常：Run 不存在 RunNotFound（404）；非发起人 InputForbidden / CredentialSubjectMismatch（403）；不在 input-required
    或无匹配的待补充输入 InputNotPending（409）；输入不合规 ValueError（400）；并发写者抢先 SeqConflict（409，可重试）。"""
    async with SessionLocal() as s:
        result = await apply_input(s, run_id, subject, kind, call_id, value, action, user_token)
        await s.commit()
    return result


async def apply_input(
    s: AsyncSession,
    run_id: str,
    subject: str,
    kind: str,
    call_id: str | None = None,
    value: Any = None,
    action: str | None = None,
    user_token: str | None = None,
    attempt_id: str | None = None,
    value_hmac: str | None = None,
) -> dict[str, Any]:
    """复用输入规则，事务提交由调用者持有；工作台回执与输入原子提交。"""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    run = await s.get(Run, run_id)
    if run is None:
        raise RunNotFound(run_id)
    if subject != run.subject:
        raise InputForbidden("only the run initiator can provide input")
    if run.state != RunState.INPUT_REQUIRED:
        raise InputNotPending(f"run state is {run.state}")
    log = EventLog(s)
    prior = await log.list(run_id)
    state = RunState.WORKING
    if kind == KIND_ANSWER:
        await _answer(s, log, run, prior, call_id, value, attempt_id, value_hmac)
    elif kind == KIND_RECONCILE:
        await _reconcile(s, log, run, prior, call_id, action, value, user_token)
        if pending_reconciles(await log.list(run_id)):
            state = RunState.INPUT_REQUIRED
    else:
        call_id = await _credential(s, log, run, prior, subject, value, attempt_id)
    if state == RunState.WORKING:
        await log.transition(run_id, RunState.WORKING, f"input {kind}")
    return {"run_id": run_id, "state": state.value, "kind": kind, "call_id": call_id}


def validate_answer_request(prior: list[RunEvent], call_id: str | None, value: Any) -> RunEvent:
    req = _pending_request(prior, KIND_ANSWER)
    answered = {e.payload.get("call_id") for e in prior if e.type == RunEventType.TOOL_RESULT}
    if req is None or req.payload.get("call_id") != call_id or call_id in answered:
        raise InputNotPending(f"no pending user-input request for call {call_id!r}")
    if json_depth(value) > MAX_ANSWER_DEPTH:
        raise ValueError(f"answer nested deeper than {MAX_ANSWER_DEPTH} levels")
    try:
        validate_answer(req.payload.get("schema"), value)
    except RecursionError as e:  # 回答嵌套过深（schema 深度已在请求时限制）：按不合规处理（400）
        raise ValueError("answer is nested too deeply") from e
    return req


async def _answer(
    s: AsyncSession,
    log: EventLog,
    run: Run,
    prior: list[RunEvent],
    call_id: str | None,
    value: Any,
    attempt_id: str | None = None,
    value_hmac: str | None = None,
) -> None:
    validate_answer_request(prior, call_id, value)
    if attempt_id is not None and value_hmac is None:
        raise ValueError("workbench answer requires a content binding")
    from runtime.loop.toolbroker import hmac_json  # 延迟导入：toolbroker 在运行期导入本模块

    status, output, data_class = "ok", value, TOOL.data_class
    extra: dict[str, Any] = {}
    agent = await _agent_of(s, run)
    try:
        from runtime.loop.runner import thinking_visibility

        chain = HookChain.for_agent(agent.config if agent else None, thinking_visibility(agent) if agent else "internal")
    except HookConfigError:
        chain = None  # 快照 hooks 不合规：失败关闭，扣留回答（续跑时 runner 同样以 HOOK_CONFIG_INVALID 失败）
        status, output = "error", {"error": "output withheld: hook config invalid"}
    if chain is not None and chain.has(HookPoint.POST_TOOL):
        raw_digest = hmac_json(value)
        post = await chain.run(
            HookPoint.POST_TOOL, {"call_id": call_id, "tool": TOOL_NAME, "status": status, "output": value, "data_class": data_class}
        )
        if post.denied:
            status, output = "error", {"error": f"output withheld by hook:{post.denied_by}"}
        else:
            output, data_class = post.payload["output"], post.payload["data_class"]
        extra = {"hooks": post.records, "raw_output_hmac": raw_digest}
    summary, ref = ctx.offload_if_large(output, f"{settings.workspace_dir}/{run.run_id}", settings.tool_result_offload_tokens)
    provided: dict[str, Any] = {"kind": KIND_ANSWER, "call_id": call_id}
    if attempt_id is not None:
        provided.update(workbench_attempt_id=attempt_id, workbench_value_hmac=value_hmac)
    await log.append(run.run_id, RunEventType.INPUT_PROVIDED, provided)
    await log.append(
        run.run_id,
        RunEventType.TOOL_RESULT,
        {"call_id": call_id, "status": status, "output": summary, "output_ref": ref, "data_class": data_class} | extra,
    )


async def _reconcile(
    s: AsyncSession,
    log: EventLog,
    run: Run,
    prior: list[RunEvent],
    call_id: str | None,
    action: str | None,
    value: Any,
    user_token: str | None = None,
) -> None:
    pending = next((u for u in pending_reconciles(prior) if u["call_id"] == call_id), None)
    if pending is None:
        raise InputNotPending(f"no unknown tool result awaiting reconciliation for call {call_id!r}")
    if action not in RECONCILE_ACTIONS:
        raise ValueError(f"action must be one of {', '.join(RECONCILE_ACTIONS)}")
    op_id = pending.get("operation_id")
    if action == "retry" and pending.get("tool") == "run_skill_script":
        raise ValueError("signed scripts do not declare downstream idempotency; reconcile with done or skip, never retry")
    script_extra: dict[str, Any] = {}
    if pending.get("tool") == "run_skill_script":
        from runtime.skills.execution import BINDING, stop_operation

        requested = next(event for event in prior if event.type == RunEventType.TOOL_REQUESTED and event.payload.get("call_id") == call_id)
        binding = requested.payload.get("effective_input", {}).get(BINDING, {})
        await stop_operation(op_id, binding)
        script_extra = {"data_class": binding.get("data_class", "confidential"), "skill": binding}
    if action == "retry":
        agent = await _agent_of(s, run)
        tool = None
        if agent is not None:
            from runtime.assemble.assembler import assemble
            from runtime.skills.verify import SkillVerifyError

            try:
                assembled = await assemble(agent, run.subject, user_token)
            except SkillVerifyError as e:  # 技能验签 / 吊销失败：无法确认工具元数据，不允许重做（失败关闭）
                raise ValueError(f"{e.code}: cannot assemble the agent's tools to verify retry ({e.message})") from e
            tool = next((t for t in assembled.tools if t.name == pending["tool"]), None)
        if tool is None or not tool.requires_idempotency_key:
            raise ValueError(
                f"retry is only allowed for tools whose downstream declares idempotency (use done or skip): {pending['tool']!r}"
            )
    await log.append(run.run_id, RunEventType.INPUT_PROVIDED, {"kind": KIND_RECONCILE, "call_id": call_id, "action": action})
    if action == "retry":
        if op_id:
            await log.update_operation(op_id, OP_RETRY)
        return
    if action == "done":
        await log.append(
            run.run_id,
            RunEventType.TOOL_EXECUTED,
            {"call_id": call_id, "tool": pending["tool"], "reconciled": "done"},
            idempotency_key=op_id,
        )
        output = value if value is not None else {"reconciled": "done"}
        summary, ref = ctx.offload_if_large(output, f"{settings.workspace_dir}/{run.run_id}", settings.tool_result_offload_tokens)
        await log.append(
            run.run_id,
            RunEventType.TOOL_RESULT,
            {"call_id": call_id, "status": "ok", "output": summary, "output_ref": ref} | script_extra,
        )
        if op_id:
            await log.update_operation(op_id, OP_EXECUTED)
        return
    await log.append(
        run.run_id,
        RunEventType.TOOL_RESULT,
        {"call_id": call_id, "status": "error", "output": {"error": "skipped by the run initiator after an unknown result"}} | script_extra,
    )
    if op_id:
        await log.update_operation(op_id, OP_SKIPPED)


async def _credential(
    s: AsyncSession, log: EventLog, run: Run, prior: list[RunEvent], subject: str, value: Any, attempt_id: str | None = None
) -> str | None:
    req = _pending_request(prior, KIND_CREDENTIAL)
    if req is None:
        raise InputNotPending("no pending credential request")
    if value is not None:
        if not isinstance(value, str) or not value:
            raise ValueError("credential value must be the initiator's access token")
        if not credentials.is_fresh(credentials.token_expiry(value)):
            raise ValueError(f"credential expires within {settings.credential_min_ttl_s:g}s")
        await credentials.store_credential(s, run.run_id, subject, value)
    else:
        stored = await credentials.load_credential(run.run_id)
        if stored is None or not credentials.is_fresh(stored.expires_at):
            raise ValueError("no fresh credential stored for this run")
    cid = req.payload.get("call_id")
    payload = {"kind": KIND_CREDENTIAL, "call_id": cid}
    if attempt_id is not None:
        payload["workbench_attempt_id"] = attempt_id
    await log.append(run.run_id, RunEventType.INPUT_PROVIDED, payload)
    return cid if isinstance(cid, str) else None

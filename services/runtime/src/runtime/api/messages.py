"""Messages API (HTTP + SSE) and Run API (A2A-named states, approval/input/cancel, event stream).
（Messages API：兼容通用对话形态的 HTTP/SSE 入口；Run API：以 A2A 任务状态命名，提供
审批/续跑/取消/事件流查询等围绕 Run 生命周期的操作。两组 API 共享同一条底层驱动
（runtime.loop.runner.drive），只是入口形态不同。）
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import aclosing
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from ai_contract import (
    IMAGE_MEDIA_TYPES,
    IMAGE_REF_TYPE,
    DoneEvent,
    ErrorEvent,
    Event,
    RunEvent,
    RunEventType,
    RunState,
    TextEvent,
    ThinkingEvent,
    sniff_media_type,
)
from runtime import credentials
from runtime.api import rollout
from runtime.api.auth import Principal, approver_denial, check_approver, current_principal, is_admin
from runtime.api.views import ApiError, ApprovalView, ErrorDetail, InputView, RunEventsView, RunStateView, RunView
from runtime.config import settings
from runtime.harness import registry
from runtime.loop import runner, user_input
from runtime.loop.toolbroker import ARTIFACT_REF
from runtime.run import invariant
from runtime.state.db import AgentVersion, Run, RunEventRow, RunInput, SessionLocal
from runtime.state.eventlog import EventLog, RunNotFound, SeqConflict, StaleWriter
from runtime.worker import enqueue, lease, publish_cancel

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(current_principal)])  # 全部 /v1 接口要求调用方身份（AUTH_REQUIRED=true 时即 JWT）


class MessagesRequest(BaseModel):
    """/v1/messages 的请求体：类似通用对话补全接口，但绑定到具体 agent_id/agent_version。"""

    agent_id: str  # 目标 Agent 标识
    agent_version: str | None = None  # 指定版本，缺省取该 agent 已发布的最新版本
    session_id: str | None = Field(default=None, max_length=40)  # 会话标识（多轮关联，Run 列表可按它过滤；与 t_run 列宽一致）
    messages: list[dict[str, Any]]  # 本轮输入消息
    # 附件：调用方自己此前 Run 产生的外置工件引用（artifact://<id>）；他人的工件一律 403（S4.8 附件隔离）。
    # 工件为图片（png / jpeg / webp / gif）时作为图片输入交给模型（S6，D102），见 messages 接口说明。
    attachments: list[str] = Field(default_factory=list, max_length=10)
    stream: bool = True  # 是否以 SSE 流式返回
    metadata: dict[str, Any] = Field(default_factory=dict)  # 附加元数据


async def _load_agent(
    agent_id: str, version: str | None, tenant_id: str, subject: str | None = None, *, allow_unpublished: bool = False
) -> tuple[AgentVersion, str]:
    """加载调用方租户内的 Agent 版本快照，返回（版本，选版原因）。

    - 指定 version：只能是已发布版本（pinned）——否则已回滚的版本、未过发布闸门的草稿都能被直接调用；
      0% 预发布版本（D66）还要求调用方在其白名单内（与 rollout.choose 同一判定），否则同样按不存在处理；
      续跑（resume）固定原 Run 的版本，传 allow_unpublished=True，不受该版本之后被回滚 / 白名单变化的影响；
    - 未指定：在已发布版本中按主体做哈希灰度选版（stable / canary / allowlist / only，见 runtime.api.rollout）。
    其他租户的 Agent 与不存在同样返回 404，不泄露存在性。"""
    async with SessionLocal() as s:
        q = select(AgentVersion).where(AgentVersion.agent_id == agent_id, AgentVersion.tenant_id == tenant_id)
        if version:
            q = q.where(AgentVersion.version == version)
            if not allow_unpublished:
                q = q.where(AgentVersion.status == "published")
            agent = (await s.execute(q)).scalars().first()
            hidden = agent is not None and agent.rollout_percent == 0 and not rollout.in_allowlist(agent, subject or "")
            if hidden and not allow_unpublished:
                agent = None  # 0% 预发布版本只对白名单开放：非白名单主体指定该版本与不存在同样 404，不泄露存在性
            reason = "pinned"
        else:
            published = (await s.execute(q.where(AgentVersion.status == "published"))).scalars().all()
            agent, reason = rollout.choose(published, subject or "")
    if agent is None:
        raise HTTPException(404, f"agent '{agent_id}' has no published version" + (f" '{version}'" if version else ""))
    return agent, reason


async def run_in_tenant(run_id: str, request: Request, principal: Principal = Depends(current_principal)) -> None:
    """Run 级租户隔离：Run 不存在或不属于调用方租户一律 404；同时把 Run 记为本次请求的审计对象。"""
    async with SessionLocal() as s:
        run = await s.get(Run, run_id)
    if run is None or run.tenant_id != principal.tenant_id:
        raise HTTPException(404, f"run '{run_id}' not found")
    request.state.audit_object = f"run:{run_id}"


async def _approvers(s: AsyncSession, run_id: str, types: tuple[RunEventType, ...]) -> set[str]:
    """本 Run 事件日志中已记录的审批人主体（TOOL_APPROVED / TOOL_REJECTED 的 approver 字段）。"""
    return {str(e.payload["approver"]) for e in await EventLog(s).list(run_id) if e.type in types and e.payload.get("approver")}


def _forbid(request: Request, reason: str) -> HTTPException:
    """403，并把原因留给审计。"""
    request.state.auth_error = reason
    return HTTPException(403, reason)


# 待审批期间、尚未成为本 Run 审批人的有审批资格者只能看到审批所需的事件（审批视图，见 _approval_view）：
# run.created（只留元数据）、run.state_changed、待批调用的 tool.requested（只留调用与参数，不含思维链等）与其 tool.decision。
APPROVAL_VIEW_EVENTS = frozenset(
    {
        RunEventType.RUN_CREATED,
        RunEventType.RUN_STATE_CHANGED,
        RunEventType.TOOL_REQUESTED,
        RunEventType.TOOL_DECISION,
    }
)
# 审批视图中各事件保留的字段（白名单；其余如 tool.requested 的 reasoning / hooks、run.created 的附件与消息指纹一律去掉）
APPROVAL_VIEW_FIELDS: dict[RunEventType, frozenset[str]] = {
    RunEventType.RUN_CREATED: frozenset({"agent_id", "agent_version", "model_alias", "subject", "actor", "rollout"}),
    RunEventType.TOOL_REQUESTED: frozenset({"call_id", "tool", "input", "effective_input", "risk_level"}),
}
RunAccess = Literal["full", "approval"]


def _approval_view(events: list[RunEvent]) -> list[RunEvent]:
    """审批视图：只保留待批调用（已请求、尚无结果的最近一次 tool.requested）的请求与闸口决策，加上 run.created
    （只留元数据）与 run.state_changed；字段按 APPROVAL_VIEW_FIELDS 收窄。"""
    resulted = {e.payload.get("call_id") for e in events if e.type == RunEventType.TOOL_RESULT}
    pending = [
        e.payload.get("call_id") for e in events if e.type == RunEventType.TOOL_REQUESTED and e.payload.get("call_id") not in resulted
    ]
    pending_call = pending[-1] if pending else None
    out: list[RunEvent] = []
    for e in events:
        if e.type not in APPROVAL_VIEW_EVENTS:
            continue
        if e.type in (RunEventType.TOOL_REQUESTED, RunEventType.TOOL_DECISION) and (
            pending_call is None or e.payload.get("call_id") != pending_call
        ):
            continue
        keep = APPROVAL_VIEW_FIELDS.get(e.type)
        out.append(e if keep is None else e.model_copy(update={"payload": {k: v for k, v in e.payload.items() if k in keep}}))
    return out


async def run_reader(run_id: str, request: Request, principal: Principal = Depends(current_principal)) -> RunAccess:
    """Run 读取权限（D90）：其他租户与不存在一样 404；发起人、本 Run 已记录的审批人（批准或拒绝过）、
    平台管理员（platform-admin，开发态不检查角色）可读全部（"full"）。审批人在审批前尚未记录：Run 处于
    auth-required 时，有审批资格的同租户人员（与 approve 接口同一判定）可读审批所需部分（"approval"），
    以便查看待审批调用与参数指纹。其他情况 403。"""
    async with SessionLocal() as s:
        run = await s.get(Run, run_id)
        if run is None or run.tenant_id != principal.tenant_id:
            raise HTTPException(404, f"run '{run_id}' not found")
        request.state.audit_object = f"run:{run_id}"
        if principal.subject == run.subject or is_admin(principal):
            return "full"
        if principal.subject in await _approvers(s, run_id, (RunEventType.TOOL_APPROVED, RunEventType.TOOL_REJECTED)):
            return "full"
    if run.state == RunState.AUTH_REQUIRED and approver_denial(principal, run.subject) is None:
        return "approval"
    raise _forbid(request, "only the run initiator, its approvers or an admin can read this run")


ATTACHMENT_SCAN_RUNS = 200  # 查找附件来源时最多检查调用方最近的多少个 Run


async def _attachment_sources(refs: list[str], principal: Principal) -> list[tuple[str, str]]:
    """校验附件归属：每个 artifact:// 引用必须出自调用方本人（同租户）最近的某个 Run 的工作区，否则 403。
    返回（引用, 源文件路径）。"""
    async with SessionLocal() as s:
        q = (
            select(Run.run_id)
            .where(Run.subject == principal.subject, Run.tenant_id == principal.tenant_id)
            .order_by(Run.created_at.desc())
            .limit(ATTACHMENT_SCAN_RUNS)
        )
        own_runs = list((await s.execute(q)).scalars())
    return await asyncio.to_thread(_find_sources, refs, own_runs)


# 附件工件的文件扩展名：.txt 为外置的工具结果；其余为图片工件（S6，D102），扩展名 → 声称的类型（实际类型按文件头判定）
IMAGE_EXTS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
ATTACHMENT_EXTS = (".txt", *IMAGE_EXTS)
# 客户端消息内容块的白名单：列表形式的 content 只允许纯文本块 {"type": "text", "text": str}。图片、文件、音频、文档、
# image_ref 等其他块一律 400——非文本内容只能经 attachments 引用传入（否则绕过归属、大小、类型与 vision 校验），
# image_ref 只由服务端生成（_with_attachment_note）。
TEXT_PART_KEYS = frozenset({"type", "text"})


def _find_sources(refs: list[str], own_runs: list[str]) -> list[tuple[str, str]]:
    """在调用方自己的 Run 工作区中查找每个附件的源文件（文件系统操作，放在线程里执行）。"""
    out: list[tuple[str, str]] = []
    for ref in refs:
        m = ARTIFACT_REF.match(ref)
        candidates = (
            (os.path.join(settings.workspace_dir, r, m.group(1) + ext) for r in own_runs for ext in ATTACHMENT_EXTS) if m else iter(())
        )
        src = next((p for p in candidates if os.path.isfile(p)), None)
        if src is None:
            raise HTTPException(403, f"attachment not accessible: {ref[:80]}")
        out.append((ref, src))
    return out


DATA_CLASS_RANK = {"public": 0, "internal": 1, "confidential": 2}


def _artifact_data_class(events: list[RunEvent], artifact_id: str) -> str:
    """工件的数据分级（D88）：取源 Run 中产生该工件的 TOOL_RESULT（output_ref 指向它）的 data_class 与
    artifact_data_class（外置时工具分级与 Run 已有污点两者较高者，防止机密 Run 里经 internal 工具外置的内容被"洗白"），
    或源 Run 的 RUN_CREATED.attachments 中该工件的记录（工件本身是被附进源 Run 的）；多条记录取最高级。
    查不到、记录缺分级或分级未知时按 confidential（失败关闭）。"""
    found: list[Any] = []
    for e in events:
        if e.type == RunEventType.TOOL_RESULT:
            m = ARTIFACT_REF.match(str(e.payload.get("output_ref") or ""))
            if m and m.group(1) == artifact_id:
                found.append(e.payload.get("data_class"))
                if "artifact_data_class" in e.payload:
                    found.append(e.payload["artifact_data_class"])
        elif e.type == RunEventType.RUN_CREATED:
            for a in e.payload.get("attachments") or []:
                if isinstance(a, dict) and a.get("artifact_id") == artifact_id:
                    found.append(a.get("data_class"))
    if not found or any(c not in DATA_CLASS_RANK for c in found):
        return "confidential"
    return str(max(found, key=lambda c: DATA_CLASS_RANK[c]))


async def _attachment_meta(sources: list[tuple[str, str]]) -> list[dict[str, str]]:
    """附件元信息（随 RUN_CREATED 记入事件日志，新 Run 据此继承污点）：工件 id、源 Run、数据分级。"""
    out: list[dict[str, str]] = []
    async with SessionLocal() as s:
        log = EventLog(s)
        for _ref, src in sources:
            artifact_id = os.path.splitext(os.path.basename(src))[0]
            source_run_id = os.path.basename(os.path.dirname(src))
            data_class = _artifact_data_class(await log.list(source_run_id), artifact_id)
            out.append({"artifact_id": artifact_id, "source_run_id": source_run_id, "data_class": data_class})
    return out


def _is_image(src: str) -> bool:
    return os.path.splitext(src)[1] in IMAGE_EXTS


def _is_text_part(p: Any) -> bool:
    return isinstance(p, dict) and set(p) == TEXT_PART_KEYS and p["type"] == "text" and isinstance(p["text"], str)


def _reject_inline_content(messages: list[dict[str, Any]]) -> None:
    """客户端消息的 content 只能是字符串或纯文本块列表（白名单）；其他形态（图片 / 文件 / 音频 / 文档 / 伪造的
    image_ref / 未知类型的块，或非字符串非列表的 content）一律 400 INLINE_CONTENT_UNSUPPORTED。"""
    for m in messages:
        if "content" not in m or isinstance(m["content"], str):
            continue
        content = m["content"]
        if not isinstance(content, list) or not all(_is_text_part(p) for p in content):
            raise HTTPException(
                400,
                "INLINE_CONTENT_UNSUPPORTED: message content must be a string or a list of text parts "
                '({"type": "text", "text": ...}); images and files must be passed as attachments (artifact:// references)',
            )


def _check_vision(agent: AgentVersion) -> None:
    """带图片附件时：Agent 的 harness 须声明 supports_vision，且模型别名在 VISION_MODEL_ALIASES 内，否则 400。"""
    try:
        vision = registry.get(agent.harness).capabilities().supports_vision
    except Exception:  # harness 不可用：按不支持处理（失败关闭），驱动时还会以 harness 错误收尾
        vision = False
    if not vision or agent.model_alias not in settings.vision_model_aliases:
        raise HTTPException(
            400,
            f"ATTACHMENT_MODALITY_UNSUPPORTED: model alias '{agent.model_alias}' on harness "
            f"'{agent.harness or settings.harness}' does not accept image attachments",
        )


def _image_parts(sources: list[tuple[str, str]]) -> dict[str, dict[str, Any]]:
    """校验图片附件并生成平台历史里的引用块（文件系统操作，放在线程里执行）：源文件路径 → image_ref 块。

    张数超过 ATTACHMENT_IMAGE_MAX_COUNT → 400 ATTACHMENT_TOO_MANY；单张超过 ATTACHMENT_IMAGE_MAX_BYTES → 413
    ATTACHMENT_TOO_LARGE；文件头不是 png / jpeg / webp / gif → 400 ATTACHMENT_TYPE_UNSUPPORTED（不信任扩展名）。
    引用块只含引用与元数据（类型、字节数、sha256），图片内容由适配器发送时再从本 Run 工作区读取。"""
    images = [(ref, src) for ref, src in sources if _is_image(src)]
    if len(images) > settings.attachment_image_max_count:
        raise HTTPException(400, f"ATTACHMENT_TOO_MANY: {len(images)} image attachments (limit {settings.attachment_image_max_count})")
    out: dict[str, dict[str, Any]] = {}
    for ref, src in images:
        size = os.path.getsize(src)
        if size > settings.attachment_image_max_bytes:
            raise HTTPException(413, f"ATTACHMENT_TOO_LARGE: {ref[:80]} is {size} bytes (limit {settings.attachment_image_max_bytes})")
        with open(src, "rb") as f:
            data = f.read()
        media_type = sniff_media_type(data[:16])
        if media_type is None:
            raise HTTPException(400, f"ATTACHMENT_TYPE_UNSUPPORTED: {ref[:80]} is not a png / jpeg / webp / gif image")
        artifact_id = os.path.splitext(os.path.basename(src))[0]
        out[src] = {
            "type": IMAGE_REF_TYPE,
            "ref": f"artifact://{artifact_id}",
            "media_type": media_type,
            "size": size,
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    return out


def _with_attachment_note(
    sources: list[tuple[str, str]], messages: list[dict[str, Any]], images: dict[str, dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """首次驱动的初始消息：有附件时在最后一条用户消息后追加一条注明附件引用的用户消息（其指纹随 RUN_CREATED 记录）。

    只有文本工件时内容为字符串（与 S6 之前一致）；含图片时内容为内容块列表：一个文本块注明各附件，随后每张图片
    一个 image_ref 引用块（Contract V1.2）——历史与事件日志里只有引用，重建出的历史同样只含引用。"""
    if not sources:
        return messages
    images = images or {}
    texts = [ref for ref, src in sources if src not in images]
    if not images:
        return [*messages, {"role": "user", "content": "附件（可用 read_artifact 读取）：" + "、".join(texts)}]
    lines = ["附件（可用 read_artifact 读取）：" + "、".join(texts)] if texts else []
    lines.append("图片附件：" + "、".join(p["ref"] for p in images.values()))
    return [*messages, {"role": "user", "content": [{"type": "text", "text": "\n".join(lines)}, *images.values()]}]


def _attach(run_id: str, sources: list[tuple[str, str]], images: dict[str, dict[str, Any]] | None = None) -> None:
    """把附件复制进新 Run 的工作区（read_artifact 与适配器读图都只能读本 Run 的工作区）；
    图片按实际类型落为规范扩展名（``<id>.png`` / ``.jpg`` / ``.webp`` / ``.gif``，见 ai_contract.image_ref_path）。"""
    if not sources:
        return
    images = images or {}
    dest = os.path.join(settings.workspace_dir, run_id)
    os.makedirs(dest, exist_ok=True)
    for _ref, src in sources:
        part = images.get(src)
        name = f"{part['ref'].removeprefix('artifact://')}.{IMAGE_MEDIA_TYPES[part['media_type']]}" if part else os.path.basename(src)
        shutil.copyfile(src, os.path.join(dest, name))


def _sse(ev: Event) -> dict[str, str]:
    """将统一事件对象转换为 SSE 帧（event 字段为事件类型，data 字段为其 JSON 序列化）。"""
    return {"event": ev.type, "data": ev.model_dump_json()}


def _client_visible(ev: Event) -> bool:
    """思维链可见性策略：visibility=internal 的 thinking 事件只进事件日志，不下发给客户端；
    summary/full 原样下发。visibility 已由 runner 按 Agent 版本配置的 thinking_visibility 覆写。"""
    return not (isinstance(ev, ThinkingEvent) and ev.visibility == "internal")


# ---------------- 持租约内联执行 / 尾随事件流（S7，D108） ----------------

STREAM_POLL_S = float(os.environ.get("RUN_STREAM_POLL_S", "0.2"))  # 尾随事件日志的轮询间隔（秒）
INLINE_BUFFER = 64  # 内联执行的事件缓冲：客户端读得慢时驱动在此阻塞（SSE 背压）
SUSPENDED = (RunState.AUTH_REQUIRED, RunState.INPUT_REQUIRED)


def _frame(ev: Event | RunEvent) -> dict[str, str] | None:
    """SSE 帧：内联执行的统一事件同 /v1/messages（event=事件类型）；尾随的事件日志行 event=日志类型、id=seq
    （断线后可凭 Last-Event-ID / since_seq 续读）；不下发给客户端的思维链返回 None。"""
    if isinstance(ev, RunEvent):
        return {"event": ev.type.value, "id": str(ev.seq), "data": ev.model_dump_json()}
    return _sse(ev) if _client_visible(ev) else None


def _final_stop_reason(state: RunState, last_done: str | None) -> Literal["end_turn", "max_tokens", "cancelled", "error", "awaiting"]:
    """尾随结束时下发的 done 的停止原因：由 Run 状态（及最后一条 RUN_DONE）推出，与内联执行的 done 同口径。"""
    if state == RunState.COMPLETED:
        return "max_tokens" if last_done == "max_tokens" else "end_turn"
    if state == RunState.CANCELED:
        return "cancelled"
    if state == RunState.FAILED:
        return "error"
    return "awaiting"


async def tail_run(
    run_id: str, access: RunAccess = "full", since_seq: int = 0, *, until_suspended: bool = False, retry_after: int | None = None
) -> AsyncIterator[RunEvent | DoneEvent]:
    """尾随 Run 的事件日志（GET /v1/runs/{id}/stream 与 /resume 抢不到租约时共用）：逐条产出 seq > since_seq 的事件，
    Run 进入终态（until_suspended=True 时挂起等人也算）后再产出一条 done（停止原因由状态推出、带 trace_id）并结束。

    access="approval"（D90 审批视图）只产出审批视图内的事件、不带 trace_id，Run 离开 auth-required（审批视图失效）即结束。
    状态与事件在同一事务内读取：看到终态时，与终态同事务提交的事件一定已在本批中。Run 不存在（被清理）时直接结束。

    retry_after（/resume 尾随用）：seq 大于它的一次续跑以可重试错误 POOL_EXHAUSTED 结束（Run 保持 working、日志停在该错误）
    时也以 done(error) 结束——与内联执行同口径，不无限等待下一次重投。"""
    seq = since_seq
    while True:
        async with SessionLocal() as s:
            row = (await s.execute(select(Run.state, Run.trace_id).where(Run.run_id == run_id))).first()
            if row is None:
                return
            state = RunState(row[0])
            log = EventLog(s)
            if access == "full":
                events = await log.list(run_id, seq)
            else:
                events = [e for e in _approval_view(await log.list(run_id)) if e.seq > seq]
            ended = state.terminal or (until_suspended and state in SUSPENDED) or (access != "full" and state != RunState.AUTH_REQUIRED)
            last_done = None
            if ended and state == RunState.COMPLETED:
                q = (
                    select(RunEventRow.payload)
                    .where(RunEventRow.run_id == run_id, RunEventRow.type == RunEventType.RUN_DONE.value)
                    .order_by(RunEventRow.seq.desc())
                    .limit(1)
                )
                payload = (await s.execute(q)).scalar_one_or_none()
                last_done = payload.get("stop_reason") if isinstance(payload, dict) else None
        for e in events:
            seq = max(seq, e.seq)
            yield e
        last = events[-1] if events else None
        if (
            not ended
            and retry_after is not None
            and last is not None
            and last.seq > retry_after
            and last.type == RunEventType.RUN_ERROR
            and last.payload.get("code") == runner.POOL_EXHAUSTED
            and state == RunState.WORKING
        ):
            yield DoneEvent(run_id=run_id, stop_reason="error", trace_id=row[1] if access == "full" else None)
            return
        if ended:
            trace_id = row[1] if access == "full" else None
            yield DoneEvent(run_id=run_id, stop_reason=_final_stop_reason(state, last_done), trace_id=trace_id)
            return
        await asyncio.sleep(STREAM_POLL_S)


async def _drain(q: asyncio.Queue[Event], producer: asyncio.Task[None]) -> AsyncIterator[Event]:
    """依次取出内联执行任务产出的事件，直到任务结束且缓冲取空。"""
    while True:
        if not q.empty():
            yield q.get_nowait()
            continue
        if producer.done():
            return
        getter = asyncio.ensure_future(q.get())
        try:
            await asyncio.wait({getter, producer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not getter.done():
                getter.cancel()
        if getter.done() and not getter.cancelled():
            yield getter.result()


async def _requeue(run_id: str, reason: str) -> None:
    """把 Run 投递给 Worker 续跑（D108）；投递失败只记日志——持久化 Run 无有效租约时 reaper 会兜底重投。"""
    try:
        await enqueue(run_id, reason)
    except Exception:
        logger.warning("enqueue run %s (%s) failed; the reaper will re-enqueue it", run_id, reason, exc_info=True)


async def run_or_tail(
    run_id: str,
    make_drive: Callable[[], AsyncIterator[Event]],
    *,
    since_seq: int = 0,
    owner: str | None = None,
    epoch: int | None = None,
) -> AsyncGenerator[Event | RunEvent | DoneEvent, None]:
    """持租约内联执行，抢不到则尾随（D108）。

    - 抢到租约（lease.hold；给定 owner + epoch 时为创建 Run 时已同事务置好的租约，直接接管）：在独立任务里（fence 与心跳都属于
      该任务）驱动 make_drive()，统一事件经有界缓冲逐条产出；客户端断开（本生成器被关闭）即取消该任务——harness 被中断、
      租约释放，持久化 Run 由 reaper / 下次投递接管；
    - 抢不到（他人持有有效租约，如 Worker 已在自动续跑）：尾随事件日志，自 since_seq 起至 Run 终态或挂起；
    - 内联执行中失租（LeaseLost，已被他人接管）或写冲突（SeqConflict）：转为尾随并补投一次（Run 仍可执行而无人持有时
      由 Worker 继续；重复投递由租约去重）。尾随起点为 max(since_seq, 本次内联已提交的最大 seq)——已经以统一事件下发过的
      部分不再以日志行重复下发。其他异常照常抛出。"""
    acquired: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    buffer: asyncio.Queue[Event] = asyncio.Queue(INLINE_BUFFER)
    holder: list[lease.Lease] = []  # 抢到的租约（其 writes 记录内联已提交的进度）

    async def produce() -> None:
        async with lease.hold(run_id, owner, epoch=epoch) as held:
            acquired.set_result(held is not None)
            if held is None:
                return
            holder.append(held)
            async with aclosing(cast(AsyncGenerator[Event, None], make_drive())) as events:
                async for ev in events:
                    await buffer.put(ev)

    producer = asyncio.create_task(produce(), name=f"inline-run-{run_id}")
    try:
        first: set[asyncio.Future[Any]] = {acquired, producer}
        await asyncio.wait(first, return_when=asyncio.FIRST_COMPLETED)
        if not acquired.done():
            producer.result()  # 抢租约本身失败（如数据库不可达）：抛出原异常
            return
        if acquired.result():
            async for ev in _drain(buffer, producer):
                yield ev
            exc = producer.exception()
            if exc is None:
                return
            if not isinstance(exc, StaleWriter):
                raise exc
            logger.warning("run %s: inline execution stopped (%s); following the event log instead", run_id, type(exc).__name__)
            await _requeue(run_id, "takeover")
            if holder:
                since_seq = max(since_seq, holder[0].writes.last_seq)
        async for item in tail_run(run_id, "full", since_seq, until_suspended=True):
            yield item
    finally:
        if not producer.done():
            producer.cancel()
            await asyncio.wait({producer}, timeout=10)
        if producer.done() and not producer.cancelled():
            producer.exception()  # 已处理或已抛出：标记为已取用，避免"exception was never retrieved"


async def _prepare_input(
    agent: AgentVersion, messages: list[dict[str, Any]], refs: list[str], principal: Principal
) -> tuple[list[dict[str, Any]], list[tuple[str, str]], dict[str, dict[str, Any]], list[dict[str, str]] | None]:
    """初始消息与附件的共同校验（/v1/messages 与 POST /v1/runs）：内联内容白名单、附件归属、图片模态与限额。
    返回（追加附件说明后的初始消息，附件源文件，图片引用块，附件元信息）；初始消息中图片只是规范化引用（image_ref）。"""
    _reject_inline_content(messages)
    sources = await _attachment_sources(refs, principal) if refs else []
    images: dict[str, dict[str, Any]] = {}
    if any(_is_image(src) for _, src in sources):
        _check_vision(agent)
        images = await asyncio.to_thread(_image_parts, sources)
    attachments = await _attachment_meta(sources) if sources else None
    return _with_attachment_note(sources, messages, images), sources, images, attachments


@router.post(
    "/v1/messages",
    responses={
        400: {"model": ErrorDetail},
        403: {"model": ErrorDetail},
        404: {"model": ErrorDetail},
        413: {"model": ErrorDetail},
        503: {"model": ErrorDetail},
    },
)
async def messages(req: MessagesRequest, request: Request, principal: Principal = Depends(current_principal)) -> Any:
    """对话式入口：创建 Run 并驱动一次执行；支持流式（SSE）与非流式（聚合后一次性返回）两种模式。

    图片附件（S6，D102）：attachments 中的工件是 png / jpeg / webp / gif 图片时作为图片输入交给模型——历史里只记引用
    （image_ref），适配器发送时再从本 Run 工作区读取。归属与分级继承规则与其他附件相同（他人工件 403）。错误：
    Agent 的 harness 或模型别名不支持图片 → 400 `ATTACHMENT_MODALITY_UNSUPPORTED`；图片张数超限 → 400
    `ATTACHMENT_TOO_MANY`；文件头不是允许的类型 → 400 `ATTACHMENT_TYPE_UNSUPPORTED`；单张超限 → 413
    `ATTACHMENT_TOO_LARGE`；messages 的 content 只能是字符串或纯文本块列表，其他内容块（图片、文件、音频等）→ 400
    `INLINE_CONTENT_UNSUPPORTED`（detail 以错误码开头）。

    harness 进程池已满（goose / dsh，S6）：可重试——非流式返回 503 `POOL_EXHAUSTED`；流式（响应头已发出）下发
    error（code=POOL_EXHAUSTED，retryable=true）后 done(error)。两种情况本 Run 均为 failed，重发请求即可。

    持久化（S7，D108 / D109）：Run 以 durable=1 创建，初始消息（图片只存规范化引用）与发起人令牌（加密）同事务入库，
    本节点持租约内联执行（不进队列）。节点死亡后租约过期，由 Worker 接管异步续跑，客户端可用
    `GET /v1/runs/{run_id}/stream` 续读；挂起审批通过后由系统自动续跑。"""
    agent, reason = await _load_agent(req.agent_id, req.agent_version, principal.tenant_id, principal.subject)
    messages, sources, images, attachments = await _prepare_input(agent, req.messages, req.attachments, principal)
    owner = lease.new_owner()
    run_id = await runner.create_run(
        agent,
        principal.subject,
        req.session_id,
        actor=principal.actor,
        rollout=reason,
        attachments=attachments,
        initial_messages=messages,
        durable=True,
        credential=principal.token,
        lease_owner=owner,  # 创建即持租（同一事务）：reaper / Worker 不会抢在内联执行之前接走，客户端总是拿到统一事件
    )
    request.state.audit_object = f"run:{run_id}"
    _attach(run_id, sources, images)
    expires_at = credentials.token_expiry(principal.token) if principal.token else None

    def drive() -> AsyncIterator[Event]:
        return runner.drive(run_id, agent, principal.subject, messages, credential=principal.token, credential_expires_at=expires_at)

    gen = run_or_tail(run_id, drive, owner=owner, epoch=runner.FIRST_LEASE_EPOCH)
    if req.stream:

        async def stream() -> AsyncIterator[dict[str, str]]:
            async with aclosing(gen) as items:
                async for ev in items:
                    frame = _frame(ev)
                    if frame is not None:
                        yield frame

        return EventSourceResponse(stream(), headers={"X-Run-Id": run_id})
    async with aclosing(gen) as items:  # 非流式：先消费完整个生成器，再聚合返回
        collected = [ev async for ev in items if isinstance(ev, RunEvent) or _client_visible(ev)]
    events = [e for e in collected if not isinstance(e, RunEvent)]
    exhausted = next((e for e in events if isinstance(e, ErrorEvent) and e.code == runner.POOL_EXHAUSTED), None)
    if exhausted is not None:
        raise HTTPException(503, f"{runner.POOL_EXHAUSTED}: {exhausted.message}", headers={"Retry-After": "1"})
    text = "".join(e.delta for e in events if isinstance(e, TextEvent))
    # 罕见：内联执行中途被其他节点接管，之后只尾随了事件日志——回答续上日志里的模型响应。尾随自内联已提交的最大 seq 之后起，
    # 这里的模型响应都在其后，不会与已收集的文本重复
    replies = [e for e in collected if isinstance(e, RunEvent) and e.type == RunEventType.MODEL_RESPONSE]
    text += "".join(str(e.payload.get("text") or "") for e in replies)
    return {"run_id": run_id, "content": text, "events": [e.model_dump() for e in collected]}


@router.get("/v1/agents")
async def list_my_agents(principal: Principal = Depends(current_principal)) -> list[dict[str, Any]]:
    """门户用：调用方租户内有已发布版本的 Agent，以及按灰度规则该调用方会用到的版本。
    （门户可见范围按 Console 的 visibility 过滤在 S4 接入。）"""
    async with SessionLocal() as s:
        q = select(AgentVersion).where(AgentVersion.tenant_id == principal.tenant_id, AgentVersion.status == "published")
        published = (await s.execute(q)).scalars().all()
    by_agent: dict[str, list[AgentVersion]] = {}
    for v in published:
        by_agent.setdefault(v.agent_id, []).append(v)
    out = []
    for agent_id in sorted(by_agent):
        chosen, reason = rollout.choose(by_agent[agent_id], principal.subject)
        if chosen is not None:
            out.append({"agent_id": agent_id, "version": chosen.version, "rollout": reason})
    return out


# ---------------- Run API ----------------


class RunCreate(BaseModel):
    """/v1/runs 的创建请求体：面向 A2A 风格的任务提交场景。"""

    agent_id: str  # 目标 Agent 标识
    agent_version: str | None = None  # 指定版本
    input: list[dict[str, Any]]  # 初始输入消息
    session_id: str | None = Field(default=None, max_length=40)  # 会话标识（S7，Run 列表可按它过滤）
    attachments: list[str] = Field(default_factory=list, max_length=10)  # 附件（artifact:// 引用），规则同 /v1/messages
    mode: str = "async"  # 执行模式（固定为经队列由 Worker 异步执行）


@router.post(
    "/v1/runs",
    status_code=202,
    response_model=RunStateView,
    responses={400: {"model": ErrorDetail}, 403: {"model": ErrorDetail}, 404: {"model": ErrorDetail}, 413: {"model": ErrorDetail}},
)
async def create_run(req: RunCreate, request: Request, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """创建一个持久化 Run 并入队，立即返回 202（已接受），由 Worker 抢租约后执行（S7，D105）。
    t_run（durable=1、session_id）、t_run_input（初始消息，附件只存规范化引用）与发起人令牌（加密）同一事务写入后才入队；
    入队失败（如 Redis 暂不可用）仍返回 202，由 reaper 兜底重投。
    input 的 content 与 attachments 规则同 /v1/messages：content 只能是字符串或纯文本块列表，否则 400
    `INLINE_CONTENT_UNSUPPORTED`；他人的工件 403。"""
    agent, reason = await _load_agent(req.agent_id, req.agent_version, principal.tenant_id, principal.subject)
    messages, sources, images, attachments = await _prepare_input(agent, req.input, req.attachments, principal)
    run_id = await runner.create_run(
        agent,
        principal.subject,
        req.session_id,
        actor=principal.actor,
        rollout=reason,
        attachments=attachments,
        initial_messages=messages,
        durable=True,
        credential=principal.token,
    )
    request.state.audit_object = f"run:{run_id}"
    _attach(run_id, sources, images)
    await _requeue(run_id, "created")
    return {"run_id": run_id, "state": RunState.SUBMITTED}


@router.get(
    "/v1/runs/{run_id}",
    response_model=RunView,
    responses={403: {"model": ErrorDetail}, 404: {"model": ErrorDetail}},
    dependencies=[Depends(run_reader)],
)
async def get_run(run_id: str) -> dict[str, Any]:
    """查询单个 Run 的当前状态与元信息（读取权限见 run_reader：发起人 / 本 Run 审批人 / 管理员，待审批时含有审批资格者）。"""
    async with SessionLocal() as s:
        run = await s.get(Run, run_id)
        if run is None:
            raise HTTPException(404)
        return run_view(run)


def run_view(run: Run) -> dict[str, Any]:
    """t_run 行 → RunView 字段（单个查询与列表共用；session_id 只在列表项中输出）。"""
    return {
        "run_id": run.run_id,
        "state": run.state,
        "agent_id": run.agent_id,
        "agent_version": run.agent_version,
        "model_alias": run.model_alias,
        "subject": run.subject,
        "last_seq": run.last_seq,
        "created_at": run.created_at.isoformat(),
    }


@router.get(
    "/v1/runs/{run_id}/events",
    response_model=RunEventsView,
    responses={403: {"model": ErrorDetail}, 404: {"model": ErrorDetail}},
)
async def run_events(
    run_id: str, access: Annotated[RunAccess, Depends(run_reader)], since_seq: Annotated[int, Query(ge=0, le=2**53)] = 0
) -> list[dict[str, Any]]:
    """按 seq 增量查询 Run 的事件日志（自 since_seq 之后的所有事件）。读取权限见 run_reader；
    待审批期间尚未成为审批人的有审批资格者只返回审批视图：待批调用的 tool.requested / tool.decision、
    run.created（只留元数据）与 run.state_changed，字段收窄。

    since_seq 限定在 [0, 2^53]：既覆盖任何现实的 seq，又保证 JS 客户端可精确表示，超界由 FastAPI 返回 422。"""
    async with SessionLocal() as s:
        if access == "full":
            events = await EventLog(s).list(run_id, since_seq)
        else:
            # 待批调用要从完整日志判定（其 tool.requested 可能早于 since_seq），再按 since_seq 截取
            events = [e for e in _approval_view(await EventLog(s).list(run_id)) if e.seq > since_seq]
    return [e.model_dump() for e in events]


class ApprovalBody(BaseModel):
    """审批/拒绝工具调用请求体。

    params_hash 是必填项：审批附着于具体参数与资源，审批人必须回传卡片上展示的那一份参数指纹，
    否则无从判断"批的到底是哪一组参数"。
    """

    call_id: str  # 待审批的工具调用标识
    params_hash: str  # 审批时卡片上展示的参数指纹，必须与闸口决策事件中记录的一致
    ttl_seconds: int = 900  # 审批有效期（秒），超时后该批准失效，必须重新审批
    comment: str = ""  # 审批意见


async def _check_pending_approval(s: AsyncSession, run_id: str, body: ApprovalBody, request: Request, principal: Principal) -> EventLog:
    """审批 / 拒绝的共同前置校验：Run 存在（否则 404）、处于 auth-required，且回传的参数指纹
    与该调用最近一次闸口决策记录的一致（否则 409）。返回绑定到同一会话的 EventLog。"""
    run = await s.get(Run, run_id)
    if run is None:
        raise HTTPException(404, f"run '{run_id}' not found")
    if run.state != RunState.AUTH_REQUIRED:
        raise HTTPException(409, "run is not awaiting approval")
    check_approver(request, principal, run.subject)  # 强制鉴权时：只有人员能审批，且不能审批自己发起的 Run
    log = EventLog(s)
    decided = [
        e.payload.get("params_hash")
        for e in await log.list(run_id)
        if e.type == RunEventType.TOOL_DECISION and e.payload.get("call_id") == body.call_id
    ]
    if not decided or decided[-1] != body.params_hash:
        # 参数指纹对不上：说明待审批的参数已经变化（或审批人拿的是过期卡片），必须重新走审批。
        raise HTTPException(409, "params_hash does not match the pending call; re-approval required（参数已变化，需重新审批）")
    return log


@router.post(
    "/v1/runs/{run_id}/approve",
    response_model=ApprovalView,
    responses={404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}},
    dependencies=[Depends(run_in_tenant)],
)
async def approve(run_id: str, body: ApprovalBody, request: Request, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """批准一次挂起中的高风险工具调用：仅在 Run 处于 auth-required 状态时允许，
    记录 TOOL_APPROVED 事件并把状态迁回 working。持久化执行的 Run（S7，D108）随即入队，由 Worker 以发起人存储的
    凭据自动续跑；S7 之前创建的 Run（非持久化）仍需发起人调用 /resume 才继续执行。

    批准会绑定"参数指纹 + 有效期"：参数一旦变化或审批超时，闸口不会放行，必须重新审批。"""
    async with SessionLocal() as s:
        log = await _check_pending_approval(s, run_id, body, request, principal)
        run = await s.get(Run, run_id)
        durable = run is not None and bool(run.durable)
        expires_at = (datetime.now(UTC) + timedelta(seconds=body.ttl_seconds)).isoformat()
        await log.append(
            run_id,
            RunEventType.TOOL_APPROVED,
            {
                "call_id": body.call_id,
                "approver": principal.subject,
                "comment": body.comment,
                "params_hash": body.params_hash,
                "expires_at": expires_at,
            },
        )
        await log.transition(run_id, RunState.WORKING, "approved")
        await s.commit()
    if durable:
        await continue_after_approval(run_id)
    return {"run_id": run_id, "state": RunState.WORKING, "resume": f"/v1/runs/{run_id}/resume"}


async def continue_after_approval(run_id: str) -> None:
    """审批通过后统一入队（D108），由 Worker 抢租约、以发起人存储的凭据续跑。"""
    await _requeue(run_id, "approved")


@router.post(
    "/v1/runs/{run_id}/reject",
    response_model=RunStateView,
    responses={404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}},
    dependencies=[Depends(run_in_tenant)],
)
async def reject(run_id: str, body: ApprovalBody, request: Request, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """拒绝一次挂起中的高风险工具调用：记录 TOOL_REJECTED 事件并将 Run 直接置为 failed。
    与 approve 同样要求 Run 处于 auth-required 且参数指纹匹配，否则 409；Run 不存在 404。"""
    async with SessionLocal() as s:
        log = await _check_pending_approval(s, run_id, body, request, principal)
        await log.append(
            run_id,
            RunEventType.TOOL_REJECTED,
            {"call_id": body.call_id, "approver": principal.subject, "comment": body.comment, "params_hash": body.params_hash},
        )
        await log.transition(run_id, RunState.FAILED, "rejected")
        await credentials.delete_credential(run_id, s)  # 终态即删存储的发起人凭据（D113），与迁移同事务
        await s.commit()
    return {"run_id": run_id, "state": RunState.FAILED}


class ResumeBody(BaseModel):
    """续跑请求体（S7 起 messages 可选，D109）：缺省时用服务端存储的初始消息（t_run_input）；传了仍须与 RUN_CREATED
    记录的指纹一致（带附件的 Run 须连同首次驱动时追加的附件说明消息），否则 409。S7 之前创建的 Run 服务端没有存储，
    须回传初始消息。"""

    messages: list[dict[str, Any]] | None = None


def _resume_since(log: list[RunEvent]) -> int:
    """尾随续跑的起点：最近一次迁回 working（批准 / 补充输入）之前的 seq，使尾随者看到整段续跑。"""
    back = [e.seq for e in log if e.type == RunEventType.RUN_STATE_CHANGED and e.payload.get("to") == RunState.WORKING.value]
    return back[-1] - 1 if back else 0


def _woken_from_suspension(log: list[RunEvent]) -> bool:
    """事件日志中是否有"从挂起（auth-required / input-required）迁回 working"的记录（审批通过 / 补充输入）。"""
    suspended = {s.value for s in SUSPENDED}
    return any(
        e.type == RunEventType.RUN_STATE_CHANGED and e.payload.get("to") == RunState.WORKING.value and e.payload.get("from") in suspended
        for e in log
    )


@router.post(
    "/v1/runs/{run_id}/resume",
    response_model=None,  # 200 为 SSE 事件流（统一事件见 x-sse-event-schema；尾随时为事件日志行 RunEvent + 末尾 done）
    responses={403: {"model": ErrorDetail}, 404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}},
    dependencies=[Depends(run_in_tenant)],
)
async def resume(run_id: str, request: Request, body: ResumeBody | None = None, principal: Principal = Depends(current_principal)) -> Any:
    """Continue a run after approval by replaying its event log (checkpoint via replay).
    （在审批通过后续跑一个 Run：通过重放该 Run 的事件日志实现 checkpoint 续跑，
    只允许对处于 working 状态的 Run 发起。）

    续跑者（D87 修订）只能是 Run 发起人：审批人只能 approve / reject，审批人、管理员或其他同租户人员续跑一律 403
    （其他租户 404，由 run_in_tenant 判定）；先判身份再判状态，不向无权者透露 Run 状态。
    回传的初始消息须与 RUN_CREATED 记录的指纹（initial_messages_hash）一致，否则 409；S5 之前创建的 Run 没有
    该字段，无从比对，兼容放行（与此前行为一致）。

    S7（D108）：持久化 Run 审批通过 / 补充输入后已自动入队、由 Worker 续跑。对这类 Run（事件日志中有从挂起迁回 working 的
    记录）本接口结果是确定的：补投一次（租约去重），然后自最近一次迁回 working 之前起尾随事件日志到当前末尾，以 done 收尾
    （与 `GET /v1/runs/{run_id}/stream` 同一实现：事件日志行，SSE id 为 seq）——Run 已是终态或再次挂起时同样返回这段日志
    与 done（停止原因由状态推出），不返回 409，也不在本节点内联执行。
    其他 Run（S7 之前创建的非持久化 Run、尚未挂起过的持久化 Run）行为不变：须处于 working（否则 409）；抢到租约则本节点
    内联流式执行（统一事件），抢不到则尾随事件流。发起人令牌随续跑刷新存储的凭据（Run 未终态时）。
    """
    given = body.messages if body is not None else None
    async with SessionLocal() as s:
        run = await s.get(Run, run_id)
        if run is None:
            raise HTTPException(404)
        if principal.subject != run.subject:
            raise _forbid(request, "only the run initiator can resume it")
        log = await EventLog(s).list(run_id)
        follow = bool(run.durable) and _woken_from_suspension(log)  # 持久化 Run 已被唤醒过：一律尾随（结果确定）
        if not follow and run.state != RunState.WORKING:
            raise HTTPException(409, f"run state is {run.state}")
        stored = await s.get(RunInput, run_id)
    if given is not None:
        try:
            invariant.check_initial(log, given)
        except invariant.InvariantViolation as e:
            raise HTTPException(409, f"messages do not match the run's initial messages: {e}") from e
        messages = given
    elif stored is not None:
        messages = list(stored.messages)
    else:
        raise HTTPException(409, "this run has no stored initial messages; pass the original messages to resume it")
    if run.durable and principal.token and not RunState(run.state).terminal:
        async with SessionLocal() as s:  # 只接受发起人令牌：续跑时覆盖存储的凭据，供之后的自动续跑 / 接管使用
            await credentials.store_credential(s, run_id, principal.subject, principal.token)
            await s.commit()
    if follow:
        await _requeue(run_id, "resume")  # 审批 / 输入后的投递万一丢失也能续上；重复投递由租约去重

        async def follow_stream() -> AsyncIterator[dict[str, str]]:
            tail = cast(
                AsyncGenerator[RunEvent | DoneEvent, None],
                tail_run(run_id, "full", _resume_since(log), until_suspended=True, retry_after=log[-1].seq if log else 0),
            )
            async with aclosing(tail) as items:
                async for ev in items:
                    frame = _frame(ev)
                    if frame is not None:
                        yield frame

        return EventSourceResponse(follow_stream())
    agent, _ = await _load_agent(run.agent_id, run.agent_version, principal.tenant_id, allow_unpublished=True)  # 续跑固定原版本
    expires_at = credentials.token_expiry(principal.token) if principal.token else None

    def drive() -> AsyncIterator[Event]:
        # 续跑者即发起人：credential_subject 与发起主体相同，不写入事件（机制保留，见 RunCtx.foreign_credential_subject）
        return runner.drive(
            run_id,
            agent,
            run.subject,
            messages,
            credential=principal.token,
            credential_subject=principal.subject,
            credential_expires_at=expires_at,
        )

    async def stream() -> AsyncIterator[dict[str, str]]:
        async with aclosing(run_or_tail(run_id, drive, since_seq=_resume_since(log))) as items:
            async for ev in items:
                frame = _frame(ev)
                if frame is not None:
                    yield frame

    return EventSourceResponse(stream())


@router.post(
    "/v1/runs/{run_id}/cancel",
    response_model=RunStateView,
    responses={403: {"model": ErrorDetail}, 404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}},
)
async def cancel(run_id: str, request: Request, access: Annotated[RunAccess, Depends(run_reader)]) -> dict[str, Any]:
    """取消一个 Run；Run 不存在返回 404（RunNotFound 由应用级异常处理器映射），
    当前状态不允许迁移到 canceled（如已是终态）返回 409。

    取消权限（D90）与完整读取权限相同：发起人、本 Run 已记录的审批人、平台管理员（开发态不检查角色）；
    待审批期间仅有审批资格者（只读审批所需部分）不能取消，其他人 403，其他租户 404。

    跨节点（S7，D110）：CAS 迁到 canceled（并发写者抢先时重读重试一次）并删除存储的凭据，提交后经
    `runtime:cancel` 广播，持有该 Run 的节点中断其 harness；广播丢失时持有者心跳读到 canceled 兜底。"""
    if access != "full":
        raise _forbid(request, "only the run initiator, its approvers or an admin can cancel this run")
    for attempt in (1, 2):
        async with SessionLocal() as s:
            log = EventLog(s)
            try:
                await log.transition(run_id, RunState.CANCELED, "user")
                await credentials.delete_credential(run_id, s)
                await s.commit()
                break
            except ValueError as e:  # 已是其他终态：非法迁移
                raise HTTPException(409, str(e)) from e
            except SeqConflict as e:
                await s.rollback()
                if attempt == 2:
                    raise HTTPException(409, "run is being updated concurrently; retry the cancel") from e
    await publish_cancel(run_id)
    return {"run_id": run_id, "state": RunState.CANCELED}


class InputBody(BaseModel):
    """补充输入（S7 §3，D112）。

    - answer：回答 request_user_input，call_id 为该调用，value 为回答（按其 schema 校验，不符 400）；
    - reconcile：结果未知的写工具对账，call_id 为该调用，action ∈ done / skip / retry（retry 只允许下游声明幂等的工具），
      done 时 value 可选，作为补记的结果；
    - credential：用本次请求的令牌（须为发起人）刷新存储的凭据，不从请求体取令牌。"""

    kind: Literal["answer", "reconcile", "credential"]
    call_id: str | None = Field(default=None, max_length=128)
    value: Any = None
    action: Literal["done", "skip", "retry"] | None = None


@router.post(
    "/v1/runs/{run_id}/input",
    response_model=InputView,
    responses={400: {"model": ErrorDetail}, 403: {"model": ErrorDetail}, 404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}},
    dependencies=[Depends(run_in_tenant)],
)
async def provide_input(
    run_id: str, body: InputBody, request: Request, principal: Principal = Depends(current_principal)
) -> dict[str, Any]:
    """为 input-required 的 Run 补充输入（只允许发起人，D112）：同一事务写 INPUT_PROVIDED（及对应的工具结果）并 CAS 回
    working，随后入队由 Worker 续跑（S7 之前创建的非持久化 Run 不入队，由发起人 /resume）。

    错误：其他租户 / 不存在 404；非发起人或令牌主体不是发起人 403；Run 不在 input-required 或没有匹配的待补充输入 409；
    回答不符 schema、action 不合规、令牌剩余有效期不足 400（INPUT_INVALID）；并发写冲突 409（可重试）。"""
    value = principal.token if body.kind == user_input.KIND_CREDENTIAL else body.value
    try:
        out = await user_input.provide_input(
            run_id, principal.subject, body.kind, call_id=body.call_id, value=value, action=body.action, user_token=principal.token
        )
    except RunNotFound as e:
        raise HTTPException(404, f"run '{run_id}' not found") from e
    except (user_input.InputForbidden, credentials.CredentialSubjectMismatch) as e:
        raise _forbid(request, str(e)) from e
    except user_input.InputNotPending as e:
        raise HTTPException(409, str(e)) from e
    except StaleWriter as e:
        raise HTTPException(409, f"run was updated concurrently; retry: {e}") from e
    except RecursionError as e:  # 回答嵌套过深
        raise ApiError(400, "INPUT_INVALID", "input value is nested too deeply") from e
    except ValueError as e:
        raise ApiError(400, "INPUT_INVALID", str(e)) from e
    async with SessionLocal() as s:
        run = await s.get(Run, run_id)
        durable = run is not None and bool(run.durable)
    if durable:
        await _requeue(run_id, "input")
    return out


_observe_transport: httpx.AsyncBaseTransport | None = None  # 测试注入：观测适配器请求经该 transport 处理


def set_observe_transport_for_tests(transport: httpx.AsyncBaseTransport | None) -> None:
    global _observe_transport
    _observe_transport = transport


class FeedbackBody(BaseModel):
    """对一次 Run 的用户反馈：点赞 +1 / 点踩 -1，可附说明或修正后的正确答案（可沉淀为评测样本）。"""

    score: float = Field(ge=-1, le=1)
    comment: str = Field(default="", max_length=2000)
    correction: str | None = Field(default=None, max_length=8000)


@router.post(
    "/v1/runs/{run_id}/feedback",
    responses={403: {"model": ErrorDetail}, 404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}, 502: {"model": ErrorDetail}},
    dependencies=[Depends(run_in_tenant)],
)
async def feedback(run_id: str, body: FeedbackBody, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """反馈（S3.5）：只有 Run 发起人能反馈；trace_id 由 Runtime 从 t_run 取，调用方无法把评分挂到任意 Trace 上。
    经观测适配器写成 Langfuse score（user_feedback），挂在该 Run 最近一次执行的 Trace 上。"""
    async with SessionLocal() as s:
        run = await s.get(Run, run_id)
    assert run is not None  # run_in_tenant 已保证存在
    if run.subject != principal.subject:
        raise HTTPException(403, "only the run initiator can give feedback")
    if not run.trace_id:
        raise HTTPException(409, "run has no trace yet")
    payload = {"trace_id": run.trace_id, "run_id": run_id, "subject": principal.subject} | body.model_dump()
    try:
        async with httpx.AsyncClient(timeout=10.0, transport=_observe_transport) as c:
            r = await c.post(f"{settings.observe_adapter_url.rstrip('/')}/feedback", json=payload)
        forwarded = r.status_code < 300 and bool(r.json().get("forwarded"))
    except (httpx.HTTPError, ValueError) as e:
        raise HTTPException(502, f"observe adapter unavailable: {type(e).__name__}") from e
    if not forwarded:
        raise HTTPException(502, "feedback was not forwarded to Langfuse")
    return {"run_id": run_id, "trace_id": run.trace_id, "forwarded": True}


def _dumps(o: Any) -> str:
    """带非 ASCII 直出（ensure_ascii=False）的 JSON 序列化辅助函数，避免中文被转义。"""
    return json.dumps(o, ensure_ascii=False)

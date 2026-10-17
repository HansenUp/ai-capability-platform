"""Messages API (HTTP + SSE) and Run API (A2A-named states, approval/input/cancel, event stream).
（Messages API：兼容通用对话形态的 HTTP/SSE 入口；Run API：以 A2A 任务状态命名，提供
审批/续跑/取消/事件流查询等围绕 Run 生命周期的操作。两组 API 共享同一条底层驱动
（runtime.loop.runner.drive），只是入口形态不同。）
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from ai_contract import (
    IMAGE_MEDIA_TYPES,
    IMAGE_REF_TYPE,
    ErrorEvent,
    Event,
    RunEvent,
    RunEventType,
    RunState,
    TextEvent,
    ThinkingEvent,
    sniff_media_type,
)
from runtime.api import rollout
from runtime.api.auth import Principal, approver_denial, check_approver, current_principal, is_admin
from runtime.api.views import ApprovalView, ErrorDetail, RunEventsView, RunStateView, RunView
from runtime.config import settings
from runtime.harness import registry
from runtime.loop import active, runner
from runtime.loop.toolbroker import ARTIFACT_REF
from runtime.run import invariant
from runtime.state.db import AgentVersion, Run, SessionLocal
from runtime.state.eventlog import EventLog

router = APIRouter(dependencies=[Depends(current_principal)])  # 全部 /v1 接口要求调用方身份（AUTH_REQUIRED=true 时即 JWT）


class MessagesRequest(BaseModel):
    """/v1/messages 的请求体：类似通用对话补全接口，但绑定到具体 agent_id/agent_version。"""

    agent_id: str  # 目标 Agent 标识
    agent_version: str | None = None  # 指定版本，缺省取该 agent 已发布的最新版本
    session_id: str | None = None  # 会话标识（用于多轮关联，当前实现未强制使用）
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
# image_ref 等其他块一律 422——非文本内容只能经 attachments 引用传入（否则绕过归属、大小、类型与 vision 校验），
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
    image_ref / 未知类型的块，或非字符串非列表的 content）一律 422 INLINE_CONTENT_UNSUPPORTED。"""
    for m in messages:
        if "content" not in m or isinstance(m["content"], str):
            continue
        content = m["content"]
        if not isinstance(content, list) or not all(_is_text_part(p) for p in content):
            raise HTTPException(
                422,
                "INLINE_CONTENT_UNSUPPORTED: message content must be a string or a list of text parts "
                '({"type": "text", "text": ...}); images and files must be passed as attachments (artifact:// references)',
            )


def _check_vision(agent: AgentVersion) -> None:
    """带图片附件时：Agent 的 harness 须声明 supports_vision，且模型别名在 VISION_MODEL_ALIASES 内，否则 422。"""
    try:
        vision = registry.get(agent.harness).capabilities().supports_vision
    except Exception:  # harness 不可用：按不支持处理（失败关闭），驱动时还会以 harness 错误收尾
        vision = False
    if not vision or agent.model_alias not in settings.vision_model_aliases:
        raise HTTPException(
            422,
            f"ATTACHMENT_MODALITY_UNSUPPORTED: model alias '{agent.model_alias}' on harness "
            f"'{agent.harness or settings.harness}' does not accept image attachments",
        )


def _image_parts(sources: list[tuple[str, str]]) -> dict[str, dict[str, Any]]:
    """校验图片附件并生成平台历史里的引用块（文件系统操作，放在线程里执行）：源文件路径 → image_ref 块。

    张数超过 ATTACHMENT_IMAGE_MAX_COUNT → 422 ATTACHMENT_TOO_MANY；单张超过 ATTACHMENT_IMAGE_MAX_BYTES → 413
    ATTACHMENT_TOO_LARGE；文件头不是 png / jpeg / webp / gif → 422 ATTACHMENT_TYPE_UNSUPPORTED（不信任扩展名）。
    引用块只含引用与元数据（类型、字节数、sha256），图片内容由适配器发送时再从本 Run 工作区读取。"""
    images = [(ref, src) for ref, src in sources if _is_image(src)]
    if len(images) > settings.attachment_image_max_count:
        raise HTTPException(422, f"ATTACHMENT_TOO_MANY: {len(images)} image attachments (limit {settings.attachment_image_max_count})")
    out: dict[str, dict[str, Any]] = {}
    for ref, src in images:
        size = os.path.getsize(src)
        if size > settings.attachment_image_max_bytes:
            raise HTTPException(413, f"ATTACHMENT_TOO_LARGE: {ref[:80]} is {size} bytes (limit {settings.attachment_image_max_bytes})")
        with open(src, "rb") as f:
            data = f.read()
        media_type = sniff_media_type(data[:16])
        if media_type is None:
            raise HTTPException(422, f"ATTACHMENT_TYPE_UNSUPPORTED: {ref[:80]} is not a png / jpeg / webp / gif image")
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


@router.post(
    "/v1/messages",
    responses={403: {"model": ErrorDetail}, 404: {"model": ErrorDetail}, 413: {"model": ErrorDetail}, 503: {"model": ErrorDetail}},
)
async def messages(req: MessagesRequest, request: Request, principal: Principal = Depends(current_principal)) -> Any:
    """对话式入口：创建 Run 并驱动一次执行；支持流式（SSE）与非流式（聚合后一次性返回）两种模式。

    图片附件（S6，D102）：attachments 中的工件是 png / jpeg / webp / gif 图片时作为图片输入交给模型——历史里只记引用
    （image_ref），适配器发送时再从本 Run 工作区读取。归属与分级继承规则与其他附件相同（他人工件 403）。错误：
    Agent 的 harness 或模型别名不支持图片 → 422 `ATTACHMENT_MODALITY_UNSUPPORTED`；图片张数超限 → 422
    `ATTACHMENT_TOO_MANY`；文件头不是允许的类型 → 422 `ATTACHMENT_TYPE_UNSUPPORTED`；单张超限 → 413
    `ATTACHMENT_TOO_LARGE`；messages 的 content 只能是字符串或纯文本块列表，其他内容块（图片、文件、音频等）→ 422
    `INLINE_CONTENT_UNSUPPORTED`（detail 以错误码开头）。

    harness 进程池已满（goose / dsh，S6）：可重试——非流式返回 503 `POOL_EXHAUSTED`；流式（响应头已发出）下发
    error（code=POOL_EXHAUSTED，retryable=true）后 done(error)。两种情况本 Run 均为 failed，重发请求即可。"""
    agent, reason = await _load_agent(req.agent_id, req.agent_version, principal.tenant_id, principal.subject)
    _reject_inline_content(req.messages)
    sources = await _attachment_sources(req.attachments, principal) if req.attachments else []
    images: dict[str, dict[str, Any]] = {}
    if any(_is_image(src) for _, src in sources):
        _check_vision(agent)
        images = await asyncio.to_thread(_image_parts, sources)
    attachments = await _attachment_meta(sources) if sources else None
    messages = _with_attachment_note(sources, req.messages, images)
    run_id = await runner.create_run(
        agent,
        principal.subject,
        req.session_id,
        actor=principal.actor,
        rollout=reason,
        attachments=attachments,
        initial_messages=messages,
    )
    request.state.audit_object = f"run:{run_id}"
    _attach(run_id, sources, images)
    gen = runner.drive(run_id, agent, principal.subject, messages, credential=principal.token)
    if req.stream:

        async def stream() -> AsyncIterator[dict[str, str]]:
            async for ev in gen:
                if _client_visible(ev):
                    yield _sse(ev)

        return EventSourceResponse(stream(), headers={"X-Run-Id": run_id})
    events = [ev async for ev in gen if _client_visible(ev)]  # 非流式：先消费完整个生成器，再聚合返回
    exhausted = next((e for e in events if isinstance(e, ErrorEvent) and e.code == runner.POOL_EXHAUSTED), None)
    if exhausted is not None:
        raise HTTPException(503, f"{runner.POOL_EXHAUSTED}: {exhausted.message}", headers={"Retry-After": "1"})
    text = "".join(e.delta for e in events if isinstance(e, TextEvent))
    return {"run_id": run_id, "content": text, "events": [e.model_dump() for e in events]}


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
    mode: str = "async"  # 执行模式（当前实现固定为后台异步执行）


@router.post("/v1/runs", status_code=202, response_model=RunStateView, responses={404: {"model": ErrorDetail}})
async def create_run(req: RunCreate, request: Request, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """创建一个 Run 并立即返回 202（已接受）；实际驱动在后台任务中异步执行。
    input 的 content 规则同 /v1/messages：只能是字符串或纯文本块列表，否则 422 `INLINE_CONTENT_UNSUPPORTED`。"""
    agent, reason = await _load_agent(req.agent_id, req.agent_version, principal.tenant_id, principal.subject)
    _reject_inline_content(req.input)
    run_id = await runner.create_run(agent, principal.subject, None, actor=principal.actor, rollout=reason, initial_messages=req.input)
    request.state.audit_object = f"run:{run_id}"
    # In production a worker picks this up from Redis Streams; here we execute eagerly in background.
    # （生产环境应由 worker 从 Redis Streams 消费任务；此处为简化实现，直接在后台任务中即时执行。
    # Redis Streams 接入属于 S7 工作包。）
    import asyncio

    async def _bg() -> None:
        async for _ in runner.drive(run_id, agent, principal.subject, req.input, credential=principal.token):
            pass  # 后台驱动只需消费生成器使其执行完成，事件已在 drive 内部写入事件日志

    asyncio.create_task(_bg())
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
    记录 TOOL_APPROVED 事件并把状态迁回 working，随后需调用 /resume 才能真正继续执行。

    批准会绑定"参数指纹 + 有效期"：参数一旦变化或审批超时，闸口不会放行，必须重新审批。"""
    async with SessionLocal() as s:
        log = await _check_pending_approval(s, run_id, body, request, principal)
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
    return {"run_id": run_id, "state": RunState.WORKING, "resume": f"/v1/runs/{run_id}/resume"}


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
        await s.commit()
    return {"run_id": run_id, "state": RunState.FAILED}


class ResumeBody(BaseModel):
    """续跑请求体：messages: the original initial messages (clients keep them; workers read from session store)
    （客户端需要回传最初的输入消息用于重建历史；生产环境中 worker 也可以改为从会话存储读取。
    带附件的 Run 须连同首次驱动时追加的附件说明消息一并回传；指纹须与 RUN_CREATED 记录的一致，否则 409。）"""

    messages: list[dict[str, Any]]


@router.post(
    "/v1/runs/{run_id}/resume",
    response_model=None,  # 200 为 SSE 统一事件流（data 帧 schema 见 x-sse-event-schema），不做 JSON 响应建模
    responses={403: {"model": ErrorDetail}, 404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}},
    dependencies=[Depends(run_in_tenant)],
)
async def resume(run_id: str, body: ResumeBody, request: Request, principal: Principal = Depends(current_principal)) -> Any:
    """Continue a run after approval by replaying its event log (checkpoint via replay).
    （在审批通过后续跑一个 Run：通过重放该 Run 的事件日志实现 checkpoint 续跑，
    只允许对处于 working 状态的 Run 发起。）

    续跑者（D87 修订）只能是 Run 发起人：审批人只能 approve / reject，审批人、管理员或其他同租户人员续跑一律 403
    （其他租户 404，由 run_in_tenant 判定）；先判身份再判状态，不向无权者透露 Run 状态。
    回传的初始消息须与 RUN_CREATED 记录的指纹（initial_messages_hash）一致，否则 409；S5 之前创建的 Run 没有
    该字段，无从比对，兼容放行（与此前行为一致）。
    """
    async with SessionLocal() as s:
        run = await s.get(Run, run_id)
        if run is None:
            raise HTTPException(404)
        if principal.subject != run.subject:
            raise _forbid(request, "only the run initiator can resume it")
        if run.state != RunState.WORKING:
            raise HTTPException(409, f"run state is {run.state}")
        log = await EventLog(s).list(run_id)
    try:
        invariant.check_initial(log, body.messages)
    except invariant.InvariantViolation as e:
        raise HTTPException(409, f"messages do not match the run's initial messages: {e}") from e
    agent, _ = await _load_agent(run.agent_id, run.agent_version, principal.tenant_id, allow_unpublished=True)  # 续跑固定原版本

    async def stream() -> AsyncIterator[dict[str, str]]:
        # 续跑者即发起人：credential_subject 与发起主体相同，不写入事件（机制保留，见 RunCtx.foreign_credential_subject）
        async for ev in runner.drive(
            run_id, agent, run.subject, body.messages, credential=principal.token, credential_subject=principal.subject
        ):
            if _client_visible(ev):
                yield _sse(ev)

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
    待审批期间仅有审批资格者（只读审批所需部分）不能取消，其他人 403，其他租户 404。"""
    if access != "full":
        raise _forbid(request, "only the run initiator, its approvers or an admin can cancel this run")
    async with SessionLocal() as s:
        log = EventLog(s)
        try:
            await log.transition(run_id, RunState.CANCELED, "user")
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        await s.commit()
    # 迁移提交后，若该 Run 正在本进程驱动（登记在 loop.active），再中断其 harness 会话：阻塞中的模型轮次
    # 尽快收尾，runner 随后发现 Run 已是 canceled，以 done(cancelled) 结束流而不再做任何状态迁移。
    running = active.get(run_id)
    if running is not None:
        harness, session = running
        await active.interrupt_quietly(harness, session, run_id)
    return {"run_id": run_id, "state": RunState.CANCELED}


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

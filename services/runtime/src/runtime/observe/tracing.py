"""Run 级 OTel Trace（GenAI 语义约定）：TracerProvider 安装、OTLP/HTTP 导出，以及 runner 使用的三类 span 助手。

Span 结构（一次 drive() 调用 = 一条 Trace）::

    run {agent_id}                       根 span：Run 元信息 + Langfuse 会话/用户/Trace 名
    ├── chat {model_alias}               每个模型轮次一个：GenAI 请求/响应属性、token 用量、事件日志 seq
    ├── execute_tool {tool_name}         每次工具调用一个：工具名、call_id、闸口结论、TOOL_REQUESTED 的 seq
    └── chat {model_alias} ...

设计要点：
- 导出走 OTLP/HTTP → OTel Collector → Langfuse（ADR-0002）；应用代码只依赖 OTLP 协议，不直接调 Langfuse API。
- `drive()` 是异步生成器，会在多次 yield 之间跨越调用方的执行上下文；为避免 contextvars 的
  attach/detach 跨 yield 失配（客户端断开、生成器被 GC 关闭时会出现 "Failed to detach context"），
  这里的 span **不设为当前上下文**，子 span 一律通过显式的父上下文（`RunSpan.context`）挂接。
- 未调用 `setup_tracing()` 时，`get_tracer()` 退回 OTel 全局 ProxyTracer，所有助手照常可用（只是不记录）。
- 数据分级（ADR-0010）：Run 带 confidential 污点时，调用方传 `redact=True`，模型轮次不记录提示词/补全原文。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode, Tracer, format_trace_id

from ai_contract import CardEvent, CardType, Event, ToolResultEvent, UsageEvent
from runtime.config import settings

if TYPE_CHECKING:
    from runtime.state.db import AgentVersion

TRACER_NAME = "ai-runtime"  # instrumentation scope 名称
CONTENT_LIMIT = 2000  # 提示词 / 补全原文写入 span 属性时的截断长度（字符）

_provider: TracerProvider | None = None  # 本进程安装的 TracerProvider；None 表示尚未 setup


def traces_endpoint() -> str | None:
    """解析 Trace 导出端点。遵循 OTel 规范：
    - `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` 是 traces 信号的完整 URL，优先且原样使用；
    - `OTEL_EXPORTER_OTLP_ENDPOINT`（即 settings.otel_exporter_endpoint）是基址，需拼接 `/v1/traces`
      （若用户已经写成 .../v1/traces 则不重复拼接）。
    两者都未设置时返回 None，表示不导出。
    """
    explicit = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    if explicit:
        return explicit
    base = settings.otel_exporter_endpoint
    if not base:
        return None
    base = base.rstrip("/")
    return base if base.endswith("/v1/traces") else f"{base}/v1/traces"


def setup_tracing() -> TracerProvider:
    """安装本进程的 TracerProvider（幂等，重复调用返回同一实例）。在 main.lifespan 启动时调用一次。

    无导出端点时仍然安装 Provider（不挂 exporter），便于测试通过 `add_span_processor` 注入内存导出器；
    有端点时挂 OTLP/HTTP 导出器 + BatchSpanProcessor（异步批量导出，不阻塞 Run 执行）。
    """
    global _provider
    if _provider is not None:
        return _provider
    resource = Resource.create({"service.name": "ai-runtime", "service.instance.id": settings.node_id, "deployment.environment": "local"})
    provider = TracerProvider(resource=resource)
    endpoint = traces_endpoint()
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    # 全局 Provider 只能设置一次：仅在尚未被其他代码设置时接管，供将来的自动埋点（httpx 等）复用。
    if isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
        trace.set_tracer_provider(provider)
    _provider = provider
    return provider


def add_span_processor(processor: SpanProcessor) -> None:
    """向本进程的 TracerProvider 追加一个 SpanProcessor（测试注入 InMemorySpanExporter 用）；必要时先 setup。"""
    setup_tracing().add_span_processor(processor)


def get_tracer() -> Tracer:
    """返回 Runtime 使用的 Tracer：已 setup 则取本进程 Provider，否则退回全局（默认是 ProxyTracer）。"""
    provider = _provider if _provider is not None else trace.get_tracer_provider()
    return provider.get_tracer(TRACER_NAME)


def force_flush(timeout_millis: int = 5000) -> bool:
    """把缓冲中的 span 立即导出（应用关停时调用）；未 setup 时直接返回 True。"""
    return _provider.force_flush(timeout_millis) if _provider is not None else True


def truncate(text: str, limit: int = CONTENT_LIMIT) -> str:
    """把写入 span 的文本截断到 limit 字符，避免超大属性拖慢导出或被后端拒收。"""
    return text if len(text) <= limit else text[:limit] + "…[truncated]"


def last_user_text(messages: list[dict[str, Any]]) -> str:
    """取消息历史中最后一条 user 消息的文本（非字符串内容按 str() 转换），没有则返回空串。"""
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content", "")
            return content if isinstance(content, str) else str(content)
    return ""


def mark_error(span: Span, exc: BaseException) -> None:
    """把异常记到 span 上并置状态为 ERROR。"""
    span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))


@contextmanager
def _ended(span: Span) -> Iterator[Span]:
    """span 生命周期：正常退出与异常退出都结束 span；普通异常额外记录并置 ERROR。

    只捕获 Exception：生成器被关闭（GeneratorExit，如客户端断开）不算业务错误，span 照常结束。
    """
    try:
        yield span
    except Exception as exc:
        mark_error(span, exc)
        raise
    finally:
        span.end()


@dataclass(frozen=True)
class RunSpan:
    """根 span 句柄：span 本身、32 位 hex 的 trace_id，以及挂接子 span 用的显式父上下文。"""

    span: Span
    trace_id: str
    context: Context


@contextmanager
def run_span(run_id: str, agent: AgentVersion, subject: str, model_alias: str | None = None) -> Iterator[RunSpan]:
    """一次 drive() 的根 span（名字 `run {agent_id}`）。yield 的 RunSpan.trace_id 即写入 t_run 与 DoneEvent 的 trace_id。

    langfuse.* 属性让 Langfuse 把同一 Run 的多次 drive（首跑 + 审批后续跑）归入同一个 Session（session.id=run_id）。
    """
    name = f"run {agent.agent_id}"
    attrs: dict[str, Any] = {
        "ai.run_id": run_id,
        "ai.agent_id": agent.agent_id,
        "ai.agent_version": agent.version,
        "ai.model_alias": model_alias or agent.model_alias,
        "ai.subject": subject,
        "ai.tenant_id": agent.tenant_id,
        "ai.harness": agent.harness or settings.harness,
        "langfuse.session.id": run_id,
        "langfuse.user.id": subject,
        "langfuse.trace.name": name,
    }
    # 每次 drive 按契约独立成 Trace；不能继承请求/测试埋点的 ambient parent。
    span = get_tracer().start_span(name, context=Context(), kind=trace.SpanKind.INTERNAL, attributes=attrs)
    with _ended(span):
        yield RunSpan(span=span, trace_id=format_trace_id(span.get_span_context().trace_id), context=trace.set_span_in_context(span))


def record_stop(span: Span, stop_reason: str) -> None:
    """在根 span 上记录本次 drive 的停止原因；stop_reason=error 时置 ERROR 状态。"""
    span.set_attribute("ai.stop_reason", stop_reason)
    if stop_reason == "error":
        span.set_status(Status(StatusCode.ERROR, "run stopped with error"))


@contextmanager
def model_span(model_alias: str, round_: int, parent: RunSpan | None = None, model_request_seq: int | None = None) -> Iterator[Span]:
    """一个模型轮次的 span（名字 `chat {model_alias}`，GenAI 语义约定 operation=chat）。

    结束前由调用方经 `record_usage` / `record_model_content` 补齐响应模型、用量、成本、事件 seq 与内容。
    """
    attrs: dict[str, Any] = {
        "gen_ai.operation.name": "chat",
        "gen_ai.system": "litellm",
        "gen_ai.request.model": model_alias,
        "ai.round": round_,
    }
    if model_request_seq is not None:
        attrs["ai.event.seq.model_request"] = model_request_seq
    span = get_tracer().start_span(
        f"chat {model_alias}", context=parent.context if parent else None, kind=trace.SpanKind.CLIENT, attributes=attrs
    )
    with _ended(span):
        yield span


def prompt_cache_stats(usage: UsageEvent, stable_prefix_hash: str | None) -> dict[str, Any]:
    """一轮模型调用的 Prompt Cache 统计（S2.6），写入 MODEL_RESPONSE 事件与模型 span。

    口径：input_tokens 为全部输入（含命中缓存部分），hit_ratio = cache_read_tokens / input_tokens（无输入时为 0）。
    stable_prefix_hash 是系统提示词 + 技能 + 工具三个固定块的哈希：同一前缀哈希下命中率持续为 0，
    说明前缀没有被模型侧缓存（例如低于厂商的最小缓存长度，或请求间前缀其实不同）。
    """
    hit = round(usage.cache_read_tokens / usage.input_tokens, 4) if usage.input_tokens else 0.0
    return {
        "stable_prefix_hash": stable_prefix_hash or "",
        "input_tokens": usage.input_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "hit_ratio": hit,
    }


def record_usage(span: Span, usage: UsageEvent, model_response_seq: int | None, stable_prefix_hash: str | None = None) -> None:
    """把 UsageEvent 写到模型 span：响应模型、输入/输出/缓存命中 token、成本、Prompt Cache 统计，以及 MODEL_RESPONSE 的 seq。

    Langfuse 的用量明细走 ``langfuse.observation.usage_details``（JSON）：input 为未命中缓存的输入，
    input_cached_tokens 为命中部分，二者之和即全部输入，Langfuse 侧按此统计命中率与成本。
    """
    if usage.model:
        span.set_attribute("gen_ai.response.model", usage.model)
    span.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
    span.set_attribute("gen_ai.usage.output_tokens", usage.output_tokens)
    span.set_attribute("gen_ai.usage.cache_read_tokens", usage.cache_read_tokens)  # 自定义：GenAI 约定尚无缓存命中字段
    span.set_attribute("ai.cost", usage.cost)
    stats = prompt_cache_stats(usage, stable_prefix_hash)
    if stable_prefix_hash:
        span.set_attribute("ai.context.stable_prefix_hash", stable_prefix_hash)
    span.set_attribute("ai.prompt_cache.hit_ratio", stats["hit_ratio"])
    details = {
        "input": max(usage.input_tokens - usage.cache_read_tokens, 0),
        "input_cached_tokens": usage.cache_read_tokens,
        "output": usage.output_tokens,
        "total": usage.input_tokens + usage.output_tokens,
    }
    span.set_attribute("langfuse.observation.usage_details", json.dumps(details))
    if model_response_seq is not None:
        span.set_attribute("ai.event.seq.model_response", model_response_seq)


def record_model_content(span: Span, prompt: str, completion: str, redact: bool = False, mode: str | None = None) -> None:
    """写入本轮提示词（最后一条用户消息）与补全文本，各自截断到 CONTENT_LIMIT。

    观测通道（S9 OPT-4，D152）按 Run 数据等级给出 mode：``full`` 记原文；``redacted``（internal）先经 redact_pii 同一套规则
    脱敏（手机号 / 邮箱 / 身份证号 / 银行卡号）再记录，并标记 ai.content_pii_redacted；``metadata``（confidential 或矩阵未准许）
    不写原文，只标记已脱敏。redact=True 等同 mode="metadata"（ADR-0010 旧入参）。
    """
    if redact or mode == "metadata":
        span.set_attribute("ai.content_redacted", True)
        return
    if mode == "redacted":
        from runtime.guard.hooks import PII_PATTERNS, redact_text  # 延迟导入：hooks 依赖 llm 客户端

        kinds = list(PII_PATTERNS)
        prompt, completion = redact_text(prompt, kinds)[0], redact_text(completion, kinds)[0]
        span.set_attribute("ai.content_pii_redacted", True)
    if prompt:
        span.set_attribute("gen_ai.prompt", truncate(prompt))
    if completion:
        span.set_attribute("gen_ai.completion", truncate(completion))


@contextmanager
def tool_span(name: str, call_id: str, parent: RunSpan | None = None) -> Iterator[Span]:
    """一次工具调用的 span（名字 `execute_tool {name}`，GenAI 语义约定 operation=execute_tool）。"""
    attrs: dict[str, Any] = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name, "gen_ai.tool.call.id": call_id}
    span = get_tracer().start_span(f"execute_tool {name}", context=parent.context if parent else None, attributes=attrs)
    with _ended(span):
        yield span


def tool_decision(ev: Event) -> str | None:
    """从工具代理产出的事件推导闸口/执行结论：ToolResultEvent 取 status（ok/error/denied/pending_approval）；
    影响预览卡片 = pending_approval（挂起待审批）；对账卡片 = unknown_result（结果未知转人工）。其他事件返回 None。"""
    if isinstance(ev, ToolResultEvent):
        return ev.status
    if isinstance(ev, CardEvent):
        return "pending_approval" if ev.card_type == CardType.IMPACT_PREVIEW else "unknown_result"
    return None


def record_tool_event(span: Span, ev: Event) -> None:
    """按工具代理产出的事件更新工具 span：写 ai.tool.decision；error / unknown_result 置 ERROR 状态。"""
    decision = tool_decision(ev)
    if decision is None:
        return
    span.set_attribute("ai.tool.decision", decision)
    if decision in ("error", "unknown_result"):
        span.set_status(Status(StatusCode.ERROR, f"tool decision: {decision}"))


def record_hooks(span: Span, records: list[dict[str, Any]]) -> None:
    """把 hook 结论写到 span（S5）：按拦截点分组，只记 hook 名称、结论与导致拒绝的 hook，不记负载原文 / 拒绝理由。

    属性：`ai.hook.<point>.names`、`ai.hook.<point>.verdicts`（与 names 一一对应），拒绝 / 失败时 `ai.hook.<point>.denied_by`。
    """
    by_point: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        by_point.setdefault(str(r.get("point", "unknown")), []).append(r)
    for point, recs in by_point.items():
        span.set_attribute(f"ai.hook.{point}.names", [str(r.get("name")) for r in recs])
        span.set_attribute(f"ai.hook.{point}.verdicts", [str(r.get("verdict")) for r in recs])
        failed = next((r for r in recs if r.get("verdict") in ("deny", "error", "timeout")), None)
        if failed is not None:
            span.set_attribute(f"ai.hook.{point}.denied_by", str(failed.get("name")))


def record_tool_requested_seq(span: Span, seq: int | None) -> None:
    """写入该调用 TOOL_REQUESTED 事件的 seq（找不到时不写）。"""
    if seq is not None:
        span.set_attribute("ai.event.seq.tool_requested", seq)

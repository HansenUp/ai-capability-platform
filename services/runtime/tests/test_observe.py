"""S1.5 观测：每次 drive 生成一条 OTel Trace（GenAI 语义约定）。

通过 tracing.add_span_processor 注入 InMemorySpanExporter + SimpleSpanProcessor，对 demo agent（mock harness）
分别跑一次普通对话与一次 `!tool echo` 工具调用，断言 span 结构、GenAI 属性、事件日志 seq 对齐，
以及 DoneEvent.trace_id / t_run.trace_id 与根 span 的 trace_id 一致。
"""

import dataclasses
import json

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode, format_trace_id


def _events(resp):
    """把 SSE 响应体解析为事件字典列表（按 `data:` 前缀行提取 JSON）。"""
    return [json.loads(line[5:].strip()) for line in resp.iter_lines() if line.startswith("data:")]


@pytest.fixture(scope="module")
def exporter(client):
    """向已 setup 的 TracerProvider 注入内存导出器（同步导出，span 结束即可读取）。"""
    from runtime.observe import tracing

    exp = InMemorySpanExporter()
    tracing.add_span_processor(SimpleSpanProcessor(exp))
    return exp


def _converse(client, content):
    """发起一次流式对话，返回 (run_id, SSE 事件列表, 事件日志)。"""
    body = {"agent_id": "demo", "messages": [{"role": "user", "content": content}]}
    with client.stream("POST", "/v1/messages", json=body) as r:
        assert r.status_code == 200
        run_id = r.headers["x-run-id"]
        evs = _events(r)
    return run_id, evs, client.get(f"/v1/runs/{run_id}/events").json()


def _run_spans(exporter, run_id) -> tuple[ReadableSpan, list[ReadableSpan]]:
    """取出属于该 run 的根 span 与其全部子 span（按开始时间排序）。"""
    finished = exporter.get_finished_spans()
    root = next(s for s in finished if s.attributes.get("ai.run_id") == run_id)
    children = sorted(
        (s for s in finished if s.parent is not None and s.parent.span_id == root.context.span_id), key=lambda s: s.start_time
    )
    return root, children


def _db_trace_id(client, run_id):
    """在应用自己的事件循环里读 t_run.trace_id（RunView 不含该字段，直接查库）。"""

    async def load():
        from runtime.state.db import Run, SessionLocal

        async with SessionLocal() as s:
            run = await s.get(Run, run_id)
            return run.trace_id if run else None

    return client.portal.call(load)


def _seq(log, type_, call_id=None):
    """取事件日志中某类事件的 seq 列表（可按 call_id 过滤）。"""
    return [e["seq"] for e in log if e["type"] == type_ and (call_id is None or e["payload"].get("call_id") == call_id)]


def test_chat_run_produces_root_and_chat_spans(client, exporter):
    """普通对话：根 span `run demo` 带 Run 与 Langfuse 属性；chat 子 span 带用量、响应模型与事件 seq；
    DoneEvent 与 t_run 的 trace_id 都等于根 span 的 trace_id。"""
    exporter.clear()
    run_id, evs, log = _converse(client, "你好，观测")
    root, children = _run_spans(exporter, run_id)

    assert root.name == "run demo"
    a = root.attributes
    assert a["ai.agent_id"] == "demo" and a["ai.agent_version"] == "1" and a["ai.harness"] == "mock"
    assert a["langfuse.session.id"] == run_id and a["langfuse.trace.name"] == "run demo"
    assert a["langfuse.user.id"] == a["ai.subject"]
    assert a["ai.stop_reason"] == "end_turn"
    assert root.resource.attributes["service.name"] == "ai-runtime"

    trace_id = format_trace_id(root.context.trace_id)
    done = evs[-1]
    assert done["type"] == "done" and done["trace_id"] == trace_id
    assert _db_trace_id(client, run_id) == trace_id

    assert [c.name for c in children] == [f"chat {a['ai.model_alias']}"]
    chat = children[0].attributes
    assert chat["gen_ai.operation.name"] == "chat" and chat["gen_ai.system"] == "litellm"
    assert chat["gen_ai.request.model"] == a["ai.model_alias"] and chat["gen_ai.response.model"] == "mock"
    assert chat["gen_ai.usage.output_tokens"] == 16 and "gen_ai.usage.input_tokens" in chat
    assert chat["ai.event.seq.model_request"] == _seq(log, "model.request")[0]
    assert chat["ai.event.seq.model_response"] == _seq(log, "model.response")[0]
    assert chat["gen_ai.prompt"] == "你好，观测" and "你好，观测" in chat["gen_ai.completion"]


def test_tool_run_produces_execute_tool_span(client, exporter):
    """工具调用：根 span 下依次为 chat（请求工具）→ execute_tool echo → chat（收到结果后作答），
    工具 span 带 call_id、闸口结论与 TOOL_REQUESTED 的 seq。"""
    exporter.clear()
    run_id, evs, log = _converse(client, '!tool echo {"x": 1}')
    root, children = _run_spans(exporter, run_id)
    call_id = next(e for e in evs if e["type"] == "tool_use")["call_id"]

    assert [c.name.split(" ")[0] for c in children] == ["chat", "execute_tool", "chat"]
    tool = children[1]
    assert tool.name == "execute_tool echo"
    assert tool.attributes["gen_ai.operation.name"] == "execute_tool" and tool.attributes["gen_ai.tool.name"] == "echo"
    assert tool.attributes["gen_ai.tool.call.id"] == call_id
    assert tool.attributes["ai.tool.decision"] == "ok"
    assert tool.attributes["ai.event.seq.tool_requested"] == _seq(log, "tool.requested", call_id)[0]
    assert tool.status.status_code != StatusCode.ERROR

    assert children[0].attributes["gen_ai.response.finish_reasons"] == ("tool_use",)
    assert children[2].attributes["gen_ai.usage.output_tokens"] == 16
    assert children[2].attributes["ai.event.seq.model_response"] == _seq(log, "model.response")[-1]
    assert all(c.context.trace_id == root.context.trace_id for c in children)
    assert evs[-1]["trace_id"] == format_trace_id(root.context.trace_id) == _db_trace_id(client, run_id)


def test_traces_endpoint_follows_otlp_spec(monkeypatch):
    """OTEL_EXPORTER_OTLP_ENDPOINT 是基址需拼 /v1/traces；OTEL_EXPORTER_OTLP_TRACES_ENDPOINT 原样使用；都没有则不导出。"""
    from runtime.config import settings
    from runtime.observe import tracing

    def use(endpoint):
        """settings 是冻结的 dataclass：替换 tracing 模块引用的那份实例。"""
        monkeypatch.setattr(tracing, "settings", dataclasses.replace(settings, otel_exporter_endpoint=endpoint))

    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    use(None)
    assert tracing.traces_endpoint() is None
    use("http://localhost:4318/")
    assert tracing.traces_endpoint() == "http://localhost:4318/v1/traces"
    use("http://localhost:4318/v1/traces")
    assert tracing.traces_endpoint() == "http://localhost:4318/v1/traces"
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://collector:4318/custom")
    assert tracing.traces_endpoint() == "http://collector:4318/custom"


def test_run_trace_is_root_under_ambient_span(client, exporter):
    """An ambient request/instrumentation span must not adopt an independent Run trace."""
    from types import SimpleNamespace

    from runtime.observe import tracing

    agent = SimpleNamespace(agent_id="ambient-probe", version="1", model_alias="mock", tenant_id="default", harness="mock")
    with tracing.get_tracer().start_as_current_span("ambient-request") as ambient:
        with tracing.run_span("ambient-run", agent, "human:alice") as run:
            with tracing.model_span("mock", 1, run) as child:
                child_id = child.get_span_context().span_id
            run_id = run.span.get_span_context().span_id
            assert run.span.get_span_context().trace_id != ambient.get_span_context().trace_id
    root = next(s for s in exporter.get_finished_spans() if s.context.span_id == run_id)
    model = next(s for s in exporter.get_finished_spans() if s.context.span_id == child_id)
    assert root.parent is None
    assert model.parent.span_id == run_id
    assert model.context.trace_id == root.context.trace_id

"""S6 WP-D 多模态（图片附件，D102）：图片只以 image_ref 引用进入平台历史与事件日志，litellm 适配器发送时才读工作区
转成 OpenAI 兼容 image_url data URL；harness 或模型别名不支持图片 → 422 ATTACHMENT_MODALITY_UNSUPPORTED；张数 / 大小 /
类型限制；内联图片 422；附件归属规则不变（他人工件 403）；事件日志、重建历史与 Trace 中不出现 base64。

模型网关以 httpx.MockTransport 模拟（本机 DeepSeek 无视觉能力），只断言请求体结构。
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
import uuid
from typing import Any

import httpx
import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from test_harness_litellm import Gateway, text_turn

from ai_contract import HookSet, RunEvent, SessionSpec, TurnInput
from runtime.api import messages
from runtime.config import settings
from runtime.harness import registry
from runtime.harness.litellm import LiteLLMHarness
from runtime.llm import LLMClient
from runtime.run import invariant

H = {"X-AI-Subject": "human:s6-mm"}
ALIAS = "default-chat"  # conftest 已把它列入 CONFIDENTIAL_MODEL_ALIASES（无分级记录的工件按 confidential 继承）


def _png() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + os.urandom(64)


@pytest.fixture(scope="module")
def exporter(client: Any) -> InMemorySpanExporter:
    from runtime.observe import tracing

    exp = InMemorySpanExporter()
    tracing.add_span_processor(SimpleSpanProcessor(exp))
    return exp


@pytest.fixture
def vision(monkeypatch: pytest.MonkeyPatch) -> None:
    """把测试别名列入 VISION_MODEL_ALIASES。"""
    monkeypatch.setattr(messages, "settings", dataclasses.replace(messages.settings, vision_model_aliases=(ALIAS,)))


def _own_run(client: Any, headers: dict[str, str] = H) -> str:
    """以 headers 的主体跑一次 demo（mock harness），返回 run_id，用作附件工件的来源 Run。"""
    body = {"agent_id": "demo", "messages": [{"role": "user", "content": "hi"}], "stream": False}
    r = client.post("/v1/messages", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return str(r.json()["run_id"])


def _artifact(run_id: str, data: bytes, ext: str = "png") -> str:
    """在 run_id 的工作区写一个工件，返回 artifact:// 引用。"""
    aid = uuid.uuid4().hex
    d = os.path.join(settings.workspace_dir, run_id)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{aid}.{ext}"), "wb") as f:
        f.write(data)
    return f"artifact://{aid}"


def _vision_agent(client: Any) -> tuple[str, Gateway]:
    """发布一个使用 litellm 适配器（注入模拟网关）的 Agent，返回（agent_id，模拟网关）。"""
    name = f"s6-mm-{uuid.uuid4().hex[:6]}"
    gw = Gateway(text_turn("图里是一张工单截图。"))
    harness = LiteLLMHarness(client=LLMClient(base_url="http://gw.test", api_key="k", max_retries=0, transport=httpx.MockTransport(gw)))
    registry.register(name, harness)
    body = {"agent_id": name, "version": "1", "harness": name, "model_alias": ALIAS, "system_prompt": "x"}
    assert client.post("/admin/agents", json=body).status_code == 201
    assert client.post(f"/admin/agents/{name}/versions/1/publish", json={"eval_passed": True}).status_code == 200
    return name, gw


def _chat(client: Any, agent: str, attachments: list[str], headers: dict[str, str] = H) -> httpx.Response:
    body = {"agent_id": agent, "messages": [{"role": "user", "content": "看图"}], "attachments": attachments, "stream": False}
    return client.post("/v1/messages", json=body, headers=headers)


def test_image_attachment_sent_as_data_url_and_logged_as_reference(client: Any, vision: None, exporter: InMemorySpanExporter) -> None:
    src = _own_run(client)
    png = _png()
    b64 = base64.b64encode(png).decode()
    ref = _artifact(src, png)
    aid = ref.removeprefix("artifact://")
    agent, gw = _vision_agent(client)
    r = _chat(client, agent, [ref])
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    assert r.json()["content"] == "图里是一张工单截图。"

    # 请求体：最后一条用户消息为文本块 + OpenAI 兼容 image_url data URL
    sent = gw.requests[0]["messages"]
    assert sent[-2] == {"role": "user", "content": "看图"}
    text, image = sent[-1]["content"]
    assert text["type"] == "text" and ref in text["text"]
    assert image == {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}

    # 工件按规范扩展名复制进新 Run 的工作区
    with open(os.path.join(settings.workspace_dir, run_id, f"{aid}.png"), "rb") as f:
        assert f.read() == png

    # 事件日志只含引用与元数据：RUN_CREATED 记录附件分级，初始消息指纹覆盖 image_ref（含 sha256）
    events = client.get(f"/v1/runs/{run_id}/events", headers=H).json()
    assert b64 not in json.dumps(events)
    created = next(e["payload"] for e in events if e["type"] == "run.created")
    assert created["attachments"] == [{"artifact_id": aid, "source_run_id": src, "data_class": "confidential"}]
    part = {"type": "image_ref", "ref": ref, "media_type": "image/png", "size": len(png), "sha256": hashlib.sha256(png).hexdigest()}
    initial = [{"role": "user", "content": "看图"}, {"role": "user", "content": [{"type": "text", "text": f"图片附件：{ref}"}, part]}]
    assert created[invariant.INITIAL_MESSAGES_HASH] == invariant.message_hash(initial)

    # 由事件日志重建的历史仍只含引用
    rebuilt = invariant.rebuild_messages([RunEvent(**e) for e in events], initial)
    assert rebuilt[1]["content"][1] == part and b64 not in json.dumps(rebuilt)

    # Trace 不含图片内容
    for span in exporter.get_finished_spans():
        assert all(b64 not in str(v) for v in (span.attributes or {}).values())


def test_text_and_image_attachments_mixed(client: Any, vision: None) -> None:
    src = _own_run(client)
    txt = _artifact(src, b'{"x": 1}', "txt")
    img = _artifact(src, _png())
    agent, gw = _vision_agent(client)
    r = _chat(client, agent, [txt, img])
    assert r.status_code == 200, r.text
    note = gw.requests[0]["messages"][-1]["content"]
    assert note[0] == {"type": "text", "text": f"附件（可用 read_artifact 读取）：{txt}\n图片附件：{img}"}
    assert note[1]["type"] == "image_url"
    ws = os.path.join(settings.workspace_dir, r.json()["run_id"])
    assert sorted(os.listdir(ws)) == sorted([f"{txt.removeprefix('artifact://')}.txt", f"{img.removeprefix('artifact://')}.png"])


def test_jpeg_with_wrong_extension_is_stored_by_actual_type(client: Any, vision: None) -> None:
    """类型按文件头判定：扩展名为 .png 的 JPEG 以 image/jpeg 引用、在新工作区落为 .jpg。"""
    src = _own_run(client)
    jpeg = b"\xff\xd8\xff\xe0" + os.urandom(32)
    ref = _artifact(src, jpeg)
    agent, gw = _vision_agent(client)
    r = _chat(client, agent, [ref])
    assert r.status_code == 200, r.text
    url = gw.requests[0]["messages"][-1]["content"][1]["image_url"]["url"]
    assert url == "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    assert os.path.isfile(os.path.join(settings.workspace_dir, r.json()["run_id"], f"{ref.removeprefix('artifact://')}.jpg"))


def test_harness_without_vision_is_422(client: Any, vision: None) -> None:
    """demo 使用 mock harness（supports_vision=False）：即使别名在清单内也 422。"""
    ref = _artifact(_own_run(client), _png())
    r = _chat(client, "demo", [ref])
    assert r.status_code == 422 and r.json()["detail"].startswith("ATTACHMENT_MODALITY_UNSUPPORTED")


def test_alias_without_vision_is_422(client: Any) -> None:
    """VISION_MODEL_ALIASES 默认空：支持图片的 harness 也一律 422（失败关闭）；文本工件不受影响。"""
    src = _own_run(client)
    agent, gw = _vision_agent(client)
    r = _chat(client, agent, [_artifact(src, _png())])
    assert r.status_code == 422 and r.json()["detail"].startswith("ATTACHMENT_MODALITY_UNSUPPORTED")
    assert gw.requests == []
    assert _chat(client, agent, [_artifact(src, b"{}", "txt")]).status_code == 200


def test_image_limits(client: Any, vision: None, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _own_run(client)
    agent, gw = _vision_agent(client)
    a, b = _artifact(src, _png()), _artifact(src, _png())
    monkeypatch.setattr(messages, "settings", dataclasses.replace(messages.settings, attachment_image_max_count=1))
    r = _chat(client, agent, [a, b])
    assert r.status_code == 422 and r.json()["detail"].startswith("ATTACHMENT_TOO_MANY")
    monkeypatch.setattr(messages, "settings", dataclasses.replace(messages.settings, attachment_image_max_bytes=16))
    r = _chat(client, agent, [a])
    assert r.status_code == 413 and r.json()["detail"].startswith("ATTACHMENT_TOO_LARGE")
    monkeypatch.setattr(messages, "settings", dataclasses.replace(messages.settings, attachment_image_max_bytes=1024))
    r = _chat(client, agent, [_artifact(src, b"<svg xmlns='http://www.w3.org/2000/svg'/>")])
    assert r.status_code == 422 and r.json()["detail"].startswith("ATTACHMENT_TYPE_UNSUPPORTED")
    assert _chat(client, agent, [_artifact(src, b"%PDF-1.7", "pdf")]).status_code == 403  # 不是可附加的工件类型
    assert gw.requests == []


def test_other_users_image_is_403(client: Any, vision: None) -> None:
    """附件归属规则不变：他人的图片工件与不存在同样 403，且先于 vision / 大小校验。"""
    ref = _artifact(_own_run(client, {"X-AI-Subject": "human:s6-other"}), _png())
    agent, _ = _vision_agent(client)
    assert _chat(client, agent, [ref]).status_code == 403


@pytest.mark.parametrize(
    "part",
    [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
        {"type": "image_ref", "ref": "artifact://" + "a" * 32, "media_type": "image/png", "size": 1, "sha256": "0" * 64},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
        {"type": "file", "file": {"file_data": "data:application/pdf;base64,AAAA", "filename": "x.pdf"}},
        {"type": "input_file", "file_data": "data:application/pdf;base64,AAAA"},
        {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}},
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "AAAA"}},
        {"type": "no-such-type", "data": "AAAA"},
        {"type": "text", "text": "x", "image_url": {"url": "data:image/png;base64,AAAA"}},  # 文本块夹带其他字段
        {"type": "text", "text": ["not", "a", "string"]},
        "bare string part",
    ],
)
def test_inline_content_rejected(client: Any, vision: None, part: Any) -> None:
    """白名单：列表形式的 content 只允许纯文本块；/v1/messages 与 /v1/runs 都覆盖。"""
    content = [{"type": "text", "text": "看图"}, part]
    r = client.post("/v1/messages", json={"agent_id": "demo", "messages": [{"role": "user", "content": content}]}, headers=H)
    assert r.status_code == 422 and r.json()["detail"].startswith("INLINE_CONTENT_UNSUPPORTED")
    r = client.post("/v1/runs", json={"agent_id": "demo", "input": [{"role": "user", "content": content}]}, headers=H)
    assert r.status_code == 422 and r.json()["detail"].startswith("INLINE_CONTENT_UNSUPPORTED")


@pytest.mark.parametrize("content", [{"type": "image_ref", "ref": "artifact://" + "a" * 32}, 42])
def test_non_string_non_list_content_rejected(client: Any, content: Any) -> None:
    r = client.post("/v1/messages", json={"agent_id": "demo", "messages": [{"role": "user", "content": content}]}, headers=H)
    assert r.status_code == 422 and r.json()["detail"].startswith("INLINE_CONTENT_UNSUPPORTED")
    r = client.post("/v1/runs", json={"agent_id": "demo", "input": [{"role": "user", "content": content}]}, headers=H)
    assert r.status_code == 422 and r.json()["detail"].startswith("INLINE_CONTENT_UNSUPPORTED")


def test_text_parts_still_accepted(client: Any) -> None:
    content = [{"type": "text", "text": "你好"}, {"type": "text", "text": "再见"}]
    body = {"agent_id": "demo", "messages": [{"role": "user", "content": content}], "stream": False}
    assert client.post("/v1/messages", json=body, headers=H).status_code == 200
    assert client.post("/v1/runs", json={"agent_id": "demo", "input": [{"role": "user", "content": content}]}, headers=H).status_code == 202


async def test_litellm_tampered_image_is_attachment_unavailable(tmp_path: Any) -> None:
    """工作区里的图片与引用记录的 sha256 不符（或缺失）：不发请求，Error(ATTACHMENT_UNAVAILABLE) → Usage → Done(error)。"""
    png = _png()
    aid = uuid.uuid4().hex
    (tmp_path / f"{aid}.png").write_bytes(b"\x89PNG\r\n\x1a\n tampered")
    part = {
        "type": "image_ref",
        "ref": f"artifact://{aid}",
        "media_type": "image/png",
        "size": len(png),
        "sha256": hashlib.sha256(png).hexdigest(),
    }
    gw = Gateway(text_turn("不该被调用"))
    h = LiteLLMHarness(client=LLMClient(base_url="http://gw.test", api_key="k", max_retries=0, transport=httpx.MockTransport(gw)))
    spec = SessionSpec(
        run_id="r", agent_id="a", agent_version="1", model_alias=ALIAS, system_prompt="", subject="human:t", workspace_dir=str(tmp_path)
    )
    session = await h.open_session(spec)
    turn = TurnInput(messages=[{"role": "user", "content": [{"type": "text", "text": "看图"}, part]}])
    evs = [ev async for ev in h.run_turn(session, turn, [], [], HookSet())]
    assert [e.type for e in evs] == ["error", "usage", "done"]
    assert evs[0].code == "ATTACHMENT_UNAVAILABLE" and evs[-1].stop_reason == "error"
    assert gw.requests == []
    assert h.capabilities().supports_vision is True

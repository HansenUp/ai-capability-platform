"""claude-sdk 适配器的图片输入（S6，D102，Contract V1.2 image_ref）：合成转录时才从 Run 工作区读取图片并写成
Anthropic image 块（base64 source），SDK 能解析该转录；图片缺失或与记录的 sha256 不符时不启动查询（ATTACHMENT_UNAVAILABLE）。"""

from __future__ import annotations

import base64
import hashlib
import os
import uuid
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("claude_agent_sdk")

from claude_agent_sdk import TextBlock, get_session_messages  # noqa: E402
from test_harness import FakeClient, assistant, collect, kinds, make_harness, make_spec, result  # noqa: E402

from ai_contract import ImageRefError  # noqa: E402
from ai_harness_claude_sdk import from_api_messages, to_api_messages, write_transcript  # noqa: E402
from ai_harness_claude_sdk.transcript import load_transcript  # noqa: E402

SID = "5b2f8c1a-0d3e-4f6a-8b9c-1e2d3f4a5b6c"


def _image(ws: Path, data: bytes | None = None) -> tuple[dict[str, Any], bytes]:
    """在工作区写一张 PNG，返回（image_ref 块，图片字节）。"""
    png = b"\x89PNG\r\n\x1a\n" + os.urandom(48)
    aid = uuid.uuid4().hex
    ws.mkdir(parents=True, exist_ok=True)
    (ws / f"{aid}.png").write_bytes(png if data is None else data)
    part = {
        "type": "image_ref",
        "ref": f"artifact://{aid}",
        "media_type": "image/png",
        "size": len(png),
        "sha256": hashlib.sha256(png).hexdigest(),
    }
    return part, png


def _history(part: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"role": "user", "content": "看图"}, {"role": "user", "content": [{"type": "text", "text": f"图片附件：{part['ref']}"}, part]}]


def test_transcript_image_block_parsed_by_sdk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = (tmp_path / "ws").resolve()
    part, png = _image(ws)
    path = write_transcript(tmp_path / "home", str(ws), SID, _history(part), model="m", workspace_dir=str(ws))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "home"))
    parsed = get_session_messages(SID, directory=str(ws))
    api = [{"role": m.message["role"], "content": m.message["content"]} for m in parsed]
    assert api == load_transcript(path)
    # 相邻用户消息合并为一条：用户文本、附件说明、图片块
    assert api == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看图"},
                {"type": "text", "text": f"图片附件：{part['ref']}"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(png).decode()}},
            ],
        }
    ]
    # 逆变换不还原图片块（base64 无法反推平台引用），文本照常还原
    assert from_api_messages(api) == [{"role": "user", "content": "看图"}, {"role": "user", "content": f"图片附件：{part['ref']}"}]


def test_image_ref_requires_workspace_and_matching_content(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    part, _ = _image(ws)
    with pytest.raises(ImageRefError):
        to_api_messages(_history(part))  # 没有工作区
    with pytest.raises(ImageRefError):
        to_api_messages(_history(part | {"sha256": "0" * 64}), workspace_dir=str(ws))
    # 不含 image_ref 的内容块列表保持 S6 之前的行为（整体 JSON 文本）
    other = [{"role": "user", "content": [{"type": "text", "text": "x"}]}]
    assert to_api_messages(other) == [{"role": "user", "content": '[{"type": "text", "text": "x"}]'}]


async def test_run_turn_sends_image_block_via_transcript(tmp_path: Any) -> None:
    async def script(c: FakeClient) -> Any:
        yield assistant(TextBlock(text="是一张截图"), mid="m1", inp=1500, out=5)
        yield result(1500, 5)

    h, clients = make_harness(script)
    assert h.capabilities().supports_vision is True
    spec = make_spec(tmp_path)
    part, png = _image(Path(str(spec.workspace_dir)))
    handle = await h.open_session(spec)
    events = await collect(h, handle, _history(part))
    assert kinds(events) == ["text", "usage", "done:end_turn"]
    image = clients[0].raw[0]["content"][-1]
    assert image == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(png).decode()}}
    # 事件里只有文本与用量，没有图片内容
    assert all(base64.b64encode(png).decode() not in e.model_dump_json() for e in events)


async def test_tampered_image_is_attachment_unavailable(tmp_path: Any) -> None:
    h, clients = make_harness(None)
    spec = make_spec(tmp_path)
    part, _ = _image(Path(str(spec.workspace_dir)), data=b"\x89PNG\r\n\x1a\n tampered")
    handle = await h.open_session(spec)
    events = await collect(h, handle, _history(part))
    assert kinds(events) == ["error", "usage", "done:error"] and events[0].code == "ATTACHMENT_UNAVAILABLE"
    assert clients == [] and handle.session_id in h._sessions  # 不启动查询，会话保留

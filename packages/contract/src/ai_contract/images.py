"""图片附件引用（V1.2 新增，S6 多模态 / D102）。

平台历史（``TurnInput.messages``）里只出现图片的**引用**，从不出现图片内容：用户消息的 ``content`` 可以是内容块
列表，其中图片块为 ``image_ref``::

    {"type": "image_ref", "ref": "artifact://<32 位十六进制>", "media_type": "image/png", "size": 1234, "sha256": "<hex>"}

图片文件是本 Run 工作区（``SessionSpec.workspace_dir``）内的工件 ``<id>.<扩展名>``（扩展名由 ``media_type`` 决定，
见 ``IMAGE_MEDIA_TYPES``），由 Runtime 在附件校验通过后复制进来。声明 ``supports_vision=True`` 的适配器在**发送给
模型时**用 ``load_image_ref`` 读取并转成 base64（OpenAI 兼容的 image_url data URL、Anthropic 的 image 块等），
不得把 base64 写进任何统一事件；未声明的适配器不会收到 ``image_ref``（Runtime 以 422 拒绝）。

``sha256`` 随初始消息指纹写入事件日志，读取时校验，保证模型看到的就是创建 Run 时校验过的那张图。
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
from collections.abc import Mapping
from typing import Any

IMAGE_REF_TYPE = "image_ref"  # 平台历史中图片引用内容块的 type
# 允许的图片类型 → 工作区内的规范扩展名
IMAGE_MEDIA_TYPES: dict[str, str] = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
_REF = re.compile(r"^artifact://([0-9a-f]{32})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ImageRefError(ValueError):
    """图片引用不合法、工件不存在或内容与记录的 sha256 不一致（消息中不含图片内容）。"""


def sniff_media_type(head: bytes) -> str | None:
    """按文件头魔数识别图片类型（不信任扩展名）；不是允许的类型时返回 None。至少需要前 12 个字节。"""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def is_image_ref(part: Any) -> bool:
    """是否为 ``image_ref`` 内容块。"""
    return isinstance(part, Mapping) and part.get("type") == IMAGE_REF_TYPE


def image_ref_path(workspace_dir: str, part: Mapping[str, Any]) -> str:
    """``image_ref`` 在工作区内对应的文件路径；引用格式、类型或指纹字段不合法时抛 ImageRefError（杜绝路径穿越）。"""
    m = _REF.match(str(part.get("ref", "")))
    ext = IMAGE_MEDIA_TYPES.get(str(part.get("media_type", "")))
    if m is None or ext is None or not _SHA256.match(str(part.get("sha256", ""))):
        raise ImageRefError("malformed image_ref (need artifact://<32 hex>, an allowed media_type and sha256)")
    return os.path.join(workspace_dir, f"{m.group(1)}.{ext}")


def load_image_ref(workspace_dir: str | None, part: Mapping[str, Any]) -> tuple[str, str]:
    """读取 ``image_ref`` 指向的工作区图片，校验 sha256 与文件头类型，返回 ``(media_type, base64 文本)``。

    同步文件操作，异步调用方应放进线程执行。任何不一致都抛 ImageRefError（失败关闭）。"""
    if not workspace_dir:
        raise ImageRefError("image_ref requires a run workspace")
    path = image_ref_path(workspace_dir, part)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise ImageRefError(f"image attachment {part.get('ref')} not found in this run") from e
    if hashlib.sha256(data).hexdigest() != part.get("sha256"):
        raise ImageRefError(f"image attachment {part.get('ref')} does not match its recorded sha256")
    media_type = str(part["media_type"])
    if sniff_media_type(data[:16]) != media_type:
        raise ImageRefError(f"image attachment {part.get('ref')} is not a valid {media_type}")
    return media_type, base64.b64encode(data).decode("ascii")

"""数据分级与六通道处理矩阵（Contract V1.4 新增，S9 OPT-4，D152）。

数据等级 ``Level``：public < internal < confidential（可比较，取值即字符串）。数据流向外部处理方的六个通道 ``CHANNELS``：

- ``model``：发给哪个模型别名推理；
- ``embedding``：发给哪个嵌入 provider 向量化（检索的查询语句也是数据）；
- ``rerank``：发给哪个重排 provider；
- ``observe``：写进哪个观测后端（Trace / 网关回调）；
- ``fallback``：主模型失败时是否允许网关自动回退（processor 为请求的模型别名）；
- ``memory``：写进哪个跨会话记忆存储。

``Matrix.allows(channel, level, processor)``：矩阵为每个通道、每个等级列出允许的处理方（``"*"`` 表示任意）；**矩阵未覆盖即拒绝**
（失败关闭）。等级取值无法识别时按 confidential 处理。``Matrix.signed``：矩阵带合规签署信息（``signoff.by`` 与 ``signoff.at``
非空）；生产基线要求已签署，开发态使用未签署的 ``services/runtime/data_class_matrix.dev.json``。

矩阵文件（JSON）::

    {"schema": "ai-platform.data-class-matrix/v1", "version": "...",
     "channels": {"model": {"public": ["*"], "internal": ["alias-a"], "confidential": []}, ...},
     "signoff": {"by": "...", "at": "...", "ref": "..."} | null}
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

MATRIX_SCHEMA = "ai-platform.data-class-matrix/v1"
CHANNELS: tuple[str, ...] = ("model", "embedding", "rerank", "observe", "fallback", "memory")
WILDCARD = "*"


class Level(StrEnum):
    """数据等级：public < internal < confidential。比较按等级高低（不是字符串字典序）。"""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"

    @property
    def rank(self) -> int:
        return _RANK[self]

    def __lt__(self, other: object) -> bool:
        return self.rank < Level.parse(other).rank

    def __le__(self, other: object) -> bool:
        return self.rank <= Level.parse(other).rank

    def __gt__(self, other: object) -> bool:
        return self.rank > Level.parse(other).rank

    def __ge__(self, other: object) -> bool:
        return self.rank >= Level.parse(other).rank

    @classmethod
    def parse(cls, value: object) -> Level:
        """把字符串 / Level 解析为 Level；无法识别（含 None 以外的未知值）按 confidential 处理（失败关闭）。None 视为 public。"""
        if isinstance(value, Level):
            return value
        if value is None:
            return cls.PUBLIC
        try:
            return cls(str(value))
        except ValueError:
            return cls.CONFIDENTIAL

    @classmethod
    def highest(cls, values: Iterable[object]) -> Level:
        """一组等级中最高的一个；空集合为 public。"""
        out = cls.PUBLIC
        for v in values:
            lv = cls.parse(v)
            if lv.rank > out.rank:
                out = lv
        return out


_RANK = {Level.PUBLIC: 0, Level.INTERNAL: 1, Level.CONFIDENTIAL: 2}


class MatrixError(ValueError):
    """矩阵文件不合规（未知通道 / 等级、处理方不是字符串列表、schema 不符）。"""


@dataclass(frozen=True)
class Matrix:
    """六通道数据分级矩阵（只读）。用 ``Matrix.load(path)`` 或 ``Matrix.from_dict(obj)`` 构造。"""

    rules: Mapping[str, Mapping[str, frozenset[str]]] = field(default_factory=dict)
    version: str = ""
    signoff: Mapping[str, Any] | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Matrix:
        """校验并构造矩阵；不合规抛 MatrixError。"""
        if not isinstance(data, Mapping):
            raise MatrixError("matrix must be a JSON object")
        if data.get("schema", MATRIX_SCHEMA) != MATRIX_SCHEMA:
            raise MatrixError(f"unsupported matrix schema {data.get('schema')!r}")
        channels = data.get("channels")
        if not isinstance(channels, Mapping):
            raise MatrixError("matrix.channels must be an object")
        rules: dict[str, dict[str, frozenset[str]]] = {}
        for channel, levels in channels.items():
            if channel not in CHANNELS:
                raise MatrixError(f"unknown channel {channel!r}; expected one of {', '.join(CHANNELS)}")
            if not isinstance(levels, Mapping):
                raise MatrixError(f"matrix.channels.{channel} must be an object")
            per: dict[str, frozenset[str]] = {}
            for level, processors in levels.items():
                if level not in _RANK:
                    raise MatrixError(f"unknown level {level!r} in channel {channel!r}")
                if not isinstance(processors, list) or not all(isinstance(p, str) and p for p in processors):
                    raise MatrixError(f"matrix.channels.{channel}.{level} must be a list of non-empty strings")
                per[level] = frozenset(processors)
            rules[channel] = per
        signoff = data.get("signoff")
        if signoff is not None and not isinstance(signoff, Mapping):
            raise MatrixError("matrix.signoff must be an object or null")
        return cls(rules=rules, version=str(data.get("version") or ""), signoff=signoff)

    @classmethod
    def load(cls, path: str | Path) -> Matrix:
        """从 JSON 文件加载矩阵；文件不存在 / 不是合法 JSON / 不合规均抛异常（调用方失败关闭）。"""
        with open(path, encoding="utf-8") as f:
            try:
                data = json.load(f)
            except ValueError as e:
                raise MatrixError(f"matrix file {path} is not valid JSON: {e}") from e
        return cls.from_dict(data)

    def allows(self, channel: str, level: object, processor: str) -> bool:
        """该等级的数据能否经该通道交给该处理方。矩阵未覆盖（通道 / 等级缺失、处理方不在列表）即拒绝。"""
        if channel not in CHANNELS:
            raise ValueError(f"unknown channel {channel!r}")
        allowed = self.rules.get(channel, {}).get(Level.parse(level).value)
        if not allowed:
            return False
        return WILDCARD in allowed or processor in allowed

    def processors(self, channel: str, level: object) -> frozenset[str]:
        """该通道该等级允许的处理方集合（可能含 "*"）。"""
        return self.rules.get(channel, {}).get(Level.parse(level).value, frozenset())

    @property
    def signed(self) -> bool:
        """矩阵是否已经合规签署（signoff.by 与 signoff.at 均为非空字符串）。"""
        s = self.signoff or {}
        return all(isinstance(s.get(k), str) and s.get(k, "").strip() for k in ("by", "at"))

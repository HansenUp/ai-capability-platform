"""Skill 加载 / 物化 / 吊销的配置（S9.1/9.5）。每次 ``load()`` 现读环境变量（便于测试）；``config.py`` 归 WP-B，故独立成模块。

- ``SKILLS_DIR``：物化根目录，包落在 ``SKILLS_DIR/<name>/<version>``（默认 ``./runtime-skills``）。
- ``SKILL_TRUST_KEYS_FILE``：信任公钥（JWKS JSON，EC P-256；按 RFC 7638 指纹作 kid，轮换时并存新旧公钥）。
- ``SKILL_PACKAGE_MAX_BYTES``：包解压后总字节上限（默认 10 MiB）；``SKILL_PACKAGE_MAX_FILES``：文件数上限（默认 500）。
- ``SKILL_FETCH_TIMEOUT_S``：经网关下载包的超时（默认 30 秒）。
- ``SKILL_REVOCATION_TTL_S``：吊销列表刷新周期（默认 30 秒，与节点心跳同量级）；
  ``SKILL_REVOCATION_MAX_STALE_S``：刷新连续失败时旧列表最多沿用多久，超过即失败关闭（默认 300 秒）。
- ``SKILL_READ_MAX_BYTES``：``read_skill_file`` / ``load_skill`` 单次返回的字节上限（默认 256 KiB）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _f(name: str, default: str) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class SkillSettings:
    skills_dir: str = field(default_factory=lambda: os.environ.get("SKILLS_DIR", "./runtime-skills"))
    trust_keys_file: str | None = field(default_factory=lambda: os.environ.get("SKILL_TRUST_KEYS_FILE") or None)
    package_max_bytes: int = field(default_factory=lambda: int(_f("SKILL_PACKAGE_MAX_BYTES", str(10 * 1024 * 1024))))
    package_max_files: int = field(default_factory=lambda: int(_f("SKILL_PACKAGE_MAX_FILES", "500")))
    fetch_timeout_s: float = field(default_factory=lambda: _f("SKILL_FETCH_TIMEOUT_S", "30"))
    revocation_ttl_s: float = field(default_factory=lambda: _f("SKILL_REVOCATION_TTL_S", "30"))
    revocation_max_stale_s: float = field(default_factory=lambda: _f("SKILL_REVOCATION_MAX_STALE_S", "300"))
    read_max_bytes: int = field(default_factory=lambda: int(_f("SKILL_READ_MAX_BYTES", str(256 * 1024))))


def load() -> SkillSettings:
    return SkillSettings()

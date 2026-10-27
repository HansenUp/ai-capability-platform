"""S9 安全用例的 pytest 夹具（辅助函数在 s9sec.py；用例以 ``from s9sec import ...`` 引用，避免与其他目录的 conftest 同名冲突）。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from s9sec import FIXTURE_SKILLS, Signer, new_key


@pytest.fixture
def signer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Signer]:
    """一把新的签名密钥 + 只信任它的 JWKS（经 SKILL_TRUST_KEYS_FILE 注入）。"""
    s = Signer(new_key(), tmp_path / "trust-keys.json")
    s.trust(s.key)
    monkeypatch.setenv("SKILL_TRUST_KEYS_FILE", str(s.jwks_path))
    yield s


@pytest.fixture
def ok_skill(tmp_path: Path, signer: Signer) -> tuple[Path, dict[str, str]]:
    """已签名的对照组技能目录与其 lock 条目。"""
    dest = tmp_path / "skills" / "ok-baseline" / "1.0.0"
    dest.parent.mkdir(parents=True)
    return dest, signer.signed_copy(FIXTURE_SKILLS / "ok-baseline", dest)

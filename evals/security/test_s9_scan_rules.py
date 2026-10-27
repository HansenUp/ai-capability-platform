"""五个坏包按规则 id 被拒（S9.3 静态扫描，网关 ``capability_gateway.scan``，WP-A）。

- 夹具自检（始终运行）：坏包确实具备各自的违规特征（含真实 bandit 命中 HIGH），对照组干净，防止夹具本身失效。
- 规则单元（``capability_gateway.scan`` 就绪后运行）：对每个夹具目录调用 ``scan.scan_dir(path)``，报告须不通过且含期望
  规则 id（``fixtures/expected.json``）；对照组无阻断项。``bad-capability`` 需要能力目录与租户，单元层不判定，
  由全栈用例（``test_s9_stack.py``，``S9_SECURITY_STACK=1``）经网关上传判定。
- 入口约定：``scan_dir(path) -> ScanReport``，报告有 ``passed`` 与 ``findings``（每项含 ``rule`` 或 ``rule_id``），亦接受同形 dict；
  模块已存在但缺少该入口时用例失败（契约未对齐），不跳过。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from s9sec import BAD_SKILLS, FIXTURE_SKILLS

UNIT_RULES = {k: v for k, v in BAD_SKILLS.items() if k != "bad-capability"}


def _get(obj: Any, *names: str) -> Any:
    for n in names:
        if isinstance(obj, dict) and n in obj:
            return obj[n]
        if hasattr(obj, n):
            return getattr(obj, n)
    return None


def rule_ids(report: Any) -> list[str]:
    return [str(_get(f, "rule", "rule_id", "id")) for f in (_get(report, "findings") or [])]


def hits(rule: str, ids: list[str]) -> bool:
    return any(i == rule or i.startswith((f"{rule}.", f"{rule}:", f"{rule}/")) for i in ids)


# ---- 夹具自检 ----
def test_fixture_layout() -> None:
    for name in [*BAD_SKILLS, "ok-baseline"]:
        d = FIXTURE_SKILLS / name
        text = (d / "SKILL.md").read_text(encoding="utf-8")
        assert text.startswith("---\n") and f"\nname: {name}\n" in text, name
        cases = (d / "evals" / "cases.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(cases) >= 10 and all(json.loads(c)["must_call"] for c in cases), name


def test_fixture_traits() -> None:
    blob = (FIXTURE_SKILLS / "bad-binary" / "references" / "blob.md").read_bytes()
    assert blob.startswith(b"\x7fELF") and b"\x00" in blob
    remote = (FIXTURE_SKILLS / "bad-remote-fetch" / "SKILL.md").read_text(encoding="utf-8")
    assert "curl " in remote and "pip install" in remote
    assert "urllib.request" in (FIXTURE_SKILLS / "bad-remote-fetch" / "scripts" / "fetch.py").read_text(encoding="utf-8")
    inj = (FIXTURE_SKILLS / "bad-injection" / "SKILL.md").read_text(encoding="utf-8")
    assert "​" in inj and "‮" in inj and "ignore all previous instructions" in inj
    assert max(len(line) for line in inj.splitlines()) >= 500  # 长 base64 载荷
    cap = (FIXTURE_SKILLS / "bad-capability" / "SKILL.md").read_text(encoding="utf-8")
    assert "`mail.send`" in cap and "`ticket.close_batch`" in cap


def test_bandit_fixture_has_high_finding() -> None:
    """bad-bandit 的脚本经 bandit（stdin 喂入，兼容透明加密）有 HIGH 级问题；且不含 remote_fetch 禁止的导入，只命中 bandit 规则。"""
    pytest.importorskip("bandit", reason="bandit 未安装（make setup 后 pip install bandit==1.9.4）")
    src = (FIXTURE_SKILLS / "bad-bandit" / "scripts" / "digest.py").read_bytes()
    for banned in (b"requests", b"urllib", b"socket", b"httpx", b"subprocess", b"eval(", b"exec(", b"__import__"):
        assert banned not in src
    proc = subprocess.run(  # noqa: S603 —— 固定参数调用本 venv 的 bandit
        [sys.executable, "-m", "bandit", "-f", "json", "-lll", "-"], input=src, capture_output=True, check=False
    )
    report = json.loads(proc.stdout)
    assert [r["test_id"] for r in report["results"] if r["issue_severity"] == "HIGH"], report


# ---- 规则单元 ----
@pytest.fixture(scope="module")
def scan() -> Any:
    mod = pytest.importorskip("capability_gateway.scan", reason="capability_gateway.scan（WP-A）尚未就绪")
    if not callable(getattr(mod, "scan_dir", None)):
        pytest.fail("capability_gateway.scan 缺少 scan_dir(path) 入口：与 evals/security 的约定未对齐（见本文件说明）")
    return mod


@pytest.mark.parametrize(("name", "rule"), sorted(UNIT_RULES.items()))
def test_bad_package_rejected_by_rule(scan: Any, name: str, rule: str) -> None:
    report = scan.scan_dir(FIXTURE_SKILLS / name)
    ids = rule_ids(report)
    assert _get(report, "passed") is False, (name, ids)
    assert hits(rule, ids), f"{name} 应命中 {rule}，实际 {ids}"


def test_baseline_passes(scan: Any) -> None:
    report = scan.scan_dir(FIXTURE_SKILLS / "ok-baseline")
    assert _get(report, "passed") is True, rule_ids(report)


def test_report_carries_rules_version(scan: Any) -> None:
    """规则升级后已上架版本须重扫：报告必须带 rules_version，且与模块的 RULES_VERSION 一致。"""
    report = scan.scan_dir(FIXTURE_SKILLS / "ok-baseline")
    assert _get(report, "rules_version") == scan.RULES_VERSION


def test_scan_does_not_follow_symlinks(scan: Any, tmp_path: Path) -> None:
    """源目录中的符号链接（指向包外文件）不被跟随打包，扫描判定不通过。"""
    import shutil

    outside = tmp_path / "outside-secret.md"
    outside.write_text("outside the package\n", encoding="utf-8")
    d = tmp_path / "ok-baseline"
    shutil.copytree(FIXTURE_SKILLS / "ok-baseline", d)
    (d / "references").mkdir()
    (d / "references" / "leak.md").symlink_to(outside)
    assert _get(scan.scan_dir(d), "passed") is False

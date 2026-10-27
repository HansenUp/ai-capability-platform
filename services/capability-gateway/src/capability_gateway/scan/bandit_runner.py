"""bandit 规则（bandit.*）：每个脚本经 stdin 交给 bandit（``-f json --ignore-nosec -``，脚本内的 ``# nosec`` 不生效）。

- bandit.high：bandit HIGH 级问题（阻断）；
- bandit.unavailable：bandit 不可用、超时或输出无法解析（失败关闭，阻断）；
- bandit.medium：bandit MEDIUM 级问题（只记录）。
"""

from __future__ import annotations

import json
import subprocess  # noqa: S404 —— 以固定参数调用本解释器的 bandit 模块
import sys
from typing import Any

from capability_gateway.scan.model import Finding, SkillPackage, finding

RULES = {
    "bandit.high": "bandit HIGH 级问题",
    "bandit.unavailable": "bandit 不可用（失败关闭）",
    "bandit.medium": "bandit MEDIUM 级问题",
}
TIMEOUT_S = 60.0
BANDIT_CMD = [sys.executable, "-m", "bandit", "-f", "json", "-q", "--ignore-nosec", "-"]


def run_bandit(src: str) -> dict[str, Any]:
    """运行 bandit；不可用 / 超时 / 输出不是 JSON 时抛 RuntimeError。"""
    try:
        r = subprocess.run(BANDIT_CMD, input=src.encode(), capture_output=True, timeout=TIMEOUT_S, check=False)  # noqa: S603
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"bandit failed: {type(e).__name__}") from None
    if r.returncode not in (0, 1):
        raise RuntimeError(f"bandit exited with {r.returncode}")
    try:
        out = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError("bandit produced no JSON report") from None
    if not isinstance(out, dict) or not isinstance(out.get("results"), list) or out.get("errors"):
        raise RuntimeError("bandit report has errors")
    return out


def check(pkg: SkillPackage) -> list[Finding]:
    out: list[Finding] = []
    for path, src in sorted(pkg.scripts().items()):
        try:
            report = run_bandit(src)
        except RuntimeError as e:
            out.append(finding("bandit.unavailable", path, str(e)))
            continue
        for r in report["results"]:
            sev = r.get("issue_severity")
            if sev in ("HIGH", "MEDIUM"):
                rule = "bandit.high" if sev == "HIGH" else "bandit.medium"
                msg = f"{r.get('test_id')}: {r.get('issue_text')}"
                out.append(finding(rule, path, msg, line=r.get("line_number"), severity="high" if sev == "HIGH" else "medium"))
    return out

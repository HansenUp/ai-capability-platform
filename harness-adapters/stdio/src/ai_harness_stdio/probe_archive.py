"""真实二进制探针报告存档（S6，D98）：能力等级以 ``make probe-real`` 的结果为准，并按 (harness, 版本) 存档。

    python -m ai_harness_stdio.probe_archive probe-report-real.json evals/reports/probe-real

每个 harness 写 ``<out_dir>/<harness>-<version>.json``：是否通过、各探针是否通过（只记失败条数，不记失败原文——
其中可能带 agent 日志与本机路径）、能力声明与能力越级项；不含二进制路径、探针工厂入口、密钥。版本取被测适配器
``capabilities().version``（AcpStdioHarness 即 ``AcpProfile.agent_version()``）。门禁未产出报告（不可用 / 运行异常）
或版本为空的 harness 不写存档，退出码 1。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def archive_record(name: str, record: dict[str, Any]) -> dict[str, Any] | None:
    """门禁报告里一个 harness 的记录 → 存档内容；没有探针报告时返回 None。"""
    report = record.get("report")
    if not isinstance(report, dict):
        return None
    caps = report.get("capabilities") or {}
    return {
        "harness": name,
        "version": str(caps.get("version") or ""),
        "passed": bool(record.get("passed")),
        "capabilities": caps,
        "capability_violations": list(record.get("capability_violations") or []),
        "probes": [
            {"name": r.get("name"), "passed": bool(r.get("passed")), "failures": len(r.get("failures") or [])}
            for r in report.get("results") or []
            if isinstance(r, dict)
        ],
        "archived_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def archive_path(out_dir: Path, harness: str, version: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z._+-]", "_", f"{harness}-{version}")
    return out_dir / f"{safe}.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ai_harness_stdio.probe_archive", description="Archive real-binary probe results by (harness, version)"
    )
    parser.add_argument("report", type=Path, help="probe gate JSON report (make probe-real)")
    parser.add_argument("out_dir", type=Path, help="archive directory, e.g. evals/reports/probe-real")
    args = parser.parse_args(argv)
    gate = json.loads(args.report.read_text(encoding="utf-8"))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    ok = True
    for name, record in (gate.get("harnesses") or {}).items():
        entry = archive_record(name, record)
        if entry is None or not entry["version"]:
            print(f"probe archive: {name}: no probe report or empty version, not archived", file=sys.stderr)
            ok = False
            continue
        path = archive_path(args.out_dir, name, entry["version"])
        path.write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"probe archive: {'PASS' if entry['passed'] else 'FAIL'} {name} {entry['version']} -> {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

"""上架门禁 CLI（S5 §1.3，决策 D86）：对 registry 可加载的每个 harness 跑五项契约探针。

    python -m ai_harness_probe.gate --report probe-report.json [--harness mock ...] [--known module:attr] [--timeout 20]
                                    [--allow-unavailable goose ...]

- 默认从 `runtime.harness.registry:KNOWN`（harness 名 → "模块:属性" 形式的探针工厂入口）读取门禁范围，逐个延迟导入工厂；
- 每个 harness：五项全过且 `capability_violations()` 为空才算通过；工厂无法导入或探针运行异常都算不通过（失败关闭）；
- JSON 报告写到 --report（含每项探针的 failures / notes 与能力声明），摘要打印到标准输出；任一不通过退出码为 1。
- 不可用（S6）：工厂在读取能力声明时抛 FileNotFoundError（goose / dsh 等外部二进制未安装）记为 `unavailable`。
  默认仍算不通过（失败关闭）；只有 --allow-unavailable 点名的 harness 不可用时不计入失败（CI 不下载第三方二进制，
  ACP 桥由 fake agent 另行门禁）。本机门禁（make probe-real）不加该参数，二进制缺失即失败。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .probes import run_probes

DEFAULT_KNOWN = "runtime.harness.registry:KNOWN"


def load_entry(entry: str) -> Any:
    """按 "模块:属性" 导入对象（属性可用点号逐级取）。格式不对抛 ValueError，导入失败照常抛出。"""
    module, sep, attr = entry.partition(":")
    if not sep or not module or not attr:
        raise ValueError(f"entry must look like 'module:attr', got {entry!r}")
    obj: Any = importlib.import_module(module)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


async def run_gate(
    known: Mapping[str, str], *, harnesses: Sequence[str] = (), timeout_s: float = 20, allow_unavailable: Sequence[str] = ()
) -> dict[str, Any]:
    """对 known 中（或 harnesses 指定的）每个 harness 运行五项探针，返回门禁报告（可直接 JSON 序列化）。
    allow_unavailable 中的 harness 不可用（record.unavailable）时不使整体失败，但其 record.passed 仍为 False。"""
    names = list(dict.fromkeys(harnesses)) or list(known)
    out: dict[str, Any] = {"passed": True, "harnesses": {}}
    for name in names:
        entry = known.get(name)
        record: dict[str, Any] = {"passed": False, "entry": entry}
        if entry is None:
            record["error"] = f"harness {name!r} is not in the gate scope (KNOWN: {sorted(known)})"
        else:
            try:
                factory = load_entry(entry)
                if not callable(factory):
                    raise TypeError(f"{entry} is not callable")
            except Exception as exc:
                record["error"] = f"cannot load probe factory {entry}: {type(exc).__name__}: {exc}"
            else:
                try:
                    report = await run_probes(factory, timeout_s=timeout_s)
                except FileNotFoundError as exc:
                    # run_probes 只在开头读取能力声明时让工厂异常外抛（各项探针的异常记为不通过），此处即被测 harness 不可用
                    record["unavailable"] = True
                    record["error"] = f"harness unavailable: {exc}"
                except Exception as exc:
                    record["error"] = f"probe run failed: {type(exc).__name__}: {exc}"
                else:
                    record.update(
                        passed=report.passed,
                        capability_violations=report.capability_violations(),
                        report=report.model_dump(mode="json"),
                    )
        out["harnesses"][name] = record
        tolerated = bool(record.get("unavailable")) and name in allow_unavailable
        out["passed"] = out["passed"] and (record["passed"] or tolerated)
    return out


def _summary(result: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    for name, rec in result["harnesses"].items():
        lines.append(f"{'PASS' if rec['passed'] else 'UNAVAILABLE' if rec.get('unavailable') else 'FAIL'} {name}")
        if rec.get("error"):
            lines.append(f"    error: {rec['error']}")
        for v in rec.get("capability_violations", []):
            lines.append(f"    capability: {v}")
        for r in rec.get("report", {}).get("results", []):
            for f in r["failures"]:
                lines.append(f"    {r['name']}: {f}")
    lines.append("probe gate: " + ("PASSED" if result["passed"] else "FAILED"))
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ai_harness_probe.gate", description="Harness contract probe gate")
    parser.add_argument("--report", type=Path, help="write the JSON report to this path")
    parser.add_argument(
        "--known", default=DEFAULT_KNOWN, help=f"module:attr of the harness → probe factory mapping (default {DEFAULT_KNOWN})"
    )
    parser.add_argument("--harness", action="append", default=[], help="only gate this harness (repeatable)")
    parser.add_argument("--timeout", type=float, default=20, help="per-probe timeout in seconds")
    parser.add_argument(
        "--allow-unavailable",
        action="append",
        default=[],
        metavar="HARNESS",
        help="do not fail the gate when this harness is unavailable (binary not installed); repeatable",
    )
    args = parser.parse_args(argv)
    known = load_entry(args.known)
    result = asyncio.run(run_gate(known, harnesses=args.harness, timeout_s=args.timeout, allow_unavailable=args.allow_unavailable))
    if args.report is not None:
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("\n".join(_summary(result)))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

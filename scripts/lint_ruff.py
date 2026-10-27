#!/usr/bin/env python3
"""本机 ruff 包装：绕过透明加密（DLP）导致的 E902。

部分开发机启用了透明加密，ruff（原生二进制）直接读文件会得到密文并报
`E902 stream did not contain valid UTF-8`，而 python 与 git 读到的是明文。
本脚本用 python 读出文件内容，经 stdin 交给 ruff 逐文件检查，结果与 CI 上
直接运行 `ruff check .` 一致。无 DLP 的机器也可以直接用，只是略慢。

用法：
    python scripts/lint_ruff.py            # 等价于 ruff check .
    python scripts/lint_ruff.py --format   # 等价于 ruff format --check .
    python scripts/lint_ruff.py --fix      # 原地修复（结果经 python 写回）
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUFF = ROOT / ".venv" / "bin" / "ruff"
GIT = shutil.which("git") or "/usr/bin/git"  # 绝对路径，避免 S607


def python_files() -> list[Path]:
    """列出受 git 管理（含未跟踪、排除 .gitignore）的 .py 文件。"""
    out = subprocess.run(  # noqa: S603 —— 固定参数的 git 调用，不含外部输入
        [GIT, "ls-files", "-co", "--exclude-standard", "--", "*.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [ROOT / line for line in out.splitlines() if line]


def run_one(path: Path, mode: str) -> tuple[int, str]:
    """对单个文件经 stdin 运行 ruff，返回（退出码，输出）。"""
    data = path.read_bytes()
    rel = str(path.relative_to(ROOT))
    if mode == "format":
        cmd = [str(RUFF), "format", "--check", "--force-exclude", "--stdin-filename", rel, "-"]
    elif mode == "fix":
        cmd = [str(RUFF), "check", "--fix", "--force-exclude", "--stdin-filename", rel, "-"]
    else:
        cmd = [str(RUFF), "check", "--force-exclude", "--stdin-filename", rel, "-"]
    proc = subprocess.run(cmd, cwd=ROOT, input=data, capture_output=True)  # noqa: S603 —— 可执行文件为仓库内 .venv 的 ruff
    if mode == "fix":
        # --fix 模式下 ruff 把修复后的源码写到 stdout，诊断在 stderr
        if proc.stdout and proc.stdout != data:
            path.write_bytes(proc.stdout)
        return proc.returncode, proc.stderr.decode(errors="replace")
    text = (proc.stdout + proc.stderr).decode(errors="replace")
    # 单文件检查无问题时 ruff 会输出 "All checks passed!"，汇总时过滤掉
    text = "\n".join(line for line in text.splitlines() if line and "All checks passed" not in line)
    return proc.returncode, text


def main() -> int:
    mode = "check"
    if "--format" in sys.argv:
        mode = "format"
    elif "--fix" in sys.argv:
        mode = "fix"
    files = python_files()
    worst = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for code, text in pool.map(lambda p: run_one(p, mode), files):
            if text.strip():
                print(text)
            worst = max(worst, code)
    label = {"check": "ruff check", "format": "ruff format --check", "fix": "ruff check --fix"}[mode]
    print(f"{label}: {len(files)} 个文件，{'通过' if worst == 0 else '存在问题'}")
    return worst


if __name__ == "__main__":
    sys.exit(main())

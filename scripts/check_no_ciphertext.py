#!/usr/bin/env python3
"""提交前检查：暂存区中不得出现透明加密（DLP）密文。

本机透明加密下，git 通常读到明文，但个别文件会以密文（以 `\\x18DRM` 开头、含
"This is a cipher text" 字样）进入暂存区，一旦提交就会把加密内容写进仓库。
本脚本逐个检查暂存的新增/修改文件；发现密文时，用 python 读出的明文重新写入暂存区
（`git hash-object -w` + `git update-index`），并以非零退出码中止本次提交，
请检查后重新执行 `git commit`。

用法：
    python3 scripts/check_no_ciphertext.py          # 检查并修复暂存区，发现密文时返回 1
    python3 scripts/check_no_ciphertext.py --check  # 只检查不修复
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GIT = shutil.which("git") or "/usr/bin/git"  # 绝对路径，避免 S607
MAGIC = b"\x18DRM"


def _git(*args: str, data: bytes | None = None) -> bytes:
    """执行固定参数的 git 子命令，返回标准输出。"""
    return subprocess.run([GIT, *args], cwd=ROOT, input=data, capture_output=True, check=True).stdout  # noqa: S603


def is_cipher(blob: bytes) -> bool:
    """判断内容是否为透明加密密文。"""
    return blob.startswith(MAGIC) or b"This is a cipher text" in blob[:200]


def main() -> int:
    fix = "--check" not in sys.argv
    staged = _git("diff", "--cached", "--name-only", "--diff-filter=AM", "-z").decode().split("\0")
    bad = [f for f in staged if f and is_cipher(_git("show", f":{f}"))]
    for f in bad:
        plain = (ROOT / f).read_bytes()
        if is_cipher(plain):
            print(f"密文且无法读到明文：{f}")
            continue
        if fix:
            sha = _git("hash-object", "-w", "--stdin", data=plain).decode().strip()
            mode = "100755" if (ROOT / f).stat().st_mode & 0o111 else "100644"
            _git("update-index", "--cacheinfo", f"{mode},{sha},{f}")
            print(f"已用明文重新暂存：{f}")
        else:
            print(f"暂存区为密文：{f}")
    if bad:
        print("暂存区曾含透明加密密文，已处理；请检查后重新提交。" if fix else "暂存区含透明加密密文。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

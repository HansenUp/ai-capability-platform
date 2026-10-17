"""资源限制启动器（S6 §3，D100）：先 setrlimit，再 execv 目标命令（替代 preexec_fn——多线程 / asyncio 下不安全）。

    python -m ai_harness_stdio.limits [--nofile N] [--cpu SECONDS] [--nproc N] -- cmd [args...]

- --nofile：RLIMIT_NOFILE 软硬限都设为 N（不超过当前硬限）；
- --cpu：RLIMIT_CPU 软限 SECONDS（到点 SIGXCPU）、硬限 SECONDS+1（到点 SIGKILL），按进程累计、子进程继承；
- --nproc：RLIMIT_NPROC，仅 Linux 生效（按真实 UID 计数，需配合专用运行用户；macOS 上忽略）。
限制随 execv 带到目标进程并由其子孙继承。本模块只用标准库：WorkerPool 以文件路径直接运行它
（`python <path>/limits.py ...`），避免 `-m` 先导入包（pydantic 等）带来的约 0.2 s 冷启动开销。
"""

from __future__ import annotations

import argparse
import os
import resource
import sys

_CPU_HARD_GRACE_S = 1  # CPU 软限（SIGXCPU）到硬限（SIGKILL）之间的余量


def _setrlimit(res: int, soft: int, hard: int) -> None:
    """设置限制；不超过当前硬限（非特权进程不能抬高硬限）。"""
    _, cur_hard = resource.getrlimit(res)
    if cur_hard != resource.RLIM_INFINITY:
        soft, hard = min(soft, cur_hard), min(hard, cur_hard)
    resource.setrlimit(res, (soft, hard))


def apply_limits(*, nofile: int | None = None, cpu_s: int | None = None, nproc: int | None = None) -> None:
    """在当前进程上设置资源限制（None 表示不设置）。"""
    if nofile is not None:
        _setrlimit(resource.RLIMIT_NOFILE, nofile, nofile)
    if cpu_s is not None:
        _setrlimit(resource.RLIMIT_CPU, cpu_s, cpu_s + _CPU_HARD_GRACE_S)
    if nproc is not None and sys.platform.startswith("linux"):
        _setrlimit(resource.RLIMIT_NPROC, nproc, nproc)


def launcher_argv(cmd: list[str], *, nofile: int | None = None, cpu_s: int | None = None, nproc: int | None = None) -> list[str]:
    """包上启动器后的 argv；三项都为 None 时原样返回 cmd（不多一次解释器启动）。"""
    flags: list[str] = []
    for flag, value in (("--nofile", nofile), ("--cpu", cpu_s), ("--nproc", nproc)):
        if value is not None:
            flags += [flag, str(value)]
    if not flags:
        return list(cmd)
    return [sys.executable, os.path.abspath(__file__), *flags, "--", *cmd]


def main(argv: list[str] | None = None) -> None:
    args_in = list(sys.argv[1:] if argv is None else argv)
    if "--" not in args_in:
        sys.stderr.write("usage: python -m ai_harness_stdio.limits [--nofile N] [--cpu S] [--nproc N] -- cmd [args...]\n")
        raise SystemExit(2)
    split = args_in.index("--")
    parser = argparse.ArgumentParser(prog="python -m ai_harness_stdio.limits", description="setrlimit, then execv cmd")
    parser.add_argument("--nofile", type=int)
    parser.add_argument("--cpu", type=int)
    parser.add_argument("--nproc", type=int)
    opts = parser.parse_args(args_in[:split])
    cmd = args_in[split + 1 :]
    if not cmd:
        parser.error("missing command after --")
    apply_limits(nofile=opts.nofile, cpu_s=opts.cpu, nproc=opts.nproc)
    try:
        os.execvp(cmd[0], cmd)  # noqa: S606 —— 启动器的职责就是 exec 平台配置的 harness 命令（argv，无 shell）
    except OSError as exc:
        sys.stderr.write(f"ai_harness_stdio.limits: cannot exec {cmd[0]!r}: {exc}\n")
        raise SystemExit(127) from exc


if __name__ == "__main__":
    main()

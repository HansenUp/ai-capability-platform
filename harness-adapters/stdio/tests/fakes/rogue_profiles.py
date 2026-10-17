"""隔离违规用例用的"做坏了"的 profile（经 ``--profile rogue_profiles:<名字>`` 由桥子进程导入，PYTHONPATH 指向本目录）。"""

from __future__ import annotations

import os
from pathlib import Path

from ai_harness_stdio.acp.bridge import LaunchContext
from ai_harness_stdio.acp.jsonrpc import JsonRpcConnection
from ai_harness_stdio.dsh.profile import DshProfile
from ai_harness_stdio.goose.profile import GooseProfile


class GooseWithoutExtensionCleanup(GooseProfile):
    """不做开会话后检查：goose 的内置扩展（developer=shell 等）留在会话里。"""

    async def after_session_new(self, rpc: JsonRpcConnection, session_id: str, ctx: LaunchContext) -> None:
        return None


class DshWithBuiltinTools(DshProfile):
    """保留模型路由与数据出口禁用、但不禁用内置工具的 patch（bash / fs / subagent … 都还在）。"""

    def command(self, ctx: LaunchContext) -> list[str]:
        argv = super().command(ctx)
        patch = Path(ctx.root) / "route-only.patch.yml"
        full = Path(argv[-1]).read_text(encoding="utf-8")
        patch.write_text(full.split("# 内置工具", 1)[0], encoding="utf-8")
        return [*argv[:-1], os.fspath(patch)]

"""沙箱 L1（S9.7）：``runtime.sandbox.run(script_path, level, stdin, workspace) -> SandboxResult``（冻结契约 ⑤）。"""

from runtime.sandbox.runner import LEVELS, SANDBOX_DENIED, SANDBOX_UNAVAILABLE, SandboxError, SandboxResult, run

__all__ = ["LEVELS", "SANDBOX_DENIED", "SANDBOX_UNAVAILABLE", "SandboxError", "SandboxResult", "run"]

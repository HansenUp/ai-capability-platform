"""goose 接入（S6 §1）：经 ACP 桥 ``python -m ai_harness_stdio.acp.bridge --profile goose``。"""

from .profile import GooseProfile, goose_binary

__all__ = ["GooseProfile", "goose_binary"]

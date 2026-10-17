"""dsh 接入（S6 §2）：经 ACP 桥 ``python -m ai_harness_stdio.acp.bridge --profile dsh``。"""

from .profile import PATCH, DshProfile, dsh_binary, node_binary

__all__ = ["PATCH", "DshProfile", "dsh_binary", "node_binary"]

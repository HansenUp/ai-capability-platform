"""可选辅助脚本，由 Harness 在沙箱内调用（只读）：统计从标准输入传入的工单数量。"""

import json
import sys

tickets = json.load(sys.stdin)  # 期望输入形如 {"tickets": [...]} 的 JSON
print(json.dumps({"count": len(tickets.get("tickets", []))}, ensure_ascii=False))

"""POC 用策略服务入口：原样使用 services/policy 的判定逻辑，只把内存判定日志改为有界。

policy_service.main.DECISION_LOG 是无界列表（开发实现），压测时每秒上千次判定会让内存持续增长、干扰 CPU/RSS 测量；
这里替换为只保留最近 BOUND 条的列表，判定接口与规则不变。
"""

from __future__ import annotations

from typing import Any

from policy_service import main as policy_main

BOUND = 1000


class BoundedLog(list[dict[str, Any]]):
    def append(self, item: dict[str, Any]) -> None:
        super().append(item)
        if len(self) > 2 * BOUND:
            del self[:-BOUND]


policy_main.DECISION_LOG = BoundedLog()
app = policy_main.app

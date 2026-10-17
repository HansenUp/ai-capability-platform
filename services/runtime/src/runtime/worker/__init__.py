"""Run 持久化执行的队列与 Worker（S7 WP-B）。

对外接口（供 API / WP-D）：

- ``enqueue(run_id, reason)``：把 Run 投递给 Worker（消息只含 run_id 与原因）；
- ``publish_cancel(run_id)``：Run 已 CAS 迁到 canceled 并提交后调用，跨节点中断正在执行它的 harness；
- ``lease.hold(run_id)``：API 内联执行（/v1/messages、/resume）持租约并进入 fence；
- 角色：``RUNTIME_ROLE=api|worker|all``（见 ``runtime.worker.settings``），``python -m runtime.worker`` 启动纯 Worker。
"""

from runtime.worker.control import publish_cancel
from runtime.worker.queue import enqueue, get_queue

__all__ = ["enqueue", "get_queue", "publish_cancel"]

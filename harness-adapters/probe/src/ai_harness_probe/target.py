"""被测对象（ProbeTarget）协议与工厂签名。

## received() 的规范化消息格式

`ProbeTarget.received()` 返回 `list[list[dict]]`：外层每个元素对应一次模型调用（顺序与调用顺序一致，
跨 run_turn、跨 restart() 连续），内层是这次调用模型实际看到的消息历史，规范化为与 runtime
`runtime.run.invariant.rebuild_messages` 输出逐项相同的结构（不含 SessionSpec.system_prompt，系统提示词不在历史里）：

- 初始/用户消息：原样，如 `{"role": "user", "content": "<文本>"}`；
- 助手文本（一次模型调用产出的全部文本拼接，非空时才有）：`{"role": "assistant", "content": "<文本>"}`；
- 工具调用：`{"role": "assistant", "tool_use": {"call_id": str, "name": str, "input": dict}}`，
  产生该调用的那次模型调用带思维链时另有 `"reasoning": "<思维链全文>"` 键；
- 工具结果：`{"role": "tool", "call_id": str, "content": <output_ref 或 output>}`（denied / error 的结果同样是这一形状，
  content 为拒绝原因或错误信息）；
- 上下文压缩摘要：`{"role": "system", "content": "[compacted summary] <摘要>"}`。

同一次模型调用既有文本又有工具调用时，先是助手文本消息、后是 tool_use 消息（与 runner 写历史的顺序一致）。
适配器的假模型负责把各自的原生格式（OpenAI messages、CLI 转录 JSONL、stdio 帧等）还原成上述结构；
runtime 侧测试锁定该格式与 `invariant.rebuild_messages` 一致，`PlatformSim.history` 也使用同一格式，
因此"模型收到的历史"可以与"平台从事件日志重建的历史"直接逐项比较。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, runtime_checkable

from ai_contract import HarnessAdapter

from .script import ModelScript, ScriptedModel

# 一次模型调用收到的规范化消息历史（格式见模块说明）
NormalizedMessages = list[dict[str, Any]]


@runtime_checkable
class ProbeTarget(Protocol):
    """接入探针的被测适配器：适配器实例 + 其背后按 ModelScript 应答的假模型的观测接口。"""

    @property
    def adapter(self) -> HarnessAdapter:
        """当前的适配器实例；restart() 之后返回新实例。"""
        ...

    def received(self) -> list[NormalizedMessages]:
        """模型每次被调用时实际收到的规范化消息历史，按调用顺序排列（格式见模块说明）。"""
        ...

    def leaks(self) -> list[str]:
        """当前残留的资源（未退出的子进程、未关闭的会话/连接、悬挂的后台任务等），每项一行可读描述；
        空列表表示无泄漏。中断探针在 interrupt 之后、上下文退出之前调用。"""
        ...

    async def restart(self) -> HarnessAdapter:
        """模拟平台进程重启：丢弃当前适配器实例及其全部内存状态（会话、子进程、缓存），构造一个全新实例并返回。
        假模型的调用计数与 received() 记录跨重启保留——重启后的模型调用继续消费脚本的后续应答。"""
        ...


# 被测适配器工厂：给定脚本，返回一个异步上下文管理器；进入时得到 ProbeTarget，退出时释放全部资源。
# 每个探针各自调用一次工厂，互不共享状态。
ProbeFactory = Callable[[ModelScript], AbstractAsyncContextManager[ProbeTarget]]


class BasicTarget:
    """通用 ProbeTarget 实现：适配器由 `make()` 构造，假模型状态是共享的 ScriptedModel。

    - restart()：先对当前实例调用 `discard`（释放其子进程/会话等，模拟进程退出），再 `make()` 一个新实例；
    - leaks()：对构造过的全部实例（含重启前的旧实例）汇总 `leak_check(adapter)`，旧实例残留同样算泄漏；
    - aclose()：工厂退出时对全部实例调用 `discard`。
    """

    def __init__(
        self,
        model: ScriptedModel,
        make: Callable[[], HarnessAdapter],
        *,
        leak_check: Callable[[HarnessAdapter], list[str]] | None = None,
        discard: Callable[[HarnessAdapter], Awaitable[None]] | None = None,
    ) -> None:
        self.model = model
        self._make = make
        self._leak_check = leak_check
        self._discard = discard
        self.instances: list[HarnessAdapter] = [make()]  # 构造过的全部适配器实例，最后一个为当前实例

    @property
    def adapter(self) -> HarnessAdapter:
        return self.instances[-1]

    def received(self) -> list[NormalizedMessages]:
        return self.model.calls

    def leaks(self) -> list[str]:
        if self._leak_check is None:
            return []
        return [leak for a in self.instances for leak in self._leak_check(a)]

    async def restart(self) -> HarnessAdapter:
        if self._discard is not None:
            await self._discard(self.adapter)
        self.instances.append(self._make())
        return self.adapter

    async def aclose(self) -> None:
        """释放全部实例的资源（工厂上下文退出时调用）。"""
        if self._discard is not None:
            for a in self.instances:
                await self._discard(a)

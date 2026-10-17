"""针对 stdio harness 的契约探针测试：事件流、Hooks、中断、基于重放的恢复（resume-by-replay）。"""

import sys

import pytest

from ai_contract import HarnessAdapter, HookSet, SessionHandle, SessionSpec, TextEvent, TurnInput
from ai_harness_stdio import StdioHarness

CMD = [sys.executable, "-m", "ai_harness_stdio.echo_harness"]  # 以子进程方式启动内置 echo harness
SPEC = SessionSpec(run_id="r1", agent_id="a", agent_version="1", model_alias="m", system_prompt="s", subject="human:t")


@pytest.mark.asyncio
async def test_event_stream_and_hooks():
    """验证事件流可正常产出，且 pre_model 钩子能收到预期 payload 并被调用。"""
    h = StdioHarness(CMD, name="echo")
    assert isinstance(h, HarnessAdapter)
    s = await h.open_session(SPEC)
    assert h.capabilities().transport == "stdio"
    seen = []

    async def pre_model(p):
        seen.append(p)
        return None

    evs = [e async for e in h.run_turn(s, TurnInput(messages=[{"role": "user", "content": "hi"}]), [], [], HookSet(pre_model=pre_model))]
    assert seen == [{"n": 1}]
    assert isinstance(evs[0], TextEvent) and evs[0].delta.endswith("hi") and evs[-1].type == "done"
    await h.interrupt(s, "r1")
    await h.close(s)


@pytest.mark.asyncio
async def test_hook_can_deny():
    """验证钩子返回 deny 时，回合以 error 事件（DENIED）结束，而不是继续执行。"""
    h = StdioHarness(CMD)
    s = await h.open_session(SPEC)

    async def deny(p):
        return {"deny": "redline"}

    evs = [e async for e in h.run_turn(s, TurnInput(messages=[{"role": "user", "content": "x"}]), [], [], HookSet(pre_model=deny))]
    assert evs[0].type == "error" and evs[0].code == "DENIED"
    await h.close(s)


@pytest.mark.asyncio
async def test_resume_by_replay():
    """验证 resume 时传入的历史事件会被子进程计数，并体现在后续回合的回显文本中。"""
    h = StdioHarness(CMD)
    s = await h.resume(SPEC, [TextEvent(delta="a"), TextEvent(delta="b")])
    evs = [e async for e in h.run_turn(s, TurnInput(messages=[{"role": "user", "content": "again"}]), [], [], HookSet())]
    assert evs[0].delta.startswith("echo(2 replayed)")
    await h.close(s)


@pytest.mark.asyncio
async def test_interrupt_closes_subprocess_and_is_idempotent():
    """interrupt 即会话结束：子进程退出、不残留；对已结束 / 未知会话重复中断无副作用；同一适配器多会话互不覆盖。"""
    h = StdioHarness(CMD)
    s1, s2 = await h.open_session(SPEC), await h.open_session(SPEC)
    assert s1.session_id != s2.session_id and len(h.running_pids()) == 2
    await h.interrupt(s1, "r1")
    assert len(h.running_pids()) == 1
    await h.interrupt(s1, "r1")
    await h.interrupt(SessionHandle(session_id="nope", harness="stdio"), "r1")
    evs = [e async for e in h.run_turn(s1, TurnInput(messages=[{"role": "user", "content": "x"}]), [], [], HookSet())]
    assert [e.type for e in evs] == ["error", "usage", "done"] and evs[0].code == "SESSION_NOT_FOUND"
    await h.close_all()
    assert h.running_pids() == []


@pytest.mark.asyncio
async def test_contract_probes_pass():
    """stdio 适配器 + echo 脚本模式通过五项契约探针。"""
    from ai_harness_probe import run_probes
    from ai_harness_stdio.testing import probe_factory

    report = await run_probes(probe_factory)
    assert report.passed, [(r.name, r.failures) for r in report.results]

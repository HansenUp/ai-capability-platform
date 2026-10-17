"""供 probe 测试使用的参考 stdio harness：原样回显输入、遵循 Hooks 往返协议、支持 resume 重放。

脚本模式（S5 契约探针）：`python -m ai_harness_stdio.echo_harness --script <script.json> --calls <calls.ndjson>`。
每个 turn 帧即一次"模型调用"：把收到的 messages（已是平台历史格式）追加一行到 calls 文件，按调用序号
（= calls 文件已有行数，跨子进程重启连续）取 script.json（ModelScript 的 JSON）中的应答，依次产出
thinking → text → 至多一个 tool_use → usage → done；`hang_after_text` 的应答在文本之后挂起，直到收到
interrupt 帧才以 done(cancelled) 收尾。stdin 由后台线程并发读取，因此挂起期间也能收到 interrupt / close 帧。
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
from pathlib import Path
from typing import Any

# 脚本模式回报的能力：轮次循环由平台驱动、工具由平台执行（与 StdioHarness 的默认声明一致）
SCRIPTED_CAPABILITIES = {
    "name": "echo",
    "version": "0.1",
    "transport": "stdio",
    "loop_owner": "platform",
    "tool_interception": "enforcing",
    "can_resume_from_events": True,
}


def _out(frame: dict[str, Any]) -> None:
    """向 stdout 写出一行 NDJSON 帧并立即 flush，保证对端能及时读到。"""
    sys.stdout.write(json.dumps(frame, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _event(event: dict[str, Any]) -> None:
    _out({"frame": "event", "event": event})


def _reader(frames: queue.Queue[dict[str, Any] | None]) -> None:
    """后台线程：逐行读取 stdin 帧放入队列；EOF 时放入 None。"""
    for line in sys.stdin:
        if line.strip():
            frames.put(json.loads(line))
    frames.put(None)


def _scripted_turn(
    f: dict[str, Any], frames: queue.Queue[dict[str, Any] | None], replies: list[dict[str, Any]], calls: Path, run_id: str
) -> bool:
    """脚本模式的一个 turn。返回 False 表示挂起期间收到 close 或 stdin 关闭，进程应退出。"""
    with calls.open("r+", encoding="utf-8") as fh:
        n = sum(1 for _ in fh)
        fh.write(json.dumps(f["messages"], ensure_ascii=False) + "\n")
    if n >= len(replies):
        _event({"type": "error", "code": "SCRIPT_EXHAUSTED", "message": f"model call #{n} is beyond the script ({len(replies)} replies)"})
        _event({"type": "usage", "model": "echo-scripted"})
        _event({"type": "done", "run_id": run_id, "stop_reason": "error"})
        return True
    reply = replies[n]
    for delta in reply.get("thinking", []):
        _event({"type": "thinking", "delta": delta})
    for delta in reply.get("text", []):
        _event({"type": "text", "delta": delta})
    if reply.get("hang_after_text"):
        while True:
            g = frames.get()
            if g is None or g["frame"] == "close":
                return False
            if g["frame"] == "interrupt":
                break
        _event({"type": "usage", "model": "echo-scripted"})
        _event({"type": "done", "run_id": run_id, "stop_reason": "cancelled"})
        return True
    calls_ = reply.get("tool_calls") or []
    if calls_:  # 每轮至多一个工具调用（与平台 runner 一致）
        c = calls_[0]
        _event({"type": "tool_use", "call_id": c["call_id"], "tool_name": c["name"], "input": c.get("input", {})})
    usage = reply.get("usage") or {}
    _event(
        {
            "type": "usage",
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_tokens": usage.get("cache_read_tokens", 0),
            "model": "echo-scripted",
        }
    )
    _event({"type": "done", "run_id": run_id, "stop_reason": "tool_use" if calls_ else "end_turn"})
    return True


def main(argv: list[str] | None = None) -> None:
    """从 stdin 逐帧读取并按 PROTOCOL.md 协议响应，直至收到 close 帧或 stdin 关闭。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", type=Path, help="ModelScript JSON（启用脚本模式）")
    parser.add_argument("--calls", type=Path, help="脚本模式下记录每次模型调用收到的消息（NDJSON，追加写）")
    args = parser.parse_args(argv)
    replies: list[dict[str, Any]] | None = None
    if args.script is not None:
        if args.calls is None:
            parser.error("--script requires --calls")
        replies = json.loads(args.script.read_text(encoding="utf-8")).get("replies", [])
        args.calls.touch()
    frames: queue.Queue[dict[str, Any] | None] = queue.Queue()
    threading.Thread(target=_reader, args=(frames,), daemon=True).start()
    replayed = 0  # resume 时收到的历史事件条数，用于在下一次回显中体现“已重放”
    run_id = "echo"
    while True:
        f = frames.get()
        if f is None:
            return
        kind = f["frame"]
        if kind == "open":
            # 建会话：回报固定的 session_id 与能力声明
            run_id = f.get("session", {}).get("run_id", run_id)
            caps = SCRIPTED_CAPABILITIES if replies is not None else {"name": "echo", "version": "0.1", "transport": "stdio"}
            _out({"frame": "opened", "session_id": "echo-1", "capabilities": caps})
        elif kind == "resume":
            # 恢复：记录历史事件条数，不重建真实上下文（历史由平台在 turn 帧中完整给出）
            replayed = len(f.get("events", []))
            _out({"frame": "resumed", "session_id": "echo-1"})
        elif kind == "turn" and replies is not None:
            if not _scripted_turn(f, frames, replies, args.calls, run_id):
                return
        elif kind == "turn":
            last: dict[str, Any] = next((m for m in reversed(f["messages"]) if m.get("role") == "user"), {})
            # 先发起一次 pre_model 钩子请求，等待引擎回灌 hook_result
            _out({"frame": "hook", "hook": "pre_model", "payload": {"n": len(f["messages"])}})
            hr = frames.get()
            if hr is None:
                return
            if hr.get("result") and hr["result"].get("deny"):
                # 钩子拒绝：以 error 事件结束本回合
                _event({"type": "error", "code": "DENIED", "message": hr["result"]["deny"]})
                _event({"type": "done", "run_id": f["session_id"], "stop_reason": "error"})
                continue
            # 正常路径：原样回显最后一条用户消息，并带上已重放的历史事件数
            _event({"type": "text", "delta": f"echo({replayed} replayed): {last.get('content', '')}"})
            _event({"type": "usage", "model": "echo"})
            _event({"type": "done", "run_id": f["session_id"], "stop_reason": "end_turn"})
        elif kind == "interrupt":
            pass  # 空闲时没有进行中的回合，接受该帧即可
        elif kind == "close":
            return


if __name__ == "__main__":
    main()

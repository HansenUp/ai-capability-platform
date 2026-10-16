"""供 probe 测试使用的参考 stdio harness：原样回显输入、遵循 Hooks 往返协议、支持 resume 重放。"""

from __future__ import annotations

import json
import sys


def _out(frame: dict) -> None:
    """向 stdout 写出一行 NDJSON 帧并立即 flush，保证对端能及时读到。"""
    sys.stdout.write(json.dumps(frame, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    """从 stdin 逐行读取帧并按 PROTOCOL.md 协议响应，直至收到 close 帧退出。"""
    replayed = 0  # resume 时收到的历史事件条数，用于在下一次回显中体现“已重放”
    for line in sys.stdin:
        f = json.loads(line)
        kind = f["frame"]
        if kind == "open":
            # 建会话：回报固定的 session_id 与能力声明
            _out({"frame": "opened", "session_id": "echo-1", "capabilities": {"name": "echo", "version": "0.1", "transport": "stdio"}})
        elif kind == "resume":
            # 恢复：记录历史事件条数，不重建真实上下文（仅用于验证重放式恢复的协议往返）
            replayed = len(f.get("events", []))
            _out({"frame": "resumed", "session_id": "echo-1"})
        elif kind == "turn":
            last = next((m for m in reversed(f["messages"]) if m.get("role") == "user"), {})
            # 先发起一次 pre_model 钩子请求，等待引擎回灌 hook_result
            _out({"frame": "hook", "hook": "pre_model", "payload": {"n": len(f["messages"])}})
            hr = json.loads(sys.stdin.readline())
            if hr.get("result") and hr["result"].get("deny"):
                # 钩子拒绝：以 error 事件结束本回合
                _out({"frame": "event", "event": {"type": "error", "code": "DENIED", "message": hr["result"]["deny"]}})
                _out({"frame": "event", "event": {"type": "done", "run_id": f["session_id"], "stop_reason": "error"}})
                continue
            # 正常路径：原样回显最后一条用户消息，并带上已重放的历史事件数
            _out({"frame": "event", "event": {"type": "text", "delta": f"echo({replayed} replayed): {last.get('content', '')}"}})
            _out({"frame": "event", "event": {"type": "usage", "model": "echo"}})
            _out({"frame": "event", "event": {"type": "done", "run_id": f["session_id"], "stop_reason": "end_turn"}})
        elif kind == "interrupt":
            pass  # echo harness 无需真实中断逻辑，仅需接受该帧不报错
        elif kind == "close":
            return


if __name__ == "__main__":
    main()

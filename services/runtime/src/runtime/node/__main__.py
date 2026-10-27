"""本机受管启动器；仅退出码 75 表示已排空且收到重启请求。"""

import argparse
import asyncio
import os
import signal
import sys


async def supervise(role: str, host: str, port: int) -> int:
    if os.environ.get("RUNTIME_ENV", "dev") == "prod":
        raise RuntimeError("the local managed launcher is not a production supervisor")
    if not os.environ.get("NODE_ID") or len(os.environ["NODE_ID"]) > 128:
        raise RuntimeError("an explicit stable NODE_ID is required")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for value in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(value, stop.set)
    environment = os.environ | {"RUNTIME_ROLE": role, "NODE_MANAGED_CHILD": "1"}
    if role == "worker":
        environment.setdefault("NODE_LABELS", "worker")
    while not stop.is_set():
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "runtime.node.process", "--role", role, "--host", host, "--port", str(port), env=environment
        )
        print(f"managed node child pid={child.pid}", flush=True)
        waiting = asyncio.create_task(child.wait())
        stopping = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait([waiting, stopping], return_when=asyncio.FIRST_COMPLETED)
            if stopping in done and not waiting.done():
                child.terminate()
            result = await waiting
        finally:
            stopping.cancel()
            await asyncio.gather(stopping, return_exceptions=True)
        if result != 75 or stop.is_set():
            return result
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("api", "worker", "all"), default="all")
    parser.add_argument("--host", choices=("127.0.0.1", "::1", "localhost"), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    sys.exit(asyncio.run(supervise(args.role, args.host, args.port)))


if __name__ == "__main__":
    main()

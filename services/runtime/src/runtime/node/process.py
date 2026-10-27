"""受管启动器的固定子进程入口，不从远程指令接收可执行文件或信号。"""

import argparse
import asyncio
import os
import sys


async def serve(role: str, host: str, port: int) -> int:
    if os.environ.get("NODE_MANAGED_CHILD") != "1" or os.environ.get("RUNTIME_ENV", "dev") == "prod":
        raise RuntimeError("managed local child only")
    if role == "worker":
        from runtime.worker.__main__ import main

        return await main(managed=True)
    import uvicorn

    from runtime.main import app

    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port))
    restarting = False

    def request_exit() -> None:
        nonlocal restarting
        restarting = True
        app.state.node_restart_requested = True
        server.should_exit = True

    app.state.node_request_exit = request_exit
    await server.serve()
    if restarting:
        return 75 if getattr(app.state, "node_shutdown_complete", False) else 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("api", "worker", "all"), required=True)
    parser.add_argument("--host", choices=("127.0.0.1", "::1", "localhost"), required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    sys.exit(asyncio.run(serve(args.role, args.host, args.port)))


if __name__ == "__main__":
    main()

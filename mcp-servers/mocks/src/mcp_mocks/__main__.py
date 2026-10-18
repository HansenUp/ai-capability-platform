"""启动 mock 连接器（只绑 127.0.0.1）：``python -m mcp_mocks knowledge|crm|mail [--port N]``。"""

from __future__ import annotations

import argparse

import uvicorn

from mcp_mocks import PORTS

HOST = "127.0.0.1"  # 固定只绑本机：mock 的 /_mock/* 审计接口不鉴权


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m mcp_mocks", description=__doc__)
    parser.add_argument("name", choices=sorted(PORTS))
    parser.add_argument("--port", type=int, default=None, help="端口，缺省 knowledge 8092 / crm 8093 / mail 8094")
    args = parser.parse_args()
    uvicorn.run(f"mcp_mocks.{args.name}:app", host=HOST, port=args.port or PORTS[args.name], log_level="info")


if __name__ == "__main__":
    main()

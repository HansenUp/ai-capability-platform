"""最小 TCP 转发（POC 容器内用）：`python relay.py 127.0.0.1:8180 host.docker.internal:8180`。

容器访问宿主机 Keycloak 时若直接用 host.docker.internal，签发的令牌 iss 会随 Host 变成 host.docker.internal，
与宿主机上 localhost:8180 签发的用户令牌不一致；在容器内把 localhost:8180 转发出去即可保持 Host 不变。
"""

from __future__ import annotations

import asyncio
import sys


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


async def main(listen: str, target: str) -> None:
    lhost, lport = listen.rsplit(":", 1)
    thost, tport = target.rsplit(":", 1)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            ur, uw = await asyncio.open_connection(thost, int(tport))
        except OSError:
            writer.close()
            return
        await asyncio.gather(_pipe(reader, uw), _pipe(ur, writer))

    server = await asyncio.start_server(handle, lhost, int(lport))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))

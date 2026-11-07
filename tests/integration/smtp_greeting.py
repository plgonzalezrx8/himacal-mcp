"""Correct GreenMail's InetAddress.toString() greeting; relay SMTP traffic unchanged."""

import asyncio
import re


def normalize_greeting(line: bytes) -> bytes:
    # shortcut: remove this fixture once GreenMail emits a valid SMTP greeting domain.
    return re.sub(
        rb"^(220 |250[- ])[^ \r\n]*/[^ \r\n]+(?= |\r\n)", rb"\1greenmail.test", line, count=1
    )


async def relay(client_reader, client_writer):
    server_writer = None
    tasks = []
    try:
        server_reader, server_writer = await asyncio.open_connection("greenmail", 3025, limit=8192)

        async def upstream():
            while data := await client_reader.read(65536):
                server_writer.write(data)
                await server_writer.drain()

        async def downstream():
            while line := await server_reader.readline():
                client_writer.write(normalize_greeting(line))
                await client_writer.drain()

        tasks = [asyncio.create_task(upstream()), asyncio.create_task(downstream())]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for writer in (client_writer, server_writer):
            if writer is not None:
                writer.close()
                await writer.wait_closed()


async def main():
    server = await asyncio.start_server(relay, "0.0.0.0", 3025)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())

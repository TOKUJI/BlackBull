#!/usr/bin/env python3
"""Count event-loop interactions per request — the CI-policeable hot-path metric.

Wall-clock says uvloop is worth ~2x on BlackBull but ~0 on a bare
``asyncio.Protocol`` echo.  That ratio is not about uvloop; it is about how
many times per request each server re-enters the loop.  Every ``call_soon`` /
``call_at`` / ``call_later`` / ``create_future`` is a Python object plus a
scheduling step on the stock loop and a Cython one under uvloop, so the count
*is* the exposure.

The reason this instrument exists rather than a req/s gate: it emits a
**count**.  Counts are deterministic, so this measures the servers rather than
the machine, and a shared CI runner can police it.  req/s cannot be.

The server runs on its own thread with its own loop; every client here is a
plain blocking socket, so nothing on the client side inflates the count.

Run under the **stock** loop.  uvloop does not route through
``asyncio.base_events.BaseEventLoop``, so the counters would read zero and the
budgets would pass vacuously; the harness refuses to run under it.

Usage::

    python bench/loop_touches.py            # print the ladder and the budgets
    python bench/loop_touches.py --check    # exit 1 if any budget is exceeded
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import os
import socket
import struct
import sys
import threading
import time
from asyncio.base_events import BaseEventLoop

# Per-request ceilings.  A phase may trade throughput for clarity; it may not
# trade the count upward.  Values sit just above the measured count so a real
# regression (a re-introduced timer, a future per request) trips them while
# connection-setup work amortised across the run does not.
#
# The reference rows are not budgeted — they are the ladder these numbers are
# read against.  ``bare asyncio.Protocol`` is the floor (nothing left for
# uvloop to accelerate); ``bare asyncio streams`` is what the streams layer
# alone costs, and therefore what BlackBull cannot get below without leaving
# ``StreamReader`` behind.
# Each sits ~7 % above what the protocol currently measures, which is enough
# headroom for connection-setup work amortised across the run and not enough
# to hide a re-armed timer or an extra future.
#
#   HTTP/1.1    2.06 = call_soon 1.04 + create_future 1.02 — all of it the
#               StreamReader handshake, i.e. identical to the bare
#               ``asyncio.start_server`` row.  BlackBull adds nothing.
#   HTTP/2      5.20 = call_soon 4.17 + create_future 1.02 — the per-stream
#               task the frame loop spawns into its TaskGroup dominates.
#   WebSocket   2.09 = call_soon 1.06 + create_future 1.02 — identical to
#               HTTP/1.1, i.e. the StreamReader handshake and nothing else.
#               It read 4.09 while a background reader task handed every
#               message through an asyncio.Queue; reading inline in the app's
#               own task removed exactly one future and one call_soon per
#               message.  Setting BB_WS_QUEUE_DEPTH > 0 restores the reader
#               task — and the 4.09 — in exchange for read-ahead.
BUDGETS = {
    'BlackBull HTTP/1.1': 2.20,
    'BlackBull HTTP/2': 5.40,
    'BlackBull WebSocket': 2.20,
}

COUNTS = {'call_soon': 0, 'call_at': 0, 'call_later': 0, 'create_future': 0}
_orig = {n: getattr(BaseEventLoop, n) for n in COUNTS}

WARMUP = 30
N = 200


def _patch() -> None:
    for name, fn in _orig.items():
        def make(name, fn):
            def wrapper(self, *a, **k):
                COUNTS[name] += 1
                return fn(self, *a, **k)
            return wrapper
        setattr(BaseEventLoop, name, make(name, fn))


# --------------------------------------------------------------------------
# Drivers — one per protocol, all on plain blocking sockets
# --------------------------------------------------------------------------

_H1_REQ = (b'GET /ping HTTP/1.1\r\nHost: x\r\n'
           b'User-Agent: probe\r\nAccept: */*\r\n\r\n')


def _connect(port: int) -> socket.socket:
    s = socket.create_connection(('127.0.0.1', port))
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return s


def _read_h1_response(s: socket.socket, buf: bytes = b'') -> bytes:
    while b'\r\n\r\n' not in buf:
        buf += s.recv(65536)
    head, _, rest = buf.partition(b'\r\n\r\n')
    length = 0
    for line in head.split(b'\r\n'):
        if line.lower().startswith(b'content-length:'):
            length = int(line.split(b':')[1])
    while len(rest) < length:
        rest += s.recv(65536)
    return rest[length:]


def drive_http1(port: int, n: int) -> None:
    s = _connect(port)
    try:
        leftover = b''
        for _ in range(n):
            s.sendall(_H1_REQ)
            leftover = _read_h1_response(s, leftover)
    finally:
        s.close()


_H2_PREFACE = b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'
_H2_END_STREAM = 0x01
_H2_END_HEADERS = 0x04


def _h2_frame(ftype: int, flags: int, stream_id: int, payload: bytes) -> bytes:
    return (len(payload).to_bytes(3, 'big') + bytes((ftype, flags))
            + struct.pack('>I', stream_id) + payload)


def _h2_literal(name_index: int, value: bytes) -> bytes:
    """HPACK literal-without-indexing, indexed name, no Huffman (RFC 7541 §6.2.2).

    Deliberately the dumbest legal encoding: it touches neither the dynamic
    table nor the Huffman decoder, so the count reflects the server's request
    pipeline rather than how well the client compressed.
    """
    return bytes((name_index, len(value))) + value


def _h2_read_frame(s: socket.socket, buf: bytearray):
    while len(buf) < 9:
        buf += s.recv(65536)
    length = int.from_bytes(buf[:3], 'big')
    while len(buf) < 9 + length:
        buf += s.recv(65536)
    ftype, flags = buf[3], buf[4]
    stream_id = struct.unpack('>I', buf[5:9])[0] & 0x7FFFFFFF
    del buf[:9 + length]
    return ftype, flags, stream_id


def drive_http2(port: int, n: int) -> None:
    s = _connect(port)
    try:
        s.sendall(_H2_PREFACE + _h2_frame(0x04, 0, 0, b''))
        buf = bytearray()
        headers = (b'\x82'                                  # :method: GET
                   + b'\x86'                                # :scheme: http
                   + _h2_literal(0x04, b'/ping')            # :path
                   + _h2_literal(0x01, b'127.0.0.1'))       # :authority
        stream_id = 1
        for _ in range(n):
            s.sendall(_h2_frame(0x01, _H2_END_STREAM | _H2_END_HEADERS,
                                stream_id, headers))
            while True:
                ftype, flags, sid = _h2_read_frame(s, buf)
                if ftype == 0x04 and not flags & 0x01:      # SETTINGS
                    s.sendall(_h2_frame(0x04, 0x01, 0, b''))  # ACK
                elif ftype == 0x06 and not flags & 0x01:    # PING
                    continue
                if sid == stream_id and flags & _H2_END_STREAM:
                    break
            stream_id += 2
    finally:
        s.close()


def drive_websocket(port: int, n: int) -> None:
    s = _connect(port)
    try:
        key = base64.b64encode(os.urandom(16))
        s.sendall(b'GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n'
                  b'Connection: Upgrade\r\nSec-WebSocket-Key: ' + key
                  + b'\r\nSec-WebSocket-Version: 13\r\n\r\n')
        buf = b''
        while b'\r\n\r\n' not in buf:
            buf += s.recv(65536)
        if b' 101 ' not in buf.split(b'\r\n', 1)[0]:
            raise RuntimeError(f'websocket handshake failed: {buf[:120]!r}')
        rest = bytearray(buf.split(b'\r\n\r\n', 1)[1])
        payload = b'ping'
        mask = b'\x00\x00\x00\x00'          # legal, and keeps the client cheap
        frame = (b'\x81' + bytes((0x80 | len(payload),)) + mask + payload)
        for _ in range(n):
            s.sendall(frame)
            # Server never masks (RFC 6455 §5.1), so the reply is
            # 2 bytes of header + payload for these short frames.
            while len(rest) < 2:
                rest += s.recv(65536)
            need = 2 + (rest[1] & 0x7F)
            while len(rest) < need:
                rest += s.recv(65536)
            del rest[:need]
    finally:
        s.close()


# --------------------------------------------------------------------------
# Servers under test
# --------------------------------------------------------------------------

async def _blackbull(port: int) -> None:
    from http import HTTPMethod

    from blackbull import BlackBull
    from blackbull.server.server import ASGIServer
    from blackbull.utils import Scheme

    app = BlackBull()

    @app.route(path='/ping')
    async def ping():
        return b'pong'

    @app.route(path='/ws', methods=[HTTPMethod.GET], scheme=Scheme.websocket)
    async def ws_echo(conn, receive, send):
        if (await receive()).get('type') != 'websocket.connect':
            return
        await send({'type': 'websocket.accept'})
        while True:
            event = await receive()
            if event.get('type') == 'websocket.disconnect':
                break
            if event.get('type') == 'websocket.receive':
                await send({'type': 'websocket.send',
                            'bytes': event.get('bytes') or b''})

    await ASGIServer(app).run(port=port)


def _serve(coro_factory, port: int) -> None:
    # The server's own exception is what a failure to bind almost always is,
    # and it dies with the daemon thread unless it is carried out by hand.
    failure: list[BaseException] = []

    def _run() -> None:
        try:
            asyncio.run(coro_factory(port))
        except BaseException as exc:       # noqa: BLE001 — re-raised below
            failure.append(exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    for _ in range(80):
        if failure:
            raise RuntimeError(f'server on port {port} failed to start') \
                from failure[0]
        try:
            socket.create_connection(('127.0.0.1', port), 0.3).close()
            return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError(f'server never bound port {port}')


def run_case(name: str, driver, port: int) -> float:
    """Drive *n* serialized keep-alive requests and return touches/req."""
    driver(port, WARMUP)          # first-request-only work, off the books
    for key in COUNTS:
        COUNTS[key] = 0
    driver(port, N)
    total = sum(COUNTS.values()) / N
    detail = '  '.join(f'{k}={COUNTS[k] / N:.2f}' for k in COUNTS)
    budget = BUDGETS.get(name)
    flag = ''
    if budget is not None:
        flag = '  OK' if total <= budget else f'  OVER BUDGET ({budget:.2f})'
    print(f'{name:<28} {total:6.2f} touches/req   {detail}{flag}')
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--check', action='store_true',
                    help='exit non-zero if a protocol exceeds its budget')
    ap.add_argument('--base-port', type=int, default=8951)
    args = ap.parse_args()

    if os.environ.get('BB_UVLOOP') == '1':
        print('error: run under the stock loop — uvloop does not route '
              'through BaseEventLoop, so every count would read zero',
              file=sys.stderr)
        return 2

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from streams_vs_protocol import _streams, _protocol

    _patch()
    port = args.base_port
    measured: dict[str, float] = {}

    _serve(_protocol, port)
    run_case('bare asyncio.Protocol', drive_http1, port)
    port += 1
    _serve(_streams, port)
    run_case('bare asyncio streams', drive_http1, port)
    port += 1

    _serve(_blackbull, port)
    for name, driver in (('BlackBull HTTP/1.1', drive_http1),
                         ('BlackBull HTTP/2', drive_http2),
                         ('BlackBull WebSocket', drive_websocket)):
        measured[name] = run_case(name, driver, port)

    over = {n: v for n, v in measured.items() if v > BUDGETS[n]}
    if args.check and over:
        print('\nloop-touch budget exceeded:', file=sys.stderr)
        for name, value in over.items():
            print(f'  {name}: {value:.2f} > {BUDGETS[name]:.2f}',
                  file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Targeted repro for the Autobahn close-handshake hang.

Mimics the Autobahn fuzzingclient's pattern that leaves a *client* stalled
for the suite's whole duration:

  1. TCP connect, HTTP upgrade handshake.
  2. Send a frame the server must fail the connection over (reserved
     non-control opcode 0x7 — Autobahn case 4.2.5).
  3. Server should answer with CLOSE 1002 and close TCP.
  4. Client replies with its own CLOSE (as Autobahn's client does), then
     waits for the server's TCP close.

Autobahn's client no-ops its 1 s ``killAfter`` once its WS state is
STATE_CLOSED, so if the server never closes TCP the *whole suite* hangs on
that one connection.  This script hammers the pattern and reports any
connection where the server fails to close TCP within the deadline.

Run against a live echo server:
    python bench/conformance/autobahn_app.py --port 9001
    python bench/conformance/repro_autobahn_close.py --n 500 --port 9001
"""
import argparse
import base64
import os
import socket
import struct
import sys
import time

HOST = '127.0.0.1'


def http_upgrade(port: int) -> socket.socket:
    s = socket.create_connection((HOST, port), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f'GET / HTTP/1.1\r\n'
        f'Host: {HOST}:{port}\r\n'
        f'Upgrade: websocket\r\n'
        f'Connection: Upgrade\r\n'
        f'Sec-WebSocket-Key: {key}\r\n'
        f'Sec-WebSocket-Version: 13\r\n'
        f'\r\n'
    ).encode()
    s.sendall(req)
    # Read the 101 response (up to a few KB).
    buf = b''
    s.settimeout(5)
    while b'\r\n\r\n' not in buf:
        chunk = s.recv(4096)
        if not chunk:
            raise RuntimeError('EOF during handshake')
        buf += chunk
    status = buf.split(b'\r\n', 1)[0]
    if b'101' not in status:
        raise RuntimeError(f'bad handshake: {status!r}')
    return s


def mask_frame(opcode: int, payload: bytes) -> bytes:
    mask = os.urandom(4)
    if len(payload) < 126:
        header = bytes([0x80 | opcode, 0x80 | len(payload)])
    elif len(payload) < 65536:
        header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack('>H', len(payload))
    else:
        header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack('>Q', len(payload))
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return header + mask + masked


def read_exact(s: socket.socket, n: int, timeout: float) -> bytes:
    s.settimeout(timeout)
    buf = b''
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise EOFError(f'EOF with {len(buf)}/{n} bytes')
        buf += chunk
    return buf


def run_once(port: int, wait_tcp_close: float) -> tuple[str, float]:
    """One 4.2.5-style connection.  Returns (verdict, elapsed)."""
    t0 = time.monotonic()
    s = http_upgrade(port)
    try:
        # Send reserved non-control opcode 0x7 with a small payload.
        s.sendall(mask_frame(0x7, b'Hello, world!'))
        # Read the server's CLOSE frame (expect 1002).  The server may also
        # just drop TCP, which Autobahn also accepts for 4.2.x.
        try:
            hdr = read_exact(s, 2, wait_tcp_close)
        except socket.timeout:
            return 'no-frame', time.monotonic() - t0
        except EOFError:
            return 'tcp-drop', time.monotonic() - t0
        opcode = hdr[0] & 0x0F
        length = hdr[1] & 0x7F
        if length == 126:
            length = struct.unpack('>H', read_exact(s, 2, wait_tcp_close))[0]
        elif length == 127:
            length = struct.unpack('>Q', read_exact(s, 8, wait_tcp_close))[0]
        payload = read_exact(s, length, wait_tcp_close) if length else b''
        if opcode != 0x8:
            return 'not-close', time.monotonic() - t0
        # Reply with our own CLOSE (what Autobahn's client does on receiving
        # the server's close frame).
        s.sendall(mask_frame(0x8, struct.pack('>H', 1000)))
        # Wait for the server to close TCP.  Autobahn's client waits here
        # forever once its WS state is STATE_CLOSED.
        s.settimeout(wait_tcp_close)
        while True:
            chunk = s.recv(4096)
            if not chunk:
                return 'clean', time.monotonic() - t0
    except socket.timeout:
        return 'tcp-stays-open', time.monotonic() - t0
    except OSError as e:
        return f'reset({e.errno})', time.monotonic() - t0
    finally:
        try:
            s.close()
        except OSError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--n', type=int, default=300)
    ap.add_argument('--port', type=int, default=9001)
    ap.add_argument('--wait', type=float, default=2.0,
                    help='seconds to wait for TCP close before declaring a stall')
    args = ap.parse_args()

    verdicts: dict[str, int] = {}
    stalls = 0
    t0 = time.monotonic()
    for i in range(1, args.n + 1):
        try:
            verdict, elapsed = run_once(args.port, args.wait)
        except Exception as e:  # handshake failure etc.
            verdict, elapsed = f'exc:{type(e).__name__}', 0.0
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
        if verdict in ('tcp-stays-open', 'no-frame', 'not-close'):
            stalls += 1
            print(f'[{i}/{args.n}] {verdict} after {elapsed:.3f}s  '
                  f'<<< STALL-LIKE')
        elif i % 50 == 0:
            print(f'[{i}/{args.n}] ...')
    dt = time.monotonic() - t0
    print(f'\n{args.n} connections in {dt:.1f}s')
    for k in sorted(verdicts):
        print(f'  {k}: {verdicts[k]}')
    print(f'stall-like verdicts: {stalls}')
    return 1 if stalls else 0


if __name__ == '__main__':
    sys.exit(main())

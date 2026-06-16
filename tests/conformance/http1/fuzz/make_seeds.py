#!/usr/bin/env python3
"""Generate atheris seed scenarios in :class:`Scenario.from_bytes` opcode form.

Sprint 18 epilogue — the pre-Sprint-17 ``l[1-5]_*.txt`` corpus contains
raw HTTP/1.1 wire bytes, but ``fuzz_http1.py``'s ``TestOneInput`` now
decodes bytes via :meth:`Scenario.from_bytes` (opcode-tagged
1 byte per step).  Without re-encoding, most existing seeds decode
to a single ``Abort()`` and contribute nothing to atheris coverage.

This script emits a small set of ``seed_*.bin`` files under
``corpus/`` (atheris's working directory) targeting code paths the
default Hypothesis sweep + ad-hoc mutation don't reliably reach.

Run::

    python tests/conformance/http1/fuzz/make_seeds.py
"""
from __future__ import annotations
import os
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Opcode encoder — must match blackbull.fault_injection.scenario_h1.Scenario.from_bytes
# ---------------------------------------------------------------------------

def encode_send(data: bytes, *, byte_interval_idx: int = 0) -> bytes:
    """One SEND step.  byte_interval_idx selects from
    _BYTE_INTERVAL_TABLE = (0.0, 0.0, 0.05, 0.2)."""
    n = len(data)
    if n > 0xFFFF:
        raise ValueError(f'payload too large for uint16 length: {n}')
    return bytes([0x00, n >> 8, n & 0xFF]) + data + bytes([byte_interval_idx & 0xFF])


def encode_read(timeout_idx: int = 3) -> bytes:
    """One READ step.  timeout_idx selects from
    _TIMEOUT_TABLE = (0.5, 1.0, 2.0, 5.0); 3 → 5.0 s."""
    return bytes([0x02, timeout_idx & 0xFF])


def encode_sleep(duration_idx: int = 0) -> bytes:
    """One SLEEP step.  duration_idx selects from
    _SLEEP_TABLE = (0.05, 0.25, 1.0, 2.0); 0 → 50 ms."""
    return bytes([0x01, duration_idx & 0xFF])


def encode_abort() -> bytes:
    return bytes([0x03])


def send_then_read(payload: bytes) -> bytes:
    """The most common shape — send a complete request, read the response."""
    return encode_send(payload) + encode_read(timeout_idx=2)  # 2.0 s read


# ---------------------------------------------------------------------------
# Targeted seeds — each one names the code path it aims to reach.
# ---------------------------------------------------------------------------

SEEDS: dict[str, bytes] = {
    # 100-Continue — server must answer with 100 first, then handler reply.
    # Exercises http1_actor.py:295-296 (the special-case Continue branch).
    'seed_expect_continue': send_then_read(
        b'POST /echo HTTP/1.1\r\n'
        b'Host: localhost\r\n'
        b'Content-Length: 5\r\n'
        b'Expect: 100-continue\r\n'
        b'\r\n'
        b'hello'
    ),

    # HEAD — response must echo a GET's headers but with no body.
    # Exercises http1_actor.py:319-321 head_mode synthesis.
    'seed_head_method': send_then_read(
        b'HEAD / HTTP/1.1\r\nHost: localhost\r\n\r\n'
    ),

    # OPTIONS * — asterisk-form request-target (RFC 9112 §3.2.4).
    # Stresses the request-target validator + urlparse path.
    'seed_options_asterisk': send_then_read(
        b'OPTIONS * HTTP/1.1\r\nHost: localhost\r\n\r\n'
    ),

    # CONNECT — authority-form target.  Unusual urlparse shape.
    'seed_connect_method': send_then_read(
        b'CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n'
    ),

    # TRACE — should be a method the router doesn't bind; with the
    # 405 → 200 fallback in fuzz_http1.py both sides answer 200.
    'seed_trace_method': send_then_read(
        b'TRACE / HTTP/1.1\r\nHost: localhost\r\n\r\n'
    ),

    # HTTP/1.0 — no required Host, default close semantics.  Exercises
    # _should_keep_alive's HTTP/1.0 branch.
    'seed_http10': send_then_read(
        b'GET / HTTP/1.0\r\n\r\n'
    ),

    # Connection: close — explicit close.  Exercises the
    # keep_alive_loop's exit on Connection: close.
    'seed_connection_close': send_then_read(
        b'GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n'
    ),

    # WebSocket upgrade — exercises http1_actor.py:291-293
    # (_handle_upgrade dispatch).  The handshake will fail without
    # Sec-WebSocket-Key etc. — that's fine, we just want the path hit.
    'seed_ws_upgrade': send_then_read(
        b'GET / HTTP/1.1\r\n'
        b'Host: localhost\r\n'
        b'Upgrade: websocket\r\n'
        b'Connection: Upgrade\r\n'
        b'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n'
        b'Sec-WebSocket-Version: 13\r\n'
        b'\r\n'
    ),

    # Chunked with trailers — recipient's _read_chunked has a
    # trailer-collection loop that the simple-chunked corpus
    # doesn't exercise.
    'seed_chunked_trailers': send_then_read(
        b'POST /echo HTTP/1.1\r\n'
        b'Host: localhost\r\n'
        b'Transfer-Encoding: chunked\r\n'
        b'Trailer: X-Trailer\r\n'
        b'\r\n'
        b'5\r\nhello\r\n'
        b'0\r\n'
        b'X-Trailer: signed\r\n'
        b'\r\n'
    ),

    # Pipelined two requests — second request lands in the keep-alive
    # loop after the first completes.  Exercises the buffer-carry
    # path in HTTP1Actor.run().
    'seed_pipelined_two': (
        encode_send(b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n')
        + encode_read(timeout_idx=2)
        + encode_send(b'GET /echo HTTP/1.1\r\nHost: localhost\r\n\r\n')
        + encode_read(timeout_idx=2)
    ),

    # TLS ClientHello first bytes — what a client sending TLS to a
    # plaintext port produces.  Server should reject as 400; both
    # nginx and BlackBull see junk before they see a request line.
    'seed_tls_handshake_bytes': send_then_read(
        b'\x16\x03\x01\x00\x80'  # TLS record header
        b'\x01\x00\x00\x7c\x03\x03'  # ClientHello + version
        + b'\x00' * 32  # random
        + b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n'  # tail that looks like HTTP
    ),

    # Query string with percent-encoded values — exercises urlparse
    # decoding + the router's path-param coercion (if any).
    'seed_query_string': send_then_read(
        b'GET /echo?a=1&b=hello%20world&c=%2F HTTP/1.1\r\n'
        b'Host: localhost\r\n\r\n'
    ),

    # Multiple identical Content-Length values — RFC 9112 §6.2 allows
    # this (collapses to one); exercises the value-collapse loop in
    # _validate_message_framing.
    'seed_cl_same_value_twice': send_then_read(
        b'POST /echo HTTP/1.1\r\n'
        b'Host: localhost\r\n'
        b'Content-Length: 5\r\n'
        b'Content-Length: 5\r\n'
        b'\r\n'
        b'hello'
    ),

    # Slowloris-shape header trickle — opcode-encoded variant of the
    # hand-written test_rfc9112_slowloris scenario.  Exercises the
    # header_timeout enforcement path under fuzz.
    'seed_slowloris_header': (
        # SEND with byte_interval index 3 = 0.2 s
        encode_send(
            b'POST /echo HTTP/1.1\r\nHost: localhost\r\n',
            byte_interval_idx=3,
        )
        + encode_read(timeout_idx=3)  # 5s read
    ),

    # Body trickle — header block intact, body trickled.
    'seed_slowloris_body': (
        encode_send(
            b'POST /echo HTTP/1.1\r\n'
            b'Host: localhost\r\n'
            b'Content-Length: 20\r\n\r\n',
        )
        + encode_send(b'x' * 20, byte_interval_idx=3)  # 0.2 s/byte
        + encode_read(timeout_idx=3)
    ),
}


def main() -> int:
    here = Path(__file__).resolve().parent
    out = here / 'corpus'
    out.mkdir(parents=True, exist_ok=True)
    for name, payload in SEEDS.items():
        path = out / f'{name}.bin'
        path.write_bytes(payload)
        print(f'wrote {path.relative_to(here.parent.parent.parent.parent)}'
              f' ({len(payload)} bytes)')
    print(f'\n{len(SEEDS)} seeds written under {out.relative_to(here.parent.parent.parent.parent)}/')
    return 0


if __name__ == '__main__':
    sys.exit(main())

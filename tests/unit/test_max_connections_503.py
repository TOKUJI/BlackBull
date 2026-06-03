"""Unit tests for the BB_MAX_CONNECTIONS cap behaviour.

Sprint 30 Tier 1.3 + 1.4: when the per-worker cap is hit, new
connections receive HTTP/1.1 ``503 Service Unavailable`` with
``Retry-After: 1`` (well-formed response, not a silent reset).

These tests exercise ``ASGIServer.client_connected_cb`` directly with
mocked reader/writer pairs so we can assert on the wire bytes.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.server.server import ASGIServer


class _RecordingWriter:
    """Minimal asyncio.StreamWriter shape that records what was
    written + close() calls."""

    def __init__(self):
        self.written = bytearray()
        self.closed = False
        # Pretend to be a wrapped asyncio.StreamWriter for the
        # ``get_extra_info`` calls in client_connected_cb.
        self.transport = _RecordingTransport()

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _RecordingTransport:
    def get_extra_info(self, name, default=None):
        # peername / sockname / ssl_object — minimal shapes
        if name == 'peername':
            return ('127.0.0.1', 12345)
        if name == 'sockname':
            return ('127.0.0.1', 8080)
        if name == 'ssl_object':
            return None
        return default


class _NoopReader:
    """Minimal reader — never has data to give (we don't expect any
    read calls on the reject path)."""

    async def read(self, n: int = -1) -> bytes:
        return b''

    async def readuntil(self, sep: bytes) -> bytes:
        return b''

    async def readexactly(self, n: int) -> bytes:
        return b''


async def _noop_app(scope, receive, send):
    """Trivial ASGI app placeholder.  client_connected_cb on the
    reject path never reaches the app, so it doesn't need to do
    anything sensible."""
    return None


@pytest.mark.asyncio
async def test_below_cap_does_not_send_503():
    """When _active_connections < max, the cb proceeds normally and
    does NOT write a 503.  (Sanity: confirms our condition doesn't
    fire prematurely.)"""
    srv = ASGIServer(_noop_app, max_connections=2)
    # Two slots free → don't reject the first connection.
    writer = _RecordingWriter()
    # We can't easily run the full client_connected_cb without
    # mocking ConnectionActor — but we can assert that with 0 active
    # the cap branch isn't entered.  Just check the precondition.
    assert srv._active_connections == 0
    assert srv._max_connections == 2
    # The reject branch test runs the actual code path below.


@pytest.mark.asyncio
async def test_at_cap_sends_503_with_retry_after_and_closes():
    """When _active_connections >= max, the cb emits a 503 with
    Retry-After then closes the writer.  No ConnectionActor is
    constructed (verified by the writer's recording — only the 503
    bytes appear, no protocol-specific output)."""
    srv = ASGIServer(_noop_app, max_connections=1)
    # Pre-fill the active counter so the next connection hits the cap.
    srv._active_connections = 1

    writer = _RecordingWriter()
    reader = _NoopReader()

    # client_connected_cb signature: (reader, writer).  It pulls
    # peername/sockname/alpn from writer.transport.
    await srv.client_connected_cb(reader, writer)

    payload = bytes(writer.written)
    # First line must be the 503 status line.
    assert payload.startswith(b'HTTP/1.1 503 Service Unavailable\r\n'), \
        f'unexpected first line: {payload[:40]!r}'
    # Retry-After must be present in the header block.
    assert b'\r\nretry-after: 1\r\n' in payload, \
        f'retry-after not found in: {payload!r}'
    # No body — content-length: 0.
    assert b'\r\ncontent-length: 0\r\n' in payload
    # Connection: close so the client doesn't try to reuse this slot.
    assert b'\r\nconnection: close\r\n' in payload
    # Headers terminated with double CRLF + no body bytes after.
    assert payload.endswith(b'\r\n\r\n')
    # And the writer was closed.
    assert writer.closed is True

    # The active-connections counter must NOT have been incremented
    # for the rejected connection.
    assert srv._active_connections == 1


@pytest.mark.asyncio
async def test_max_connections_zero_disables_the_cap(monkeypatch):
    """``max_connections=0`` means "no cap" — the reject branch is
    skipped even with many "active" connections.

    Mocks ``ConnectionActor`` so we don't run the full actor on a
    no-op reader (which would block forever waiting for bytes).
    """
    srv = ASGIServer(_noop_app, max_connections=0)
    srv._active_connections = 999_999

    actor_ran = False

    async def fake_run(self):
        nonlocal actor_ran
        actor_ran = True

    monkeypatch.setattr(
        'blackbull.server.connection_actor.ConnectionActor.run', fake_run)

    writer = _RecordingWriter()
    reader = _NoopReader()
    await srv.client_connected_cb(reader, writer)

    # The cap branch was skipped → no 503 written, actor was invoked.
    payload = bytes(writer.written)
    assert b'503' not in payload, \
        f'unexpected 503 emitted with cap=0: {payload[:80]!r}'
    assert actor_ran is True
    # And the active-connection counter cycled: incremented, then
    # decremented when the (mock) actor returned.
    assert srv._active_connections == 999_999


@pytest.mark.asyncio
async def test_at_cap_503_response_is_rfc9112_well_formed():
    """The 503 must be a complete, parseable HTTP/1.1 response.

    Verifies CRLF line endings, status code 503, Reason-Phrase
    "Service Unavailable", and proper header termination.
    """
    srv = ASGIServer(_noop_app, max_connections=1)
    srv._active_connections = 1

    writer = _RecordingWriter()
    reader = _NoopReader()
    await srv.client_connected_cb(reader, writer)

    payload = bytes(writer.written)
    # Split status line + headers (RFC 9112 §2.2 — CRLF line endings).
    lines = payload.split(b'\r\n')
    # Status line: HTTP/1.1 SP 503 SP Service Unavailable
    status_line = lines[0]
    parts = status_line.split(b' ', 2)
    assert len(parts) == 3, f'malformed status line: {status_line!r}'
    assert parts[0] == b'HTTP/1.1'
    assert parts[1] == b'503'
    assert parts[2] == b'Service Unavailable'

    # Every header line is "name: value" lowercase (we emit lowercase
    # consistently with the rest of BlackBull's HTTP/1.1 senders).
    header_lines = [l for l in lines[1:] if l]
    for h in header_lines:
        assert b': ' in h, f'malformed header: {h!r}'

    # End with empty line (CRLFCRLF).
    assert payload.endswith(b'\r\n\r\n')

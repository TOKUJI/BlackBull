"""Unit tests for the accept-pause hysteresis (Sprint 30 Tier 2).

When the active connection count crosses the HIGH watermark, the
server enters "shedding" mode: every new connection is immediately
closed (no protocol handling, no actor task spawned).  Shedding
persists until active drops to or below LOW.

Tests use the existing test_max_connections_503 fixtures
(_RecordingWriter, _NoopReader) so they exercise the real
``ASGIServer.client_connected_cb`` codepath.

This is Sprint 30's "prioritise close over open" mechanism: under
sustained burst-close pressure the loop's accept callbacks turn
into trivial close-and-return work, leaving the event loop free
to drain existing close work.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.server.server import ASGIServer


class _RecordingWriter:
    def __init__(self):
        self.written = bytearray()
        self.closed = False
        self.transport = _RecordingTransport()

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None: return None
    def close(self) -> None: self.closed = True
    async def wait_closed(self) -> None: return None


class _RecordingTransport:
    def get_extra_info(self, name, default=None):
        if name == 'peername':   return ('127.0.0.1', 12345)
        if name == 'sockname':   return ('127.0.0.1', 8080)
        if name == 'ssl_object': return None
        return default


class _NoopReader:
    async def read(self, n: int = -1) -> bytes: return b''
    async def readuntil(self, sep: bytes) -> bytes: return b''
    async def readexactly(self, n: int) -> bytes: return b''


async def _noop_app(scope, receive, send): return None


@pytest.mark.asyncio
async def test_accept_pause_disabled_when_high_watermark_zero(monkeypatch):
    """When ``accept_pause_high_watermark == 0`` the feature is fully
    disabled — even with high active counts, new connections proceed
    to the actor (unless ``max_connections`` kicks in)."""
    # ``max_connections=0`` (no hard cap) + ``accept_pause_high=0``
    # (no shedding) → no rejection regardless of active count.
    monkeypatch.setattr(
        'blackbull.server.connection_actor.ConnectionActor.run',
        lambda self: asyncio.sleep(0))

    srv = ASGIServer(_noop_app, max_connections=0,
                     accept_pause_high_watermark=0,
                     accept_pause_low_watermark=0)
    srv._active_connections = 999_999

    writer = _RecordingWriter()
    await srv.client_connected_cb(_NoopReader(), writer)

    # No rejection — the writer either gets nothing written (actor
    # handled it) or the actor mock ran to completion.
    assert writer.closed is False
    assert srv._shedding is False


@pytest.mark.asyncio
async def test_crossing_high_watermark_starts_shedding(monkeypatch):
    """When active >= HIGH, the next connection sheds (writer closed,
    no actor invoked) and the ``_shedding`` flag flips True."""
    monkeypatch.setattr(
        'blackbull.server.connection_actor.ConnectionActor.run',
        lambda self: pytest.fail('actor.run must not be called when shedding'))

    srv = ASGIServer(_noop_app, max_connections=20,
                     accept_pause_high_watermark=10,
                     accept_pause_low_watermark=5)
    srv._active_connections = 10        # at HIGH

    writer = _RecordingWriter()
    await srv.client_connected_cb(_NoopReader(), writer)

    assert writer.closed is True
    assert srv._shedding is True
    # Active counter unchanged (the rejected connection didn't enter
    # the active pool).
    assert srv._active_connections == 10


@pytest.mark.asyncio
async def test_shedding_persists_above_low_watermark(monkeypatch):
    """While in shedding mode, active counts BETWEEN low and high
    keep rejecting (hysteresis — don't flap)."""
    monkeypatch.setattr(
        'blackbull.server.connection_actor.ConnectionActor.run',
        lambda self: pytest.fail('actor.run must not be called when shedding'))

    srv = ASGIServer(_noop_app, max_connections=20,
                     accept_pause_high_watermark=10,
                     accept_pause_low_watermark=5)
    srv._shedding = True                 # already shedding
    srv._active_connections = 8          # above LOW, below HIGH

    writer = _RecordingWriter()
    await srv.client_connected_cb(_NoopReader(), writer)

    assert writer.closed is True
    assert srv._shedding is True         # stays shedding


@pytest.mark.asyncio
async def test_dropping_to_low_watermark_resumes(monkeypatch):
    """When active drops to LOW while shedding, the next connection
    is accepted normally and ``_shedding`` flips back to False."""
    actor_called = False

    async def fake_run(self):
        nonlocal actor_called
        actor_called = True

    monkeypatch.setattr(
        'blackbull.server.connection_actor.ConnectionActor.run', fake_run)

    srv = ASGIServer(_noop_app, max_connections=20,
                     accept_pause_high_watermark=10,
                     accept_pause_low_watermark=5)
    srv._shedding = True
    srv._active_connections = 5          # at LOW

    writer = _RecordingWriter()
    await srv.client_connected_cb(_NoopReader(), writer)

    assert actor_called is True
    assert srv._shedding is False        # resumed
    # Connection counter cycled: incremented then decremented in
    # client_connected_cb's try/finally.
    assert srv._active_connections == 5


@pytest.mark.asyncio
async def test_max_connections_takes_precedence_over_watermarks(monkeypatch):
    """Hard cap (max_connections) wins over soft shedding.  This
    matters because operators can set a cap without watermarks and
    expect the existing 503-on-hard-cap behaviour unchanged."""
    monkeypatch.setattr(
        'blackbull.server.connection_actor.ConnectionActor.run',
        lambda self: pytest.fail('actor.run must not be called at cap'))

    srv = ASGIServer(_noop_app, max_connections=20,
                     accept_pause_high_watermark=10,
                     accept_pause_low_watermark=5)
    srv._active_connections = 20         # at HARD cap

    writer = _RecordingWriter()
    await srv.client_connected_cb(_NoopReader(), writer)
    assert writer.closed is True
    # max_connections path doesn't set _shedding (it's the hard cap,
    # not the watermark feature).
    assert srv._shedding is False

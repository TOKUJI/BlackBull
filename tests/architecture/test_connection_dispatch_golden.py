"""Golden dispatch-order tests — Stage 0 regression oracle.

Pins the **current** observable protocol-selection decisions of
``ConnectionActor._dispatch()`` so the refactor in
``.claude/planning/proposals/decouple-connection-detection.md`` (peek-and-replay
detection + a unified ``serve(conn)``) can be verified to preserve them.

These assert *which protocol claims each wire shape* and the lifecycle/timeout
side effects — not how the bytes are read — so they stay meaningful as the
dispatch internals change.  A few tests deliberately pin behaviours the refactor
is meant to **change** (the HTTP-shaped 408 on timeout, symptom #3; the missing
``connection_closed`` for HTTP, symptom #5); those are marked and will be flipped
by the stage that fixes them.
"""
import asyncio

import pytest
from unittest.mock import AsyncMock

from blackbull.event_aggregator import EventAggregator
from blackbull.server.connection_actor import ConnectionActor
from blackbull.server.protocol_registry import (
    ProtocolDetector, ProtocolRegistry, RawBinding,
)
from blackbull.server.recipient import AbstractReader, IncompleteReadError
from blackbull.server.sender import AbstractWriter

pytestmark = pytest.mark.asyncio

_HTTP2_PREFACE = b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'
_HTTP1_REQUEST = b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n'


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _BufReader(AbstractReader):
    """Buffer-backed reader; raises IncompleteReadError when short."""

    def __init__(self, data: bytes = b'') -> None:
        self.buf = bytearray(data)

    async def read(self, n: int) -> bytes:
        chunk = bytes(self.buf[:n])
        del self.buf[:n]
        return chunk

    async def readuntil(self, sep: bytes) -> bytes:
        idx = self.buf.find(sep)
        if idx == -1:
            raise IncompleteReadError()
        end = idx + len(sep)
        chunk = bytes(self.buf[:end])
        del self.buf[:end]
        return chunk

    async def readexactly(self, n: int) -> bytes:
        if len(self.buf) < n:
            raise IncompleteReadError()
        chunk = bytes(self.buf[:n])
        del self.buf[:n]
        return chunk


class _TimeoutReader(AbstractReader):
    """Every detection read raises TimeoutError — simulates a fired deadline."""

    async def read(self, n: int) -> bytes:
        raise TimeoutError

    async def readuntil(self, sep: bytes) -> bytes:
        raise TimeoutError

    async def readexactly(self, n: int) -> bytes:
        raise TimeoutError


class _Writer(AbstractWriter):
    def __init__(self) -> None:
        self.written = bytearray()
        self.closed = False

    async def write(self, data: bytes) -> None:
        self.written += data

    async def close(self) -> None:
        self.closed = True


class _FirstByteDetector(ProtocolDetector):
    """Minimal shared-port detector: claims if the first byte equals *value*."""

    def __init__(self, value: int) -> None:
        self._value = value

    def detect(self, first_bytes: bytes, alpn: str | None) -> bool:
        return bool(first_bytes) and first_bytes[0] == self._value

    @property
    def protocol_name(self) -> str:
        return 'fake'


def _spy_serves(registry: ProtocolRegistry, calls: list) -> None:
    """Replace each binding's unified ``serve`` with a recorder (no real actor
    runs).

    Records the selected binding's ``name`` so a test can assert *which* protocol
    the dispatcher chose without depending on actor internals or on how the bytes
    are read.  ``claims`` / ``by_alpn`` selection logic is left intact.
    """
    for b in (*registry.cleartext_bindings, *registry.raw_bindings.values()):
        async def _serve(conn, _n=b.name):
            calls.append(_n)
        b.serve = _serve             # type: ignore[method-assign]


def _actor(reader, writer, *, registry=None, bound=None, alpn=None, aggregator=None):
    return ConnectionActor(
        reader, writer, AsyncMock(), aggregator,
        alpn=alpn, registry=registry, bound_binding=bound,
    )


# ---------------------------------------------------------------------------
# Protocol selection (must be preserved across the refactor)
# ---------------------------------------------------------------------------

async def test_http1_cleartext_selects_http1() -> None:
    reg, calls = ProtocolRegistry(), []
    _spy_serves(reg, calls)
    await _actor(_BufReader(_HTTP1_REQUEST), _Writer(), registry=reg).run()
    assert calls == ['http1']


async def test_http2_cleartext_preface_selects_http2() -> None:
    reg, calls = ProtocolRegistry(), []
    _spy_serves(reg, calls)
    await _actor(_BufReader(_HTTP2_PREFACE), _Writer(), registry=reg).run()
    assert calls == ['http2']


async def test_http2_alpn_selects_http2() -> None:
    reg, calls = ProtocolRegistry(), []
    _spy_serves(reg, calls)
    await _actor(_BufReader(_HTTP2_PREFACE), _Writer(), registry=reg, alpn='h2').run()
    assert calls == ['http2']


async def test_shared_port_detector_claims_before_http1() -> None:
    """A registered first-byte detector wins over the http1 fallback — and is
    recognised on its first byte, with no CRLF in sight (the fix for the
    shared-port MQTT readuntil hang)."""
    reg, calls = ProtocolRegistry(), []
    reg.register('fake', AsyncMock(), detector=_FirstByteDetector(0x10))
    _spy_serves(reg, calls)
    await _actor(_BufReader(b'\x10\x00'), _Writer(), registry=reg).run()
    assert calls == ['fake']


async def test_shared_port_prefix_is_replayed_to_serve() -> None:
    """The peeked discriminator bytes are replayed to the winning binding's
    serve — a shared-port frame with **no CRLF** (which the old
    readuntil(b'\\r\\n') detection would have hung on) reaches the handler whole.
    Locks the decouple-connection-detection bug-#4 fix."""
    reg = ProtocolRegistry()
    reg.register('fake', AsyncMock(), detector=_FirstByteDetector(0x10))
    seen: list[bytes] = []

    async def _serve(conn):
        # The handler reads the full stream back, including the peeked prefix.
        seen.append(await conn.reader.read(64))

    reg.raw_bindings['fake'].serve = _serve   # type: ignore[method-assign]
    # A short MQTT-shaped CONNECT, no CRLF anywhere.
    await _actor(_BufReader(b'\x10\x0d\x00\x04MQTT\x05\x02\x00\x00\x00'),
                 _Writer(), registry=reg).run()
    assert seen == [b'\x10\x0d\x00\x04MQTT\x05\x02\x00\x00\x00']


async def test_port_bound_skips_detection() -> None:
    """A port-bound binding serves immediately; no detection read happens."""
    calls = []
    bound = RawBinding('bound', AsyncMock())
    async def _serve(conn):
        calls.append('raw')
    bound.serve = _serve             # type: ignore[method-assign]
    reader = _BufReader(b'anything-unread')
    await _actor(reader, _Writer(), bound=bound).run()
    assert calls == ['raw']
    assert reader.buf == b'anything-unread'   # detection never consumed bytes


# ---------------------------------------------------------------------------
# Behaviours the refactor is meant to CHANGE — pinned here so the change is
# deliberate and visible, not silent.
# ---------------------------------------------------------------------------

async def test_detection_timeout_writes_http1_408() -> None:
    """A cleartext detection timeout writes an HTTP/1.1 408 — but the string now
    lives in Http1Binding.on_detect_timeout (the cleartext catch-all), not in
    ConnectionActor (decouple-connection-detection, Stage 3)."""
    writer = _Writer()
    await _actor(_TimeoutReader(), writer, registry=ProtocolRegistry()).run()
    assert writer.written.startswith(b'HTTP/1.1 408')


async def test_alpn_detection_timeout_is_silent() -> None:
    """An ALPN-committed h2 peer that never sends its preface is closed silently
    — no 408 (it would be garbage to an h2 client).  Stage 3 routes the timeout
    response through the committed binding's on_detect_timeout, so the protocol,
    not ConnectionActor, decides whether to write anything."""
    writer = _Writer()
    await _actor(_TimeoutReader(), writer,
                 registry=ProtocolRegistry(), alpn='h2').run()
    assert writer.written == b''


async def test_http_connection_emits_connection_closed() -> None:
    """Symptom #5 fixed (Stage 4): HTTP connections now fire BOTH
    connection_accepted and connection_closed — the lifecycle is symmetric with
    raw protocols.  The close event carries the served protocol name."""
    agg = AsyncMock(spec_set=EventAggregator)
    reg, calls = ProtocolRegistry(), []
    _spy_serves(reg, calls)
    await _actor(_BufReader(_HTTP1_REQUEST), _Writer(), registry=reg, aggregator=agg).run()
    agg.on_connection_accepted.assert_called_once()
    agg.on_connection_closed.assert_called_once()
    # peername, protocol, duration_ms — the protocol is the served binding.
    assert agg.on_connection_closed.call_args[0][1] == 'http1'


async def test_raw_connection_emits_connection_closed() -> None:
    """A raw (port-bound) connection fires connection_closed — now from
    ConnectionActor.run(), the single lifecycle owner for every protocol (the
    RawProtocolActor L2 wrapper was removed in Stage 4)."""
    agg = AsyncMock(spec_set=EventAggregator)

    async def handler(reader, writer, ctx):
        return

    bound = RawBinding('echo', handler)
    await _actor(_BufReader(), _Writer(), bound=bound, aggregator=agg).run()
    agg.on_connection_closed.assert_called_once()
    assert agg.on_connection_closed.call_args[0][1] == 'echo'

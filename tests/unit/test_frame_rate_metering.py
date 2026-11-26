"""Four floods that cost the peer nothing and the server a frame each.

`RateWindow`'s own contract is asserted in `test_rate_window.py`; this
file asserts that the four sites are wired to it and answer correctly.

Each shape is the *multiplication* mechanism: a small frame obliging a
small piece of work, unbounded in count.  No byte budget can see any of
them — a zero-length CONTINUATION is the clearest case, since it adds
literally nothing to `BB_HEADER_MAX_TOTAL` while still costing a parse
and a loop turn.

Every test sets the budget explicitly.  The false-positive test in each
group is the one that decides whether the shipped default is safe: a
meter that eventually trips on a well-behaved peer is worse than no
meter, because it fails a connection nobody can diagnose.
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest

from tests.harness import fed, peer, read_h2_frame
from hpack import Encoder

from blackbull.protocol.frame_types import (
    DataFrameFlags, ErrorCodes, FrameTypes, HeaderFrameFlags, PingFrameFlags,
)
from blackbull.client import HTTP2Client
from blackbull.server.http2_actor import HTTP2Actor
from blackbull.server.recipient import (WebSocketRecipient)
from blackbull.server.sender import AsyncioWriter

pytestmark = pytest.mark.asyncio

_LIMIT = 5


@pytest.fixture(autouse=True)
def _small_budget(monkeypatch):
    monkeypatch.setenv('BB_FRAME_RATE_LIMIT', str(_LIMIT))
    monkeypatch.setenv('BB_FRAME_RATE_WINDOW', '30.0')  # never rolls mid-test
    monkeypatch.setenv('BB_H2_IDLE_TIMEOUT', '0')       # not under test here
    from blackbull.env import reset_settings_cache
    reset_settings_cache()
    yield
    reset_settings_cache()


# ---------------------------------------------------------------------------
# HTTP/2
# ---------------------------------------------------------------------------

def _frame(type_byte: FrameTypes, flags: int = 0, stream_id: int = 0,
           payload: bytes = b'') -> bytes:
    return (len(payload).to_bytes(3, 'big') + type_byte + bytes([flags])
            + stream_id.to_bytes(4, 'big') + payload)


def _ping() -> bytes:
    return _frame(FrameTypes.PING, 0, 0, b'\x00' * 8)


def _settings() -> bytes:
    return _frame(FrameTypes.SETTINGS, 0, 0, b'')


def _headers(stream_id: int, *, end_headers: bool = True) -> bytes:
    fields = [(b':method', b'GET'), (b':path', b'/'),
              (b':scheme', b'https'), (b':authority', b'example.com')]
    flags = HeaderFrameFlags.END_HEADERS if end_headers else 0
    return _frame(FrameTypes.HEADERS, flags, stream_id, Encoder().encode(fields))


async def _drive(frames: list[bytes], app=None):
    """Run an actor over *frames*; return (actor, sent frames)."""
    writer = _Writer()

    async def _app(*a, **kw):
        pass

    actor = HTTP2Actor(None, AsyncioWriter(writer), app or _app, aggregator=None)
    queue = list(frames)
    sent: list = []
    real_send = actor.send_frame

    async def _recording(frame):
        sent.append(frame)
        return await real_send(frame)

    actor.send_frame = _recording

    async def _receive():
        return queue.pop(0) if queue else b''

    actor.receive = _receive
    await asyncio.wait_for(actor.run(), timeout=5.0)
    return actor, sent


class _Writer:
    def __init__(self):
        self.written = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written += data

    def writelines(self, parts) -> None:
        for p in parts:
            self.written += p

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


def _rst(stream_id: int) -> bytes:
    return _frame(FrameTypes.RST_STREAM, 0, stream_id, int(ErrorCodes.CANCEL).to_bytes(4, 'big'))


async def _working(scope, receive, send):
    await asyncio.Event().wait()


async def _finishing(scope, receive, send):
    await asyncio.sleep(0.2)


def _goaway_codes(sent) -> list[int]:
    return [f.error_code for f in sent
            if getattr(f, 'FrameType', None) and f.FrameType() == FrameTypes.GOAWAY]


class TestSettingsFlood:
    """CVE-2019-9515 — one ACK write per SETTINGS."""

    async def test_the_budgets_are_per_type(self):
        """A peer spending its PING allowance keeps its SETTINGS allowance.

        One shared counter would make two conformant behaviours compete
        for a single budget, and a peer has no way to predict that.
        """
        frames = [_settings()] * _LIMIT + [_ping()] * _LIMIT
        _, sent = await _drive(frames)
        assert ErrorCodes.ENHANCE_YOUR_CALM not in _goaway_codes(sent)


class TestEmptyFrameFlood:
    """CVE-2019-9518's shape — zero bytes, so a byte budget never sees it."""

    async def test_zero_length_data_frames_are_counted(self):
        frames = [_settings(), _headers(1)]
        frames += [_frame(FrameTypes.DATA, 0, 1, b'')] * (_LIMIT + 3)
        _, sent = await _drive(frames)
        assert ErrorCodes.ENHANCE_YOUR_CALM in _goaway_codes(sent)

    async def test_zero_length_continuation_frames_are_counted(self):
        frames = [_settings(), _headers(1, end_headers=False)]
        frames += [_frame(FrameTypes.CONTINUATION, 0, 1, b'')] * (_LIMIT + 3)
        _, sent = await _drive(frames)
        assert ErrorCodes.ENHANCE_YOUR_CALM in _goaway_codes(sent)

    async def test_an_empty_data_frame_that_ends_its_stream_is_not_counted(self):
        """A streamed request body ends with an empty END_STREAM DATA frame
        (Go net/http2 and Connect clients do), so each request carries one;
        the stream count, not this meter, bounds them."""
        frames = [_settings()]
        for sid in range(1, 2 * (_LIMIT + 3), 2):
            frames += [_headers(sid), _frame(FrameTypes.DATA, DataFrameFlags.END_STREAM, sid)]
        _, sent = await _drive(frames)
        assert ErrorCodes.ENHANCE_YOUR_CALM not in _goaway_codes(sent)

    async def test_a_frame_carrying_bytes_is_not_counted_as_empty(self):
        """The meter is for frames that cost work while paying nothing."""
        frames = [_settings(), _headers(1)]
        frames += [_frame(FrameTypes.DATA, 0, 1, b'x')] * (_LIMIT + 3)
        _, sent = await _drive(frames)
        assert ErrorCodes.ENHANCE_YOUR_CALM not in _goaway_codes(sent), (
            'DATA frames carrying real bytes were metered as empty frames; '
            'the body caps are what bound those')


class TestRapidReset:
    """CVE-2023-44487 — the cost is work a stream started and the peer abandoned."""

    async def test_resets_of_open_streams_are_counted(self):
        frames = [_settings()]
        for sid in range(1, 2 * (_LIMIT + 1), 2):
            frames += [_headers(sid), _rst(sid)]
        _, sent = await _drive(frames, _working)
        assert ErrorCodes.ENHANCE_YOUR_CALM in _goaway_codes(sent)

    async def test_resets_of_a_closed_stream_are_not_counted(self):
        """Go's client resets a stream whose response already ended; nothing is abandoned."""
        frames = [_settings(), _headers(1)] + [_rst(1)] * (_LIMIT + 3)
        _, sent = await _drive(frames, _working)
        assert ErrorCodes.ENHANCE_YOUR_CALM not in _goaway_codes(sent)

    async def test_cancelling_a_minority_of_streams_is_not_a_flood(self):
        """Deadlines expiring on a busy connection; most streams still complete."""
        opened = range(1, 2 * 3 * (_LIMIT + 1), 2)
        frames = [_settings(), *(_headers(sid) for sid in opened)]
        frames += [_rst(sid) for sid in opened[:_LIMIT + 1]]
        _, sent = await _drive(frames, _finishing)
        assert ErrorCodes.ENHANCE_YOUR_CALM not in _goaway_codes(sent)

    async def test_streams_that_were_not_reset_buy_at_most_as_many_resets(self):
        banked = 2 * _LIMIT
        frames = [_settings(), *(_headers(sid) for sid in range(1, 2 * banked, 2))]
        for sid in range(2 * banked + 1, 2 * (4 * banked), 2):
            frames += [_headers(sid), _rst(sid)]
        _, sent = await _drive(frames, _working)
        assert ErrorCodes.ENHANCE_YOUR_CALM in _goaway_codes(sent)

    async def test_received_and_emitted_resets_have_separate_budgets(self):
        """The server's own resets are not charged to the peer's allowance."""
        frames = [_settings()]
        for sid in range(1, 2 * _LIMIT, 2):
            frames += [_headers(sid), _rst(sid)]
        frames += [_frame(FrameTypes.PRIORITY, 0, sid, b'\x00' * 4)
                   for sid in range(101, 101 + 2 * _LIMIT, 2)]
        _, sent = await _drive(frames, _working)
        emitted = [f for f in sent if getattr(f, 'FrameType', None)
                   and f.FrameType() == FrameTypes.RST_STREAM]
        assert len(emitted) == _LIMIT
        assert ErrorCodes.ENHANCE_YOUR_CALM not in _goaway_codes(sent)


async def test_a_server_resetting_a_minority_of_requests_is_not_a_flood():
    """The client meters resets on its own requests by the same rule."""
    count = 3 * (_LIMIT + 1)
    goaways: list[int] = []
    answered = asyncio.Event()

    async def origin(reader, writer):
        await reader.readexactly(24)
        writer.write(_settings())
        opened = []
        while len(opened) < count:
            kind, _, sid, _ = await read_h2_frame(reader)
            if kind == FrameTypes.HEADERS.value[0]:
                opened.append(sid)
        writer.write(b''.join(_rst(sid) for sid in opened[:_LIMIT + 1]) + _ping())
        await writer.drain()
        with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError, TimeoutError):
            while not answered.is_set():
                kind, flags, _, payload = await read_h2_frame(reader)
                if kind == FrameTypes.GOAWAY.value[0]:
                    goaways.append(int.from_bytes(payload[4:8], 'big'))
                    answered.set()
                elif kind == FrameTypes.PING.value[0] and flags & PingFrameFlags.ACK:
                    answered.set()

    async with peer(origin) as port:
        async with HTTP2Client('127.0.0.1', port) as client:
            requests = [asyncio.ensure_future(client.request('GET', '/'))
                        for _ in range(count)]
            await asyncio.wait_for(answered.wait(), 3)
            for request in requests:
                request.cancel()
            await asyncio.gather(*requests, return_exceptions=True)
    assert goaways == []


class TestEmittedResets:
    """Audit G8 — the meter watched inbound resets only."""

    async def test_resets_the_server_emits_are_counted(self):
        """A peer that provokes our resets got the churn for free.

        Sending RST_STREAM on an idle stream makes the server answer with
        its own RST_STREAM; each one cycles a stream slot.  Counting only
        what the peer sends leaves that path unmetered — and Sprint 103
        added two more ways to provoke it (the body-size and body-rate
        refusals).
        """
        frames = [_settings()]
        # A malformed PRIORITY (RFC 9113 §6.3 requires exactly 5 octets) is a
        # *stream* error, so the server answers each one with its own
        # RST_STREAM.  The peer sends no resets at all — which is precisely
        # the blind spot.
        frames += [_frame(FrameTypes.PRIORITY, 0, sid, b'\x00' * 4)
                   for sid in range(101, 101 + (_LIMIT + 3) * 2, 2)]
        actor, sent = await _drive(frames)

        emitted = [f for f in sent
                   if getattr(f, 'FrameType', None)
                   and f.FrameType() == FrameTypes.RST_STREAM]
        assert emitted, 'the setup never provoked a server-side reset'
        assert ErrorCodes.ENHANCE_YOUR_CALM in _goaway_codes(sent), (
            f'{len(emitted)} server-emitted resets against a budget of '
            f'{_LIMIT} and the connection survived')

    async def test_a_few_emitted_resets_are_not_a_flood(self):
        frames = [_settings(),
                  _frame(FrameTypes.PRIORITY, 0, 101, b'\x00' * 4)]
        _, sent = await _drive(frames)
        assert ErrorCodes.ENHANCE_YOUR_CALM not in _goaway_codes(sent)


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

_MASK = b'\xde\xad\xbe\xef'


def _ws_frame(payload: bytes = b'', *, opcode: int = 0x9) -> bytes:
    """A masked client control frame (PING by default)."""
    masked = bytes(b ^ _MASK[i % 4] for i, b in enumerate(payload))
    return bytes([0x80 | opcode, 0x80 | len(payload)]) + _MASK + masked


def _ws_recipient(raw: bytes):
    writer = _Writer()
    recipient = WebSocketRecipient(
        fed(raw).reader, AsyncioWriter(writer), ws_queue_depth=0)
    return recipient, writer


class TestWebSocketControlFlood:
    """A PING costs a PONG write; nothing counted them."""

    async def test_within_the_budget_every_ping_earns_a_pong(self):
        recipient, writer = _ws_recipient(_ws_frame() * _LIMIT)
        assert await recipient() == {'type': 'websocket.connect'}

        # A PING emits nothing to the app, so drive until the reader runs
        # out and the recipient reports the disconnect.
        for _ in range(_LIMIT + 1):
            event = await recipient()
            if isinstance(event, dict) and event.get('type') == 'websocket.disconnect':
                break

        pongs = bytes(writer.written).count(b'\x8a')
        assert pongs == _LIMIT, (
            f'{pongs} PONGs for {_LIMIT} PINGs inside the budget')


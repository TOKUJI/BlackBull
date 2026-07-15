"""Regression tests for the Sprint 62 HTTP/2 flow-control + lifecycle batch.

Pins the bugs from the 2026-07-07 comprehensive audit:

- 1.2  HTTP/2 connection-level send window is never shared
- 1.8  GOAWAY early-return skips recipient disconnect signalling
- 1.9  ``_closed_streams`` grows without bound per connection
- 1.10 PRIORITY frame with unknown dependency crashes the connection
- 1.14 (#1) HEADERS on an OPEN stream respawns a second request (trailers)
- 1.14 (#3) 16 MiB allocation before frame-size validation
- 2.5  stream_window_size dict-of-one → int

Plus the Sprint 62 deferral landed later: consume-based inbound flow control
(proposals/consume-based-inbound-flow-control.md) — WINDOW_UPDATE credit for a
DATA frame is replayed when the app pops the event off the recipient queue,
not when the frame is enqueued, so a stalled handler closes the window and
back-pressures the peer instead of overflowing a 64-deep frame-count queue
into RST_STREAM(ENHANCE_YOUR_CALM).
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import (
    Data, DataFrameFlags, ErrorCodes, FrameTypes, HeaderFrameFlags,
    DEFAULT_INITIAL_WINDOW_SIZE, DEFAULT_MAX_FRAME_SIZE,
)
from blackbull.server.http2_actor import HTTP2Actor, _CLOSED_STREAMS_CAP
from blackbull.server.recipient import HTTP2Recipient
from blackbull.server.sender import (
    AsyncioWriter, ConnectionWindow, HTTP2Sender,
)
from blackbull.server.response import WindowUpdateResponder, PriorityResponder


def _make_h2_frame(type_byte, flags=0, stream_id=0, payload=b''):
    return (len(payload).to_bytes(3, 'big') + type_byte
            + bytes([flags]) + stream_id.to_bytes(4, 'big') + payload)


def _make_actor(app=None):
    if app is None:
        app = AsyncMock()
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.close = MagicMock()
    handler = HTTP2Actor(None, AsyncioWriter(writer), app, aggregator=None)
    handler.send_frame = AsyncMock()
    return handler


def _new_sender(factory, stream_id, conn_window):
    writer = MagicMock()
    writer.write = MagicMock()
    writer.drain = AsyncMock()
    return HTTP2Sender(AsyncioWriter(writer), factory, stream_id,
                       conn_window=conn_window)


# ---------------------------------------------------------------------------
# 1.2 — shared connection send window (+ 2.5 stream window is a scalar int)
# ---------------------------------------------------------------------------

def test_senders_share_one_connection_window():
    factory = FrameFactory()
    conn = ConnectionWindow(1000)
    a = _new_sender(factory, 1, conn)
    b = _new_sender(factory, 3, conn)
    # Both senders point at the same window object (bug 1.2 — not private copies).
    assert a._conn_window is conn and b._conn_window is conn
    # A debit through one sender is visible to the other immediately.
    a._conn_window.size -= 400
    assert b.connection_window_size == 600
    assert conn.size == 600


def test_connection_window_default_is_private_when_unshared():
    """A sender constructed without a shared window (the experimental client
    path) still gets its own ConnectionWindow — behaviour preserved."""
    factory = FrameFactory()
    s = HTTP2Sender(AsyncioWriter(MagicMock()), factory, 1)
    assert isinstance(s._conn_window, ConnectionWindow)
    assert s.connection_window_size == DEFAULT_INITIAL_WINDOW_SIZE


def test_stream_window_size_is_a_scalar_int():
    """Refactor 2.5 — the per-stream window is a plain int, not a dict-of-one."""
    factory = FrameFactory()
    s = _new_sender(factory, 1, ConnectionWindow())
    assert isinstance(s.stream_window_size, int)
    s.window_update(512)
    assert s.stream_window_size == DEFAULT_INITIAL_WINDOW_SIZE + 512


@pytest.mark.asyncio
async def test_connection_window_update_credits_shared_window_once():
    handler = _make_actor()
    conn = handler._conn_window
    start = conn.size
    frame = MagicMock()
    frame.stream_id = 0
    frame.length = 4
    frame.window_size = 1000
    # Two live senders sharing the connection window.
    handler._senders = {1: _new_sender(handler.factory, 1, conn),
                        3: _new_sender(handler.factory, 3, conn)}
    await WindowUpdateResponder(frame).respond(handler)
    # Credited once — not once per sender.
    assert conn.size == start + 1000


# ---------------------------------------------------------------------------
# 1.9 — bounded closed-stream record
# ---------------------------------------------------------------------------

def test_closed_streams_record_is_bounded():
    handler = _make_actor()
    for sid in range(1, (_CLOSED_STREAMS_CAP + 500) * 2, 2):  # odd ids
        handler._mark_closed(sid, via_rst=False)
    assert len(handler._closed_streams) <= _CLOSED_STREAMS_CAP
    # The high-water mark still recognises an evicted-but-closed id as closed.
    assert handler._closed_high_water >= _CLOSED_STREAMS_CAP


def test_closed_stream_watermark_survives_eviction():
    handler = _make_actor()
    handler._mark_closed(1, via_rst=True)   # will be evicted
    for sid in range(3, _CLOSED_STREAMS_CAP * 2 + 5, 2):
        handler._mark_closed(sid, via_rst=False)
    assert 1 not in handler._closed_streams          # evicted from exact cache
    assert 1 <= handler._closed_high_water           # still ≤ watermark → CLOSED


# ---------------------------------------------------------------------------
# 1.8 — the GOAWAY early-return path still signals recipients
# ---------------------------------------------------------------------------

class _FakeRecipient:
    """Minimal object satisfying the _StreamRecipient protocol."""
    def __init__(self):
        self.disconnected = False

    def put_disconnect(self):
        self.disconnected = True

    def put_DATAFrame(self, frame):
        return True


@pytest.mark.asyncio
async def test_goaway_early_return_signals_recipients():
    handler = _make_actor()
    recipient = _FakeRecipient()
    handler._recipients = {1: recipient}
    handler._goaway_sent = True  # a prior frame already triggered a conn error
    # A queued frame arrives after the error; the goaway early-return must
    # inject http.disconnect into blocked recipients (bug 1.8) rather than
    # returning and leaving a stream task waiting on receive() forever.
    handler.receive = AsyncMock(side_effect=[
        _make_h2_frame(FrameTypes.PING, 0, 0, b'\x00' * 8), None])
    import asyncio
    async with asyncio.TaskGroup() as tg:  # real TaskGroup; loop returns at once
        await handler._frame_loop(tg)
    assert recipient.disconnected is True


# ---------------------------------------------------------------------------
# 1.10 — PRIORITY with an unknown dependent stream must not crash
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_priority_unknown_dependency_does_not_crash():
    handler = _make_actor()
    frame = MagicMock()
    frame.stream_id = 7          # not in the tree
    frame.dependent_stream = 99  # also unknown
    frame.exclusion = False
    frame.weight = 16
    # Must not raise AttributeError (find_child(99) → None); the stream is
    # parented under the root instead (bug 1.10).
    await PriorityResponder(frame).respond(handler)
    assert handler.root_stream.find_child(7) is not None


# ---------------------------------------------------------------------------
# 1.14 (#3) — an over-sized frame is rejected without buffering its payload
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_receive_refuses_to_buffer_oversized_frame():
    """receive() detects an over-sized frame from its 9-byte header and returns
    without ever reading (buffering) the declared payload (bug 1.14 #3)."""
    from blackbull.server.recipient import AbstractReader
    declared = DEFAULT_MAX_FRAME_SIZE + 1_000_000
    header = (declared.to_bytes(3, 'big') + FrameTypes.DATA
              + bytes([0]) + (1).to_bytes(4, 'big'))

    class _HeaderOnlyReader(AbstractReader):
        def __init__(self):
            self.buf = bytearray(header)

        async def read(self, n):
            chunk = bytes(self.buf[:n]); del self.buf[:n]; return chunk

        async def readexactly(self, n):
            if len(self.buf) < n:
                # receive() must not attempt to read the (absent) payload.
                raise AssertionError('over-sized payload must not be buffered')
            chunk = bytes(self.buf[:n]); del self.buf[:n]; return chunk

    handler = _make_actor()
    handler._reader = _HeaderOnlyReader()
    data = await handler.receive()
    assert handler._oversize_frame_len == declared
    assert len(data) == 9, 'only the frame header should be returned'


@pytest.mark.asyncio
async def test_oversized_frame_len_drives_frame_size_error():
    """When receive() flags an over-sized frame, the frame loop raises a
    connection FRAME_SIZE_ERROR (bug 1.14 #3)."""
    handler = _make_actor()
    settings = _make_h2_frame(FrameTypes.SETTINGS, 0, 0, b'')
    header = ((DEFAULT_MAX_FRAME_SIZE + 1_000_000).to_bytes(3, 'big')
              + FrameTypes.DATA + bytes([0]) + (1).to_bytes(4, 'big'))

    async def fake_receive():
        # First the SETTINGS handshake, then the over-sized header with the
        # flag set exactly as the real receive() would.
        if not getattr(fake_receive, '_sent_settings', False):
            fake_receive._sent_settings = True
            return settings
        if not getattr(fake_receive, '_sent_oversize', False):
            fake_receive._sent_oversize = True
            handler._oversize_frame_len = DEFAULT_MAX_FRAME_SIZE + 1_000_000
            return header
        return b''

    handler.receive = fake_receive
    await handler.run()
    goaway = [c.args[0] for c in handler.send_frame.call_args_list
              if getattr(c.args[0], 'error_code', None) == ErrorCodes.FRAME_SIZE_ERROR]
    assert goaway, 'expected a FRAME_SIZE_ERROR for the over-sized frame'


# ---------------------------------------------------------------------------
# 1.14 (#1) — trailers on an open stream do not respawn a request
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_trailers_do_not_respawn_request():
    from hpack import Encoder
    calls = []

    async def app(scope, receive, send):
        calls.append(scope['path'])
        # Drain the body to completion (trailers deliver the clean EOS).
        while True:
            ev = await receive()
            if not ev.get('more_body', False):
                break
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok'})

    handler = _make_actor(app)
    enc = Encoder()
    settings = _make_h2_frame(FrameTypes.SETTINGS, 0, 0, b'')
    headers = _make_h2_frame(
        FrameTypes.HEADERS,
        HeaderFrameFlags.END_HEADERS,  # open, no END_STREAM (body/trailers follow)
        1,
        enc.encode([(b':method', b'POST'), (b':path', b'/x'), (b':scheme', b'https'), (b':authority', b'example.com')]))
    body = _make_h2_frame(FrameTypes.DATA, 0, 1, b'hello')
    trailers = _make_h2_frame(
        FrameTypes.HEADERS,
        HeaderFrameFlags.END_HEADERS | HeaderFrameFlags.END_STREAM,
        1,
        enc.encode([(b'x-trailer', b'v')]))
    handler.receive = AsyncMock(
        side_effect=[settings, headers, body, trailers, None])
    await handler.run()
    # The app was dispatched exactly once — the trailers HEADERS did not spawn
    # a second request over the live stream (bug 1.14).
    assert calls == ['/x']
    # No PROTOCOL_ERROR was raised for the (valid, END_STREAM-carrying) trailers.
    assert not [c.args[0] for c in handler.send_frame.call_args_list
                if getattr(c.args[0], 'error_code', None) == ErrorCodes.PROTOCOL_ERROR]


# ---------------------------------------------------------------------------
# Consume-based inbound flow control (the Sprint 62 deferral) — credit is
# replayed when the app pops the event, not when the DATA frame is enqueued.
# ---------------------------------------------------------------------------

def _data(payload: bytes, stream_id: int = 1, end_stream: bool = False) -> Data:
    flags = int(DataFrameFlags.END_STREAM) if end_stream else 0
    return Data(len(payload), FrameTypes.DATA, flags, stream_id, data=payload)


def _window_updates(handler):
    """(stream_id, increment) for every WINDOW_UPDATE the actor sent."""
    return [(f.stream_id, f.window_size)
            for f in (c.args[0] for c in handler.send_frame.call_args_list)
            if f.FrameType() == FrameTypes.WINDOW_UPDATE]


@pytest.mark.asyncio
async def test_recipient_credits_on_consume_not_on_enqueue():
    """No WINDOW_UPDATE credit is owed at enqueue; each pop replays exactly
    the popped frame's flow-control length through the credit callback."""
    credits = []

    async def cb(n):
        credits.append(n)

    r = HTTP2Recipient(credit_callback=cb)
    assert r.put_DATAFrame(_data(b'aaa')) is True
    assert r.put_DATAFrame(_data(b'bbbb', end_stream=True)) is True
    assert credits == []                      # nothing credited at enqueue
    ev = await r()
    assert ev['body'] == b'aaa' and credits == [3]
    ev = await r()
    assert ev['body'] == b'bbbb' and credits == [3, 4]
    assert ev['more_body'] is False


@pytest.mark.asyncio
async def test_recipient_zero_length_data_yields_no_credit():
    """The empty END_STREAM DATA grpcio sends consumes no window; a
    WINDOW_UPDATE with increment 0 is a §6.9.1 protocol error, so consuming
    it must not invoke the credit callback."""
    credits = []

    async def cb(n):
        credits.append(n)

    r = HTTP2Recipient(credit_callback=cb)
    assert r.put_DATAFrame(_data(b'', end_stream=True)) is True
    ev = await r()
    assert ev['more_body'] is False
    assert credits == []


@pytest.mark.asyncio
async def test_recipient_byte_budget_is_the_abuse_backstop():
    """put_DATAFrame refuses (→ RST) only when the peer overruns the
    advertised inbound window it was never credited for — not at a frame
    count a conformant peer can legitimately reach."""
    async def cb(n):
        pass

    r = HTTP2Recipient(credit_callback=cb, credit_budget=10)
    assert r.put_DATAFrame(_data(b'x' * 6)) is True
    assert r.put_DATAFrame(_data(b'x' * 4)) is True    # exactly at budget: fine
    assert r.put_DATAFrame(_data(b'x')) is False       # overrun: refused


@pytest.mark.asyncio
async def test_recipient_event_count_cap_stops_tiny_frame_floods():
    """The byte budget cannot see a zero/tiny-frame flood; a generous
    frame-count cap (queue_depth × 16) refuses degenerate dribble."""
    async def cb(n):
        pass

    r = HTTP2Recipient(queue_depth=1, credit_callback=cb, credit_budget=10**6)
    for _ in range(16):
        assert r.put_DATAFrame(_data(b'')) is True
    assert r.put_DATAFrame(_data(b'')) is False


@pytest.mark.asyncio
async def test_recipient_take_uncredited_returns_the_unconsumed_balance():
    """take_uncredited() reports (and clears) bytes enqueued but never popped
    — the balance the actor replays to the connection window on release."""
    async def cb(n):
        pass

    r = HTTP2Recipient(credit_callback=cb)
    r.put_DATAFrame(_data(b'aaa'))
    r.put_DATAFrame(_data(b'bb', end_stream=True))
    await r()                          # consume 3 bytes → credited via cb
    assert r.take_uncredited() == 2    # the un-popped frame's balance
    assert r.take_uncredited() == 0    # cleared


def test_legacy_recipient_without_callback_keeps_bounded_queue():
    """Direct constructions without a credit callback (tests, push streams)
    keep the historical bounded-queue, credit-at-enqueue behaviour."""
    r = HTTP2Recipient(queue_depth=2)
    assert r.credits_on_consume is False
    assert r.put_DATAFrame(_data(b'a')) is True
    assert r.put_DATAFrame(_data(b'b')) is True
    assert r.put_DATAFrame(_data(b'c')) is False   # QueueFull → refused


@pytest.mark.asyncio
async def test_over_queue_depth_burst_within_window_is_not_rst():
    """The original failure mode: more DATA frames than the old 64-deep queue,
    all within the 65535-byte window, while the handler is stalled.  With
    consume-based crediting the burst is buffered (bounded by the window
    budget), never RST_STREAM(ENHANCE_YOUR_CALM); once the handler drains,
    stream + connection credit is replayed in full."""
    from hpack import Encoder
    all_delivered = asyncio.Event()
    consumed = []

    async def app(scope, receive, send):
        await all_delivered.wait()     # stall until every frame is enqueued
        while True:
            ev = await receive()
            if ev['type'] != 'http.request':
                break
            consumed.append(ev['body'])
            if not ev.get('more_body', False):
                break
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok'})

    handler = _make_actor(app)
    enc = Encoder()
    frames = [
        _make_h2_frame(FrameTypes.SETTINGS, 0, 0, b''),
        _make_h2_frame(FrameTypes.HEADERS, HeaderFrameFlags.END_HEADERS, 1,
                       enc.encode([(b':method', b'POST'), (b':path', b'/up'),
                                   (b':scheme', b'https'), (b':authority', b'example.com')])),
    ]
    frames += [_make_h2_frame(FrameTypes.DATA, 0, 1, b'x' * 100)
               for _ in range(64)]
    frames.append(_make_h2_frame(
        FrameTypes.DATA, int(DataFrameFlags.END_STREAM), 1, b'x' * 100))

    async def fake_receive():
        if frames:
            return frames.pop(0)
        all_delivered.set()
        return None

    handler.receive = fake_receive
    await handler.run()

    rsts = [f for f in (c.args[0] for c in handler.send_frame.call_args_list)
            if f.FrameType() == FrameTypes.RST_STREAM]
    assert not rsts, f'window-conformant burst must not be RST: {rsts}'
    assert sum(len(b) for b in consumed) == 6500
    wus = _window_updates(handler)
    assert sum(n for sid, n in wus if sid == 1) == 6500
    assert sum(n for sid, n in wus if sid == 0) == 6500


@pytest.mark.asyncio
async def test_unread_body_balance_flushes_to_connection_window():
    """A handler that finishes without draining its body must not leak the
    shared connection window shut: the un-consumed balance is replayed as
    WINDOW_UPDATE(0) when the stream is released.  Stream-level credit is
    NOT replayed — the stream is closed (RFC 9113 §5.1)."""
    from hpack import Encoder

    async def app(scope, receive, send):
        await receive()                # reads ONLY the first 3-byte frame
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'done'})

    handler = _make_actor(app)
    enc = Encoder()
    settings = _make_h2_frame(FrameTypes.SETTINGS, 0, 0, b'')
    headers = _make_h2_frame(
        FrameTypes.HEADERS, HeaderFrameFlags.END_HEADERS, 1,
        enc.encode([(b':method', b'POST'), (b':path', b'/up'),
                    (b':scheme', b'https'), (b':authority', b'example.com')]))
    data1 = _make_h2_frame(FrameTypes.DATA, 0, 1, b'aaa')
    data2 = _make_h2_frame(
        FrameTypes.DATA, int(DataFrameFlags.END_STREAM), 1, b'bbbb')
    handler.receive = AsyncMock(side_effect=[settings, headers, data1, data2, None])
    await handler.run()
    for _ in range(10):                # let the release-time flush task run
        await asyncio.sleep(0)

    wus = _window_updates(handler)
    assert sum(n for sid, n in wus if sid == 1) == 3   # only the consumed frame
    assert sum(n for sid, n in wus if sid == 0) == 7   # consumed + flushed balance


@pytest.mark.asyncio
async def test_window_overrun_is_rst_enhance_your_calm():
    """A peer that keeps sending past the advertised 65535-byte inbound window
    (i.e. ignores the closed window) hits the abuse backstop: the frame is
    refused and the stream is RST_STREAM(ENHANCE_YOUR_CALM)."""
    from hpack import Encoder
    all_delivered = asyncio.Event()

    async def app(scope, receive, send):
        await all_delivered.wait()     # never consumes while frames arrive
        while (await receive())['type'] != 'http.disconnect':
            pass
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})

    handler = _make_actor(app)
    enc = Encoder()
    frames = [
        _make_h2_frame(FrameTypes.SETTINGS, 0, 0, b''),
        _make_h2_frame(FrameTypes.HEADERS, HeaderFrameFlags.END_HEADERS, 1,
                       enc.encode([(b':method', b'POST'), (b':path', b'/up'),
                                   (b':scheme', b'https'), (b':authority', b'example.com')])),
    ]
    # 5 × 16 KiB = 81920 bytes — crosses the 65535-byte budget at frame 4.
    frames += [_make_h2_frame(FrameTypes.DATA, 0, 1, b'x' * 16384)
               for _ in range(5)]

    async def fake_receive():
        if frames:
            return frames.pop(0)
        all_delivered.set()
        return None

    handler.receive = fake_receive
    await handler.run()

    calms = [f for f in (c.args[0] for c in handler.send_frame.call_args_list)
             if f.FrameType() == FrameTypes.RST_STREAM
             and f.error_code == ErrorCodes.ENHANCE_YOUR_CALM]
    assert calms, 'window overrun must trip the ENHANCE_YOUR_CALM backstop'

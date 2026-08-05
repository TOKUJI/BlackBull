"""Deferred reader (design A') and wire ownership on the WebSocket read path.

These are deliberately *structural* assertions, and that is the point.  The
behavioural contract — ``websocket_message`` fires when the server reads a
message — is satisfied identically by the eager reader and by the deferred
one, so a test that only watches emitted events cannot tell whether the
deferred path exists at all.  The properties the design turns on are
control-flow properties:

* a registered listener must **not** start a background reader at connect —
  a consuming handler keeps the inline path (no queue, no per-message task
  handoff), and the idle watchdog starts a reader only once the app goes
  quiet;
* exactly one of {inline ``receive()``, reader task, watchdog servicing}
  drives the transport at a time, so nothing can consume bytes out from
  under an app parked mid-frame;
* switching read modes must not strand an event the previous mode buffered.

``tests/architecture/events/test_websocket_message_server_path.py`` covers
the observable event contract over the real server; this file covers the
structure that contract is supposed to be delivered by.
"""
import asyncio

import pytest

from blackbull.asgi import ASGIEvent
from blackbull.server.recipient import (
    AbstractReader, WebSocketRecipient, _WS_READ_INLINE,
)
from blackbull.server.sender import AbstractWriter


class _FeedReader(AbstractReader):
    """Reader over an in-memory buffer; reads park while the buffer is dry.

    Parking (rather than returning ``b''``) is what lets a test hold the
    recipient inside an inline ``receive()`` and inspect who owns the wire.
    """

    def __init__(self, data: bytes = b'') -> None:
        self._buf = bytearray(data)
        self._more = asyncio.Event()

    def feed(self, data: bytes) -> None:
        self._buf += data
        self._more.set()

    async def _await_bytes(self, n: int) -> None:
        while len(self._buf) < n:
            self._more.clear()
            await self._more.wait()

    async def read(self, n: int) -> bytes:
        await self._await_bytes(1)
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    async def readexactly(self, n: int) -> bytes:
        await self._await_bytes(n)
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    def has_buffered(self) -> bool:
        return bool(self._buf)

    def buffered_len(self) -> int:
        return len(self._buf)

    def peek(self, n: int) -> bytes:
        return bytes(self._buf[:n])

    def at_eof(self) -> bool:
        return False


class _CollectingWriter(AbstractWriter):
    def __init__(self) -> None:
        self.written = bytearray()

    async def write(self, data: bytes) -> None:
        self.written += data


def _make_recipient(*, listeners: bool, depth: int = _WS_READ_INLINE
                    ) -> tuple[WebSocketRecipient, _FeedReader]:
    reader = _FeedReader()
    recipient = WebSocketRecipient(
        reader, _CollectingWriter(),
        ws_queue_depth=depth,
        on_message=lambda _event: asyncio.sleep(0),
        read_ahead_needed=lambda: listeners,
    )
    return recipient, reader


@pytest.mark.asyncio
async def test_listener_defers_the_reader_instead_of_starting_it():
    """A ``websocket_message`` listener must not force read-ahead at connect.

    The listener needs the message to be *read*, not read *ahead* — a handler
    that consumes drives the wire itself, so the reader task is only marked
    pending here.  Starting it at connect costs every consuming handler the
    queue handoff design A' exists to avoid.
    """
    recipient, _reader = _make_recipient(listeners=True)
    try:
        event = await recipient()
        assert event['type'] == ASGIEvent.WS_CONNECT

        assert recipient._event_queue is None, (
            'a listener started the read-ahead queue at connect; the '
            'consuming handler now pays the per-message handoff')
        assert recipient._reader_task is None
        assert recipient._deferred_pending is True, (
            'the reader was neither started nor deferred — the idle '
            'watchdog has nothing to start')
    finally:
        await recipient.shutdown()


@pytest.mark.asyncio
async def test_explicit_queue_depth_still_starts_the_reader_at_connect():
    """``BB_WS_QUEUE_DEPTH > 0`` is an explicit opt-in to read-ahead."""
    recipient, _reader = _make_recipient(listeners=False, depth=4)
    try:
        await recipient()
        assert recipient._event_queue is not None
        assert recipient._reader_task is not None
        assert recipient._deferred_pending is False
    finally:
        await recipient.shutdown()


@pytest.mark.asyncio
async def test_no_listener_never_defers_or_starts_a_reader():
    """The zero-listener echo stays fully inline — nothing to start, ever."""
    recipient, _reader = _make_recipient(listeners=False)
    try:
        await recipient()
        assert recipient._event_queue is None
        assert recipient._reader_task is None
        assert recipient._deferred_pending is False
    finally:
        await recipient.shutdown()


@pytest.mark.asyncio
async def test_idle_watchdog_starts_the_deferred_reader():
    """Once the app goes quiet, the watchdog tick starts the deferred reader.

    This is what keeps the read-time ``websocket_message`` contract for a
    handler that never calls ``receive()``.
    """
    recipient, _reader = _make_recipient(listeners=True)
    try:
        await recipient()
        assert recipient._deferred_pending is True

        recipient._on_idle_tick()

        assert recipient._event_queue is not None, (
            'the idle watchdog did not start the deferred reader')
        assert recipient._reader_task is not None
        assert recipient._deferred_pending is False
    finally:
        await recipient.shutdown()


@pytest.mark.asyncio
async def test_app_owns_the_wire_while_parked_in_receive():
    """Nothing may read the transport while an inline ``receive()`` drives it.

    The app parks inside the frame reader with a partial frame consumed; a
    second reader starting there would interpret payload bytes as a frame
    header and desync the stream.  Both the watchdog's servicing path and the
    deferred-reader start must yield.
    """
    recipient, _reader = _make_recipient(listeners=True)
    try:
        await recipient()                       # websocket.connect

        parked = asyncio.create_task(recipient())
        await asyncio.sleep(0)                  # let it reach the wire
        await asyncio.sleep(0)

        assert recipient._reading is True, (
            'the app is parked in receive() but the recipient does not '
            'claim wire ownership — the watchdog is free to read underneath it')

        assert await recipient.service_available_control_frames() is False, (
            'watchdog servicing ran while the app owned the wire')

        recipient._on_idle_tick()
        assert recipient._event_queue is None, (
            'the deferred reader started while the app owned the wire — '
            'two readers on one transport')

        parked.cancel()
        with pytest.raises(asyncio.CancelledError):
            await parked
        assert recipient._reading is False, 'wire ownership leaked after cancel'
    finally:
        await recipient.shutdown()


@pytest.mark.asyncio
async def test_switching_to_read_ahead_does_not_strand_a_buffered_event():
    """An event buffered by the inline path survives the switch to a queue.

    Once the queue exists the app reads from it alone, so anything the
    previous mode left in the handoff slot has to come with it.
    """
    recipient, _reader = _make_recipient(listeners=True)
    try:
        await recipient()
        # The handoff slot carries the message itself — the channel is native;
        # `receive()` encodes it as an ASGI event on the way out.
        recipient._pending.append('buffered')

        recipient._on_idle_tick()               # starts the deferred reader
        assert recipient._event_queue is not None

        event = await asyncio.wait_for(recipient(), timeout=1.0)
        assert event == {'type': ASGIEvent.WS_RECEIVE, 'text': 'buffered',
                         'bytes': None}, (
            'the message buffered before the switch was never delivered')
    finally:
        await recipient.shutdown()

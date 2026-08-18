"""Request-body limits on HTTP/2 — the same ceilings, a different refusal.

``BB_MAX_BODY_SIZE`` and ``BB_MIN_BODY_RATE`` are properties of the *request*,
not of HTTP/1.1, so they hold on both protocols.  What differs is what a
refusal can say and what it has to cost:

* A declared over-cap body (``content-length`` in HEADERS) is answered with a
  real **413** and then ``RST_STREAM(NO_ERROR)`` — the sequence RFC 9113 §8.1
  names for "complete response before the request finished, please stop
  sending".  The handler never runs.
* An undeclared over-cap body, or a trickled one, is discovered mid-stream and
  answered with ``RST_STREAM(ENHANCE_YOUR_CALM)`` — the same backstop this
  actor already uses for a window overrun.  The recipient is told, so a handler
  parked in ``receive()`` unwinds instead of waiting for the request timeout.

The connection survives either way.  That is the real asymmetry with HTTP/1.1,
where a refused body forces the connection closed: HTTP/2 frames every stream
explicitly, so octets we decline can never be re-read as the next request.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from hpack import Decoder, Encoder

from blackbull.protocol.frame_types import (
    DataFrameFlags, ErrorCodes, FrameTypes, HeaderFrameFlags,
)
from blackbull.server.http2_actor import HTTP2Actor
from blackbull.server.sender import AsyncioWriter

pytestmark = pytest.mark.asyncio

_CAP = 1024


@pytest.fixture(autouse=True)
def _small_body_cap(monkeypatch):
    monkeypatch.setenv('BB_MAX_BODY_SIZE', str(_CAP))


def _frame(type_byte: FrameTypes, flags: int = 0, stream_id: int = 0,
           payload: bytes = b'') -> bytes:
    return (len(payload).to_bytes(3, 'big') + type_byte + bytes([flags])
            + stream_id.to_bytes(4, 'big') + payload)


def _settings() -> bytes:
    return _frame(FrameTypes.SETTINGS, 0, 0, b'')


def _headers(fields, *, stream_id: int = 1, end_stream: bool = False) -> bytes:
    flags = HeaderFrameFlags.END_HEADERS
    if end_stream:
        flags |= HeaderFrameFlags.END_STREAM
    return _frame(FrameTypes.HEADERS, flags, stream_id, Encoder().encode(fields))


def _post(content_length: int | None) -> list[tuple[bytes, bytes]]:
    fields = [(b':method', b'POST'), (b':path', b'/upload'),
              (b':scheme', b'https'), (b':authority', b'example.com')]
    if content_length is not None:
        fields.append((b'content-length', str(content_length).encode()))
    return fields


def _data(payload: bytes, *, stream_id: int = 1,
          end_stream: bool = False) -> bytes:
    flags = DataFrameFlags.END_STREAM if end_stream else 0
    return _frame(FrameTypes.DATA, flags, stream_id, payload)


def _make_actor():
    """An actor whose control frames are captured and whose response bytes
    land in a writer we can decode."""
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.close = MagicMock()
    app = AsyncMock()
    actor = HTTP2Actor(None, AsyncioWriter(writer), app, aggregator=None)
    actor.send_frame = AsyncMock()
    return actor, app, writer


def _rst_codes(actor, stream_id: int = 1) -> list[int]:
    return [c.args[0].error_code for c in actor.send_frame.call_args_list
            if getattr(c.args[0], 'FrameType', None)
            and c.args[0].FrameType() == FrameTypes.RST_STREAM
            and c.args[0].stream_id == stream_id]


def _response_statuses(writer) -> list[bytes]:
    """Every ``:status`` the actor wrote, decoded off the wire.

    The response never goes through ``send_frame`` — it is written by the
    stream's sender — so the transport is the only place it can be observed,
    which is also the only place a client would see it.
    """
    written = b''.join(c.args[0] for c in writer.write.call_args_list
                       if c.args and isinstance(c.args[0], (bytes, bytearray)))
    decoder = Decoder()
    statuses = []
    pos = 0
    while pos + 9 <= len(written):
        length = int.from_bytes(written[pos:pos + 3], 'big')
        ftype = written[pos + 3]
        payload = written[pos + 9:pos + 9 + length]
        if ftype == int.from_bytes(FrameTypes.HEADERS, 'big'):
            for name, value in decoder.decode(payload, raw=True):
                if bytes(name) == b':status':
                    statuses.append(bytes(value))
        pos += 9 + length
    return statuses


class TestADeclaredOverCapBodyIsRefusedAtTheHead:
    async def test_it_answers_413_and_never_dispatches(self):
        actor, app, writer = _make_actor()
        actor.receive = AsyncMock(side_effect=[
            _settings(), _headers(_post(_CAP * 100)), None])
        await actor.run()

        assert b'413' in _response_statuses(writer), (
            f'expected a 413 on the wire, saw {_response_statuses(writer)}')
        app.assert_not_awaited()

    async def test_it_asks_the_peer_to_stop_without_an_error(self):
        """RFC 9113 §8.1 — ``NO_ERROR``.  The request was not malformed; we
        simply answered it early, and the peer should stop sending the body
        rather than treat its request as broken."""
        actor, _, _ = _make_actor()
        actor.receive = AsyncMock(side_effect=[
            _settings(), _headers(_post(_CAP * 100)), None])
        await actor.run()

        assert _rst_codes(actor) == [ErrorCodes.NO_ERROR]

    async def test_a_declared_body_within_the_cap_is_dispatched(self):
        actor, app, _ = _make_actor()
        actor.receive = AsyncMock(side_effect=[
            _settings(), _headers(_post(_CAP - 1)), None])
        await actor.run()

        assert _rst_codes(actor) == []
        app.assert_awaited()

    async def test_a_body_less_request_is_unaffected(self):
        actor, app, _ = _make_actor()
        fields = [(b':method', b'GET'), (b':path', b'/'),
                  (b':scheme', b'https'), (b':authority', b'example.com')]
        actor.receive = AsyncMock(side_effect=[
            _settings(), _headers(fields, end_stream=True), None])
        await actor.run()

        assert _rst_codes(actor) == []
        app.assert_awaited()


class TestAnUndeclaredOverCapBodyIsRefusedMidStream:
    async def test_data_past_the_cap_resets_the_stream(self):
        """No ``content-length`` to refuse at HEADERS, so the octets are what
        answers: the frame that crosses the cap is refused and the stream is
        reset."""
        actor, _, _ = _make_actor()
        actor.receive = AsyncMock(side_effect=[
            _settings(), _headers(_post(None)),
            _data(b'a' * 700), _data(b'b' * 700), None])
        await actor.run()

        assert _rst_codes(actor) == [ErrorCodes.ENHANCE_YOUR_CALM]

    async def test_data_within_the_cap_is_not_reset(self):
        actor, _, _ = _make_actor()
        actor.receive = AsyncMock(side_effect=[
            _settings(), _headers(_post(None)),
            _data(b'a' * 500), _data(b'b' * 400, end_stream=True), None])
        await actor.run()

        assert _rst_codes(actor) == []

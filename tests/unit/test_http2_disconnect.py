"""Tests for HTTP/2 disconnect detection via HTTP2Recipient and _frame_loop."""
import asyncio
import ssl
from unittest.mock import MagicMock

import pytest

from blackbull.server.recipient import AbstractReader, HTTP2Recipient
from blackbull.server.http2_actor import _signal_recipients


# ---------------------------------------------------------------------------
# HTTP2Recipient.put_disconnect
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_put_disconnect_unblocks_call():
    recipient = HTTP2Recipient()
    recipient.put_disconnect()
    event = await recipient()
    assert event == {'type': 'http.disconnect'}


@pytest.mark.asyncio
async def test_put_disconnect_after_data():
    """Disconnect event is delivered after any queued data events."""
    from blackbull.protocol.frame_types import Data
    frame = MagicMock(spec=Data)
    frame.payload = b'hello'
    frame.end_stream = False

    recipient = HTTP2Recipient()
    recipient.put_DATAFrame(frame)
    recipient.put_disconnect()

    first = await recipient()
    assert first['type'] == 'http.request'
    assert first['body'] == b'hello'

    second = await recipient()
    assert second == {'type': 'http.disconnect'}


# ---------------------------------------------------------------------------
# _signal_recipients
# ---------------------------------------------------------------------------

def test_signal_recipients_calls_put_disconnect_on_all():
    r1 = MagicMock(spec=HTTP2Recipient)
    r2 = MagicMock(spec=HTTP2Recipient)
    _signal_recipients({1: r1, 3: r2})
    r1.put_disconnect.assert_called_once()
    r2.put_disconnect.assert_called_once()


def test_signal_recipients_empty_dict():
    _signal_recipients({})  # must not raise


def test_only_a_stream_the_peer_ended_is_peer_gone():
    retired, reset = HTTP2Recipient(), HTTP2Recipient()
    retired.put_disconnect()
    reset.put_disconnect(peer=True)
    assert (retired.peer_gone, reset.peer_gone) == (False, True)


class TestHTTP2Recipient:
    @pytest.mark.asyncio
    async def test_with_initial_frame_enqueues_event(self):
        from blackbull.server.recipient import HTTP2Recipient
        from blackbull.protocol.frame_types import Data
        frame = MagicMock(spec=Data)
        frame.payload = b'body'
        frame.end_stream = True
        r = HTTP2Recipient(frame)
        event = await r()
        assert event == {'type': 'http.request', 'body': b'body', 'more_body': False}


class _Transport:
    """A loopback transport; with *reset*, the peer is gone and every write raises."""

    def __init__(self, reset: bool) -> None:
        self._reset = reset

    def write(self, data) -> None:
        if self._reset:
            raise ConnectionResetError()

    def writelines(self, parts) -> None:
        if self._reset:
            raise ConnectionResetError()

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    def is_closing(self) -> bool:
        return False

    def get_extra_info(self, name, default=None):
        return default


@pytest.mark.asyncio
@pytest.mark.parametrize('reset', [False, True], ids=['delivered', 'write-reset'])
async def test_a_stream_is_answered_only_when_its_end_reached_the_peer(reset):
    from blackbull.native import NativeResponse
    from blackbull.protocol.frame import FrameFactory
    from blackbull.server.sender import AsyncioWriter, SenderFactory

    sender = SenderFactory.http2(AsyncioWriter(_Transport(reset)), FrameFactory(), 1)
    recipient = HTTP2Recipient(sender=sender)
    await sender(NativeResponse(status=200, header=[], body=b'ok'))
    assert (sender.answered, recipient.peer_gone) == (not reset, reset)


class _LostReader(AbstractReader):
    """Hands out *data*, then waits for *lose* and raises *loss* as the
    transport's connection_lost would."""

    def __init__(self, data: bytes, lose: asyncio.Event, loss: BaseException) -> None:
        self.buf = bytearray(data)
        self.lose = lose
        self.loss = loss

    async def read(self, n: int) -> bytes:
        if not self.buf:
            await self.lose.wait()
            raise self.loss
        chunk = bytes(self.buf[:n])
        del self.buf[:n]
        return chunk

    async def readexactly(self, n: int) -> bytes:
        return await self.read(n)    # the buffer holds whole frames only


@pytest.mark.asyncio
@pytest.mark.parametrize('loss', [ConnectionResetError(), TimeoutError(), ssl.SSLError()],
                         ids=['reset', 'timeout', 'tls'])
async def test_a_transport_loss_mid_body_ends_the_request_disconnected(loss):
    from blackbull import BlackBull
    from blackbull.protocol.frame_types import FrameTypes
    from blackbull.server.http2_actor import HTTP2Actor
    from blackbull.server.sender import AsyncioWriter
    from tests.conformance.http2._harness import _make_h2_frame, _make_headers_frame

    app, ended, reading, lose = BlackBull(), [], asyncio.Event(), asyncio.Event()

    @app.route(path='/slow', methods=['POST'])
    async def slow(conn, receive, send):
        reading.set()
        await conn.body()

    @app.on('request_completed', blocking=True)
    async def completed(event):
        ended.append('request_completed')

    @app.on('request_disconnected', blocking=True)
    async def disconnected(event):
        ended.append('request_disconnected')

    fields = [(b':method', b'POST'), (b':path', b'/slow'), (b':scheme', b'http'),
              (b':authority', b'x')]
    data = (_make_headers_frame(stream_id=1, fields=fields)
            + _make_h2_frame(FrameTypes.DATA, 0, 1, b'abc'))
    actor = HTTP2Actor(_LostReader(data, lose, loss), AsyncioWriter(_Transport(False)), app,
                       aggregator=None)
    run = asyncio.ensure_future(actor.run())
    await reading.wait()
    lose.set()
    with pytest.raises((OSError, BaseExceptionGroup)):
        await run
    assert ended == ['request_disconnected']


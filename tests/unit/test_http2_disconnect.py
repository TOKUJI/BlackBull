"""Tests for HTTP/2 disconnect detection via HTTP2Recipient and _frame_loop."""
import asyncio
from unittest.mock import MagicMock

import pytest

from blackbull.server.recipient import HTTP2Recipient
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

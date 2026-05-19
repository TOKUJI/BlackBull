"""Tests for HTTP/2 disconnect detection via HTTP2Recipient and _frame_loop."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from blackbull.event_aggregator import EventAggregator
from blackbull.server.recipient import HTTP2Recipient
from blackbull.server.http2_actor import _signal_recipients
from blackbull.server.access_log import _make_disconnect_detecting_receive as _make_h2_disconnect_receiver


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


# ---------------------------------------------------------------------------
# _make_h2_disconnect_receiver
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_h2_disconnect_receiver_passes_through_normal_events():
    async def raw_receive():
        return {'type': 'http.request', 'body': b'x', 'more_body': False}

    aggregator = MagicMock(spec=EventAggregator)
    scope = {}
    wrapped = _make_h2_disconnect_receiver(raw_receive, scope, aggregator)
    event = await wrapped()
    assert event == {'type': 'http.request', 'body': b'x', 'more_body': False}
    aggregator.on_request_disconnected.assert_not_called()


@pytest.mark.asyncio
async def test_h2_disconnect_receiver_emits_request_disconnected():
    async def raw_receive():
        return {'type': 'http.disconnect'}

    aggregator = MagicMock(spec=EventAggregator)
    aggregator.on_request_disconnected = AsyncMock()
    scope = {}
    wrapped = _make_h2_disconnect_receiver(raw_receive, scope, aggregator)
    event = await wrapped()

    assert event == {'type': 'http.disconnect'}
    aggregator.on_request_disconnected.assert_awaited_once_with(scope)
    assert scope['_disconnected'] is True


@pytest.mark.asyncio
async def test_h2_disconnect_receiver_idempotent():
    """request_disconnected fires only once even if handler calls receive() again."""
    calls = 0

    async def raw_receive():
        nonlocal calls
        calls += 1
        return {'type': 'http.disconnect'}

    aggregator = MagicMock(spec=EventAggregator)
    aggregator.on_request_disconnected = AsyncMock()
    scope = {}
    wrapped = _make_h2_disconnect_receiver(raw_receive, scope, aggregator)

    await wrapped()   # first call — emits event
    await wrapped()   # second call — already flagged, must not emit again

    aggregator.on_request_disconnected.assert_awaited_once()

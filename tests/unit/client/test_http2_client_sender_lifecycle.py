"""A sender is released when its stream can no longer send.

``HTTP2Client._senders`` was inserted into and never removed, and both
``_on_window_update`` (stream 0) and ``_on_initial_window_size`` sweep every
entry.  The peer chooses how often those sweeps run and the client chooses how
long the dict is, so the cost was O(requests x peer-chosen frames) and the
memory was never returned.

Release keys off the last *send*, not the last *receive*: a server may answer
with END_STREAM while the request body is still uploading (an early 401 or
413), so dropping the sender when the response completes would strand
``_write_data`` on a window event nothing will ever set again.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.client.http2 import HTTP2Client
from blackbull.server.sender import AbstractWriter

_FRAME_HEADER = 9
_DATA_FRAME_TYPE = 0x00


class _RecordingWriter(AbstractWriter):
    """Counts DATA payload only — byte 3 of the frame header is the type,
    and HEADERS rides the same writer."""

    def __init__(self) -> None:
        self.payload_bytes = 0

    async def write(self, data: bytes) -> None:
        if len(data) >= _FRAME_HEADER and data[3] == _DATA_FRAME_TYPE:
            self.payload_bytes += len(data) - _FRAME_HEADER


def _client() -> HTTP2Client:
    c = HTTP2Client('localhost', 1)
    c._writer = _RecordingWriter()
    return c


class TestSenderRelease:
    @pytest.mark.asyncio
    async def test_a_finished_request_releases_its_sender(self):
        c = _client()
        task = asyncio.create_task(c.request('GET', '/'))
        await asyncio.sleep(0)

        assert c._senders == {}, 'the send is over; nothing may still sweep it'

        c._complete(1)
        await task
        assert c._senders == {}

    @pytest.mark.asyncio
    async def test_a_failed_send_releases_its_sender(self):
        """A send that raises must not leave its sender behind.

        Not ``ConnectionResetError``: ``BaseSender._guarded_write`` tolerates
        peer-close deliberately, marking the sender closed and dropping the
        write, so that one never reaches ``request()``.
        """
        class _Broken(AbstractWriter):
            async def write(self, data: bytes) -> None:
                raise RuntimeError('transport refused the frame')

        c = _client()
        c._writer = _Broken()
        with pytest.raises(RuntimeError):
            await c.request('GET', '/')
        assert c._senders == {}

    @pytest.mark.asyncio
    async def test_an_early_response_does_not_strand_the_upload(self):
        """The hazard that rules out releasing on response completion.

        The peer answers before the body is up.  The upload must still finish
        when its window is credited, which it cannot do if the sender it is
        parked on has been dropped.
        """
        c = _client()
        c._on_initial_window_size(10)          # park after ten octets
        task = asyncio.create_task(c.request('POST', '/', body=b'z' * 100))
        await asyncio.sleep(0)
        assert c._writer.payload_bytes == 10, 'expected the upload to park'

        c._complete(1)                          # early END_STREAM response
        await asyncio.sleep(0)

        c._on_window_update(_StreamWindowUpdate(stream_id=1, increment=100))
        await asyncio.sleep(0)
        assert c._writer.payload_bytes == 100, 'the credit reached nobody'

        await task
        assert c._senders == {}

    @pytest.mark.asyncio
    async def test_a_raw_stream_keeps_its_sender_until_unregistered(self):
        """WebSocket-over-H2 holds its sender for the life of the session."""
        c = _client()
        c.register_raw_stream(5)
        c._make_sender(5)
        assert 5 in c._senders, 'the session still writes through it'

        c.unregister_raw_stream(5)
        assert c._senders == {}

    @pytest.mark.asyncio
    async def test_teardown_releases_what_is_left(self):
        c = _client()
        c._make_sender(7)
        await c.__aexit__(None, None, None)
        assert c._senders == {}


class _StreamWindowUpdate:
    def __init__(self, stream_id: int, increment: int) -> None:
        self.stream_id = stream_id
        self.window_size = increment

"""The client's connection-level send window is one budget, not one per stream.

RFC 9113 §6.9.1 makes the connection window a single budget every stream
debits.  ``HTTP2Sender`` takes the ``ConnectionWindow`` to debit; the server
hands one instance to every sender, and the client passed none, so each sender
allocated a private copy and N concurrent streams each believed they held the
whole connection budget.  A strict peer answers the overrun with a
connection-level ``FLOW_CONTROL_ERROR``, which ends every stream rather than
the offending one.

The assertion is bytes on the wire, because that is what the peer counts.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.client.http2 import HTTP2Client
from blackbull.protocol.frame_types import DEFAULT_INITIAL_WINDOW_SIZE
from blackbull.server.sender import AbstractWriter

_FRAME_HEADER = 9


class _RecordingWriter(AbstractWriter):
    """Counts DATA payload octets, which is what the window is denominated in."""

    def __init__(self) -> None:
        self.frames: list[int] = []

    async def write(self, data: bytes) -> None:
        self.frames.append(len(data) - _FRAME_HEADER)

    @property
    def payload_bytes(self) -> int:
        return sum(self.frames)


def _client(writer: _RecordingWriter) -> HTTP2Client:
    c = HTTP2Client('localhost', 1)
    c._writer = writer
    return c


class TestConnectionWindowIsShared:
    @pytest.mark.asyncio
    async def test_two_streams_cannot_each_spend_a_full_window(self):
        """Two streams sending 40 KiB each may put at most one window on the
        wire before the peer credits more."""
        writer = _RecordingWriter()
        c = _client(writer)
        body = b'x' * 40000

        tasks = [
            asyncio.create_task(c._make_sender(sid)._write_data(body, end_stream=True))
            for sid in (1, 3)
        ]
        done, pending = await asyncio.wait(tasks, timeout=0.25)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        assert writer.payload_bytes <= DEFAULT_INITIAL_WINDOW_SIZE, (
            f'{writer.payload_bytes} octets on the wire against a '
            f'{DEFAULT_INITIAL_WINDOW_SIZE}-octet connection window'
        )

    @pytest.mark.asyncio
    async def test_crediting_the_connection_releases_a_blocked_stream(self):
        """A stream-0 WINDOW_UPDATE credits the shared budget once, and the
        stream parked on it resumes."""
        writer = _RecordingWriter()
        c = _client(writer)
        body = b'y' * 40000

        tasks = [
            asyncio.create_task(c._make_sender(sid)._write_data(body, end_stream=True))
            for sid in (1, 3)
        ]
        await asyncio.wait(tasks, timeout=0.25)
        stalled = writer.payload_bytes

        c._on_window_update(_ConnectionWindowUpdate(increment=80000))
        await asyncio.wait(tasks, timeout=0.25)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        assert writer.payload_bytes > stalled, 'the credit woke nobody'
        assert writer.payload_bytes == 80000, 'both bodies should now be out'


class _ConnectionWindowUpdate:
    """Stand-in for the WINDOW_UPDATE frame ``_on_window_update`` reads."""

    stream_id = 0

    def __init__(self, increment: int) -> None:
        self.window_size = increment

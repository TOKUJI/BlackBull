"""A server sender keeps the content rules it advertises (RFC 9110 §15.3.6)."""
from __future__ import annotations

import asyncio
from http import HTTPStatus

import pytest

from blackbull.protocol.frame import FrameFactory
from blackbull.server.sender import AbstractWriter, ConnectionWindow, HTTP2Sender

pytestmark = pytest.mark.asyncio

DATA = 0


class _Writer(AbstractWriter):
    def __init__(self) -> None:
        self.frames: list[tuple[int, int, int, bytes]] = []

    async def write(self, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            length = int.from_bytes(data[offset:offset + 3], 'big')
            end = offset + 9 + length
            kind, flags = data[offset + 3:offset + 5]
            sid = int.from_bytes(data[offset + 5:offset + 9], 'big')
            self.frames.append((kind, flags, sid, data[offset + 9:end]))
            offset = end

    def body(self, sid: int | None = None) -> bytes:
        return b''.join(payload for kind, _, stream, payload in self.frames
                        if kind == DATA and (sid is None or sid == stream))


def _sender(head_mode: bool = False) -> tuple[HTTP2Sender, _Writer]:
    writer = _Writer()
    sender = HTTP2Sender(writer, FrameFactory(), 1,
                         conn_window=ConnectionWindow(1 << 20),
                         initial_window=1 << 20,
                         flow_control_timeout=0.0,
                         head_mode=head_mode)
    return sender, writer


@pytest.mark.parametrize('status', [204, 205, 304])
async def test_no_content_reaches_the_wire_on_a_bodyless_status(status):
    """RFC 9112 §6.3 rule 1 for 204 and 304, RFC 9110 §15.3.6 for 205: the
    head promises none, so the octets never leave. The in-tree HTTP/2 client
    refuses exactly these frames."""
    sender, writer = _sender()
    await sender(b'abc', HTTPStatus(status))
    assert writer.body() == b''


async def test_no_content_reaches_the_wire_on_a_head_response():
    sender, writer = _sender(head_mode=True)
    await sender(b'abc', HTTPStatus.OK)
    assert writer.body() == b''


async def test_content_still_reaches_the_wire_on_an_ordinary_status():
    sender, writer = _sender()
    await sender(b'abc', HTTPStatus.OK)
    assert writer.body() == b'abc'


def _wire(writer: _Writer) -> list[tuple[int, int]]:
    """(frame kind, END_STREAM bit) for every frame written."""
    return [(kind, flags & 0x1) for kind, flags, _, _ in writer.frames]


async def test_the_buffered_path_keeps_the_rules_too():
    """The ASGI arms are the ones production uses, and they write through
    `_write_response_start_and_body` rather than the bytes arm above.

    The flush is scheduled rather than synchronous, so the assert waits a
    tick: without it this test passes against a sender whose rules have been
    deleted outright."""
    sender, writer = _sender()
    await sender({'type': 'http.response.start', 'status': 204, 'headers': [],
                  'trailers': True})
    await sender({'type': 'http.response.body', 'body': b'x',
                  'more_body': True})
    await asyncio.sleep(0)
    assert _wire(writer) == [(1, 1)]


async def test_no_frame_follows_end_stream_on_a_bodyless_status():
    """A second body chunk after the head already carried END_STREAM would be
    STREAM_CLOSED — a protocol error, not merely unwanted content."""
    sender, writer = _sender()
    await sender({'type': 'http.response.start', 'status': 205, 'headers': [],
                  'trailers': True})
    await sender({'type': 'http.response.body', 'body': b'ab',
                  'more_body': True})
    await sender({'type': 'http.response.body', 'body': b'cd',
                  'more_body': True})
    await asyncio.sleep(0)
    assert _wire(writer) == [(1, 1)]


async def test_a_trailer_section_does_not_reach_a_bodyless_status():
    """RFC 9112 §6.3 rule 1 — such a response "cannot contain a message body
    or trailer section", so the head terminates it and the trailers never
    leave. The head terminates the response in place of the trailing section,
    which is the shape a gRPC error takes."""
    sender, writer = _sender()
    await sender({'type': 'http.response.start', 'status': 204, 'headers': [],
                  'trailers': True})
    await sender({'type': 'http.response.body', 'body': b'ab',
                  'more_body': True})
    await sender({'type': 'http.response.trailers',
                  'headers': [(b'x-t', b'1')], 'more_trailers': False})
    await asyncio.sleep(0)
    assert _wire(writer) == [(1, 1)]


async def test_an_informational_head_leaves_the_stream_open_for_the_final_one():
    """An informational response is not the response yet: it carries no
    END_STREAM, so the final response must still be able to close the stream.
    Marking the stream finished here would leak it."""
    sender, writer = _sender()
    await sender(b'', HTTPStatus.CONTINUE)
    await sender(b'hello', HTTPStatus.OK)
    await asyncio.sleep(0)
    assert _wire(writer) == [(1, 0), (1, 0), (0, 1)]

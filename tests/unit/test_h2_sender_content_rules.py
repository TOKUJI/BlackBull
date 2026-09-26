"""A server sender keeps the content rules it advertises (RFC 9110 §15.3.6)."""
from __future__ import annotations

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

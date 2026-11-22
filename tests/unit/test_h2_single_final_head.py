"""One final response head per HTTP/2 stream (RFC 9113 §8.1).

A handler that fails after its head left must not get a second head on the
same stream: the stream ends with RST_STREAM(INTERNAL_ERROR), as HTTP/1.1
discards the second response.  A stream that already stalled on flow control
keeps its own ending (the stall is raised again, and the actor cancels it).
"""
from http import HTTPStatus

import pytest

from blackbull.native import NativeResponse
from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import ErrorCodes
from blackbull.server.sender import (AbstractWriter, ConnectionWindow,
                                     FlowControlStalled, HTTP2Sender)

pytestmark = pytest.mark.asyncio

HEADERS, DATA, RST_STREAM = 1, 0, 3
HEAD = [(b'content-type', b'text/plain')]


class _Capture(AbstractWriter):
    def __init__(self):
        self.wire = bytearray()

    async def write(self, data: bytes) -> None:
        self.wire += data

    async def writelines(self, parts) -> None:
        for part in parts:
            self.wire += part

    async def drain(self) -> None:
        return None


def _frames(wire: bytes):
    out, i = [], 0
    while i < len(wire):
        length = int.from_bytes(wire[i:i + 3], 'big')
        out.append((wire[i + 3], wire[i + 9:i + 9 + length]))
        i += 9 + length
    return out


def _sender(window: int = 65535):
    writer = _Capture()
    sender = HTTP2Sender(writer, FrameFactory(), 1, conn_window=ConnectionWindow(window))
    sender.stream_window_size = window
    return writer, sender


@pytest.mark.parametrize('second', [
    NativeResponse(status=500, header=HEAD, body=b'boom', more_body=False),
    NativeResponse(status=500, header=HEAD, trailers=[(b'x-error', b'1')]),
    b'boom',
], ids=['native-response', 'head-with-trailers', 'bytes'])
async def test_a_second_head_resets_the_stream_instead(second):
    writer, sender = _sender()
    await sender(NativeResponse(status=200, header=HEAD, body=b'partial', more_body=True))
    await sender(second, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    kinds = [kind for kind, _payload in _frames(writer.wire)]
    assert kinds.count(HEADERS) == 1, f'a second final head reached the peer: {kinds}'
    assert kinds[-1] == RST_STREAM
    assert int.from_bytes(_frames(writer.wire)[-1][1], 'big') == ErrorCodes.INTERNAL_ERROR


async def test_a_stalled_stream_still_raises_its_stall(monkeypatch):
    from blackbull.env import reset_settings_cache
    monkeypatch.setenv('BB_WRITE_TIMEOUT', '0.05')
    reset_settings_cache()
    writer, sender = _sender(window=0)
    with pytest.raises(FlowControlStalled):
        await sender(NativeResponse(status=200, header=HEAD, body=b'x' * 100, more_body=True))
    with pytest.raises(FlowControlStalled):
        await sender(NativeResponse(status=500, header=HEAD, body=b'boom', more_body=False))
    assert [k for k, _p in _frames(writer.wire)].count(HEADERS) == 1
    reset_settings_cache()

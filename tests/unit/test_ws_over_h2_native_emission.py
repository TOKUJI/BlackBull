"""RFC 8441 WS-over-H2 writes native, like every other framework producer.

`HTTP2WSWriter` wraps raw WebSocket frame bytes as HTTP/2 DATA so the RFC 6455
codec can run over an H2 stream.  It is framework-owned code on BlackBull's own
send path, so it has no interop reason to emit `http.response.body` dicts —
which the H2 sender's native arm would only convert back.

The observable contract is the bytes on the wire and where END_STREAM lands, so
that is what these assert: a frame write must never end the stream (the session
stays open), and `close()` must end it exactly once.
"""
import pytest

from blackbull.native import NativeResponse
from blackbull.server.http2_ws import HTTP2WSWriter


class _CapturingSender:
    """Stands in for `HTTP2Sender`, recording what the writer hands it."""

    def __init__(self):
        self.sent: list = []

    async def __call__(self, body, status=None, headers=None):
        self.sent.append(body)


@pytest.mark.asyncio
async def test_frame_write_is_native_and_keeps_the_stream_open():
    sender = _CapturingSender()
    writer = HTTP2WSWriter(sender)

    await writer.write(b'\x81\x05hello')

    assert len(sender.sent) == 1
    event = sender.sent[0]
    assert isinstance(event, NativeResponse), (
        f'WS-over-H2 emitted {type(event).__name__}, not a native response')
    assert event.body == b'\x81\x05hello'
    assert event.more_body is True, 'a frame write must not end the stream'
    assert event.header is None, 'the handshake already sent the header arm'


@pytest.mark.asyncio
async def test_close_ends_the_stream_once():
    sender = _CapturingSender()
    writer = HTTP2WSWriter(sender)

    await writer.close()
    await writer.close()          # idempotent — RFC 8441 §5 orderly close

    assert len(sender.sent) == 1
    event = sender.sent[0]
    assert isinstance(event, NativeResponse)
    assert event.body == b''
    assert event.more_body is False


@pytest.mark.asyncio
async def test_frames_then_close_is_one_stream():
    sender = _CapturingSender()
    writer = HTTP2WSWriter(sender)

    await writer.write(b'one')
    await writer.write(b'two')
    await writer.close()

    assert [e.body for e in sender.sent] == [b'one', b'two', b'']
    assert [e.more_body for e in sender.sent] == [True, True, False]

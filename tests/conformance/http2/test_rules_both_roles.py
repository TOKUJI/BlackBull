"""RFC 9113 rules that bind both endpoints, each row sent by the peer to the
server and to the client.

Stream 1 is open in both: the server has received a request on it, the client
has sent one.  After the row's frames the peer sends a PING; the answer is the
first RST_STREAM or GOAWAY the endpoint sends, or 'ignored' when the PING ACK
comes back first.  An answer listing several is that many answers in order.
"""
import asyncio
import contextlib

import pytest

from blackbull.client import HTTP2Client
from blackbull.protocol.frame_types import ErrorCodes, FrameTypes
from tests.caps._drive import _h2_answer, h2_request, make_app
from tests.conformance.http2._harness import _make_h2_frame, _make_headers_frame, h2_connect
from tests.harness import peer, read_h2_frame, served

pytestmark = pytest.mark.asyncio

F = _make_h2_frame
WU, SETTINGS, PING, RST, DATA, HEADERS, CONT, GOAWAY, PRIO, PUSH = (
    FrameTypes.WINDOW_UPDATE, FrameTypes.SETTINGS, FrameTypes.PING,
    FrameTypes.RST_STREAM, FrameTypes.DATA, FrameTypes.HEADERS,
    FrameTypes.CONTINUATION, FrameTypes.GOAWAY, FrameTypes.PRIORITY,
    FrameTypes.PUSH_PROMISE)
OPEN_BLOCK = _make_headers_frame(1, end_headers=False, fields=[(b'x-a', b'1')])


def u32(n: int) -> bytes:
    return n.to_bytes(4, 'big')


def setting(ident: int, value: int) -> bytes:
    return ident.to_bytes(2, 'big') + u32(value)


#: frames the peer sends -> the answer both endpoints owe (RFC 9113 section)
RULES: dict[str, tuple[tuple[bytes, ...], str]] = {
    # §6.9
    'window-update-zero-on-connection': ((F(WU, 0, 0, u32(0)),), 'GOAWAY PROTOCOL_ERROR'),
    'window-update-zero-on-stream': ((F(WU, 0, 1, u32(0)),), 'RST_STREAM PROTOCOL_ERROR'),
    'window-update-overflows-connection': ((F(WU, 0, 0, u32(2**31 - 1)),),
                                           'GOAWAY FLOW_CONTROL_ERROR'),
    'window-update-overflows-stream': ((F(WU, 0, 1, u32(2**31 - 1)),),
                                       'RST_STREAM FLOW_CONTROL_ERROR'),
    'window-update-overflows-stream-in-two-steps': ((F(WU, 0, 1, u32(2**30)),) * 2,
                                                    'RST_STREAM FLOW_CONTROL_ERROR'),
    'window-update-bad-length': ((F(WU, 0, 0, b'\x00\x00\x01'),), 'GOAWAY FRAME_SIZE_ERROR'),
    # §6.5, §6.5.2
    'settings-on-a-stream': ((F(SETTINGS, 0, 1),), 'GOAWAY PROTOCOL_ERROR'),
    'settings-bad-length': ((F(SETTINGS, 0, 0, b'\x00' * 5),), 'GOAWAY FRAME_SIZE_ERROR'),
    'settings-ack-with-payload': ((F(SETTINGS, 1, 0, setting(3, 1)),),
                                  'GOAWAY FRAME_SIZE_ERROR'),
    'settings-enable-push-not-boolean': ((F(SETTINGS, 0, 0, setting(2, 2)),),
                                         'GOAWAY PROTOCOL_ERROR'),
    'settings-window-at-the-maximum': ((F(SETTINGS, 0, 0, setting(4, 2**31 - 1)),), 'ignored'),
    'settings-delta-reaches-the-maximum': (
        (F(WU, 0, 1, u32(2**31 - 1 - 65536)), F(SETTINGS, 0, 0, setting(4, 65536))),
        'ignored'),
    'settings-delta-overflows-a-stream': (
        (F(WU, 0, 1, u32(2**31 - 1 - 65535)), F(SETTINGS, 0, 0, setting(4, 65536))),
        'GOAWAY FLOW_CONTROL_ERROR'),
    'settings-window-too-large': ((F(SETTINGS, 0, 0, setting(4, 2**31)),),
                                  'GOAWAY FLOW_CONTROL_ERROR'),
    'settings-frame-size-too-small': ((F(SETTINGS, 0, 0, setting(5, 16383)),),
                                      'GOAWAY PROTOCOL_ERROR'),
    'settings-unknown-identifier': ((F(SETTINGS, 0, 0, setting(0x99, 1)),), 'ignored'),
    # §6.7
    'ping-on-a-stream': ((F(PING, 0, 1, b'\x00' * 8),), 'GOAWAY PROTOCOL_ERROR'),
    'ping-bad-length': ((F(PING, 0, 0, b'\x00' * 7),), 'GOAWAY FRAME_SIZE_ERROR'),
    # §6.4, §5.1
    'rst-stream-on-stream-0': ((F(RST, 0, 0, u32(0)),), 'GOAWAY PROTOCOL_ERROR'),
    'rst-stream-bad-length': ((F(RST, 0, 1, b'\x00\x00\x00'),), 'GOAWAY FRAME_SIZE_ERROR'),
    'rst-stream-on-idle-stream': ((F(RST, 0, 5, u32(0)),), 'GOAWAY PROTOCOL_ERROR'),
    # §5.1 late frames, by who reset the stream
    'data-after-the-peers-reset': ((F(RST, 0, 1, u32(8)), F(DATA, 0, 1, b'x')),
                                   'RST_STREAM STREAM_CLOSED'),
    'window-update-after-the-peers-reset': ((F(RST, 0, 1, u32(8)), F(WU, 0, 1, u32(1))),
                                            'RST_STREAM STREAM_CLOSED'),
    'data-after-our-reset': ((F(PRIO, 0, 1, u32(1) + b'\x10'), F(DATA, 0, 1, b'x')),
                             'RST_STREAM PROTOCOL_ERROR, ignored'),
    'data-after-a-refused-field-section': (
        (_make_headers_frame(1, fields=[(b'X-Upper', b'1')]), F(DATA, 0, 1, b'x')),
        'RST_STREAM PROTOCOL_ERROR, ignored'),
    'window-update-after-our-reset': ((F(PRIO, 0, 1, u32(1) + b'\x10'), F(WU, 0, 1, u32(1))),
                                      'RST_STREAM PROTOCOL_ERROR, ignored'),
    'rst-stream-never-answered-in-kind': ((F(RST, 0, 1, u32(8)),) * 2, 'ignored'),
    'data-on-idle-stream': ((F(DATA, 0, 5, b'x'),), 'GOAWAY PROTOCOL_ERROR'),
    'window-update-on-idle-stream': ((F(WU, 0, 5, u32(1)),), 'GOAWAY PROTOCOL_ERROR'),
    # §6.1, §6.2, §6.3, §6.8
    'data-on-stream-0': ((F(DATA, 0, 0, b'x'),), 'GOAWAY PROTOCOL_ERROR'),
    'headers-on-stream-0': ((F(HEADERS, 4, 0, b''),), 'GOAWAY PROTOCOL_ERROR'),
    'priority-on-stream-0': ((F(PRIO, 0, 0, u32(1) + b'\x10'),), 'GOAWAY PROTOCOL_ERROR'),
    'priority-bad-length': ((F(PRIO, 0, 1, b'\x00' * 4),), 'RST_STREAM FRAME_SIZE_ERROR'),
    'priority-on-an-idle-stream': ((F(PRIO, 0, 7, u32(99) + b'\x0f'),), 'ignored'),
    'priority-on-itself': ((F(PRIO, 0, 1, u32(1) + b'\x10'),), 'RST_STREAM PROTOCOL_ERROR'),
    'goaway-on-a-stream': ((F(GOAWAY, 0, 1, u32(0) + u32(0)),), 'GOAWAY PROTOCOL_ERROR'),
    'goaway-bad-length': ((F(GOAWAY, 0, 0, b'\x00' * 7),), 'GOAWAY FRAME_SIZE_ERROR'),
    'headers-priority-field-short': ((F(HEADERS, 0x24, 1, b'1234'),), 'GOAWAY FRAME_SIZE_ERROR'),
    'headers-priority-field-short-in-an-open-block': ((F(HEADERS, 0x20, 1, b'1234'),),
                                                      'GOAWAY FRAME_SIZE_ERROR'),
    'headers-priority-field-short-padded-in-an-open-block': (
        (F(HEADERS, 0x28, 1, b'\x001234'),), 'GOAWAY FRAME_SIZE_ERROR'),
    # §6.1, §6.2, §6.6: padding as long as the payload
    'data-padding-too-long': ((F(DATA, 0x8, 1, b'\x05abcd'),), 'GOAWAY PROTOCOL_ERROR'),
    'headers-padding-too-long': ((F(HEADERS, 0xc, 1, b'\x0a\x82\x84'),),
                                 'GOAWAY PROTOCOL_ERROR'),
    'headers-padding-too-long-across-frames': (
        (F(HEADERS, 0x8, 1, b'\x0a\x82'), F(CONT, 4, 1, b'\x84')), 'GOAWAY PROTOCOL_ERROR'),
    'push-promise-padding-too-long': (
        (F(PUSH, 0xc, 1, b'\xc8' + u32(2) + b'\x82' * 12),), 'GOAWAY PROTOCOL_ERROR'),
    'push-promise-padding-too-long-across-frames': (
        (F(PUSH, 0x8, 1, b'\xff' + u32(2) + b'\x00'), F(CONT, 4, 1, b'\x00')),
        'GOAWAY PROTOCOL_ERROR'),
    # §4.3
    'block-undecodable': ((F(HEADERS, 4, 1, b'\x80'),), 'GOAWAY COMPRESSION_ERROR'),
    'block-table-size-over-the-limit': ((F(HEADERS, 4, 1, b'\x3f\xe1\x3f'),),
                                        'GOAWAY COMPRESSION_ERROR'),
    'block-undecodable-across-frames': ((F(HEADERS, 0, 1, b'\x00'), F(CONT, 4, 1, b'\x80')),
                                        'GOAWAY COMPRESSION_ERROR'),
    # §6.10, §4.3
    'continuation-without-headers': ((F(CONT, 4, 1, b''),), 'GOAWAY PROTOCOL_ERROR'),
    'block-interrupted': ((OPEN_BLOCK,), 'GOAWAY PROTOCOL_ERROR'),
    'continuation-on-another-stream': ((OPEN_BLOCK, F(CONT, 4, 3, b'')),
                                       'GOAWAY PROTOCOL_ERROR'),
    'block-malformed-across-frames': (
        (_make_headers_frame(1, end_headers=False, fields=[(b'X-Upper', b'1')]),
         F(CONT, 4, 1, b'')), 'RST_STREAM PROTOCOL_ERROR'),
    'block-over-the-head-limit': ((OPEN_BLOCK, *[F(CONT, 0, 1, b'\x00' * 16000)] * 5),
                                  'GOAWAY ENHANCE_YOUR_CALM'),
    # §4.1, §4.2, §5.5
    'unknown-frame-type': ((F(b'\xfa', 0, 0, b'xyz'),), 'ignored'),
    # CVE-2019-9512, -9515, -9518, CVE-2023-44487: BB_FRAME_RATE_LIMIT (20/s)
    'ping-within-the-budget': ((F(PING, 0, 0, b'\x00' * 8),) * 18, 'ignored'),
    'ping-flood': ((F(PING, 0, 0, b'\x00' * 8),) * 21, 'GOAWAY ENHANCE_YOUR_CALM'),
    'settings-flood': ((F(SETTINGS),) * 21, 'GOAWAY ENHANCE_YOUR_CALM'),
    'rst-stream-flood': ((F(RST, 0, 1, u32(8)),) * 21, 'GOAWAY ENHANCE_YOUR_CALM'),
    'empty-continuation-flood': ((OPEN_BLOCK, *[F(CONT, 0, 1, b'')] * 21),
                                 'GOAWAY ENHANCE_YOUR_CALM'),
    'frame-over-max-size': ((F(DATA, 0, 1, b'\x00' * 16385),), 'GOAWAY FRAME_SIZE_ERROR'),
}

_PROBE = F(PING, 0, 0, b'p' * 8)


async def as_server(frames: tuple[bytes, ...], count: int = 1) -> str:
    """The server's answers, with stream 1 opened by a request it is reading."""
    async with served(make_app()) as s:
        wire = await h2_connect(s)
        await wire.send(b''.join((h2_request(1), *frames, _PROBE)))
        return ', '.join([await _h2_answer(wire) for _ in range(count)])


async def as_client(frames: tuple[bytes, ...], count: int = 1) -> str:
    """The client's answers, with stream 1 opened by its own request."""
    answers: list[str] = []
    answered = asyncio.Event()

    async def origin(reader, writer):
        await reader.readexactly(24)
        writer.write(F(SETTINGS))
        await writer.drain()
        while (await read_h2_frame(reader))[:3:2] != (FrameTypes.HEADERS.value[0], 1):
            pass
        writer.write(b''.join(frames) + _PROBE)
        await writer.drain()
        with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError):
            while not answered.is_set():
                kind, flags, _, payload = await read_h2_frame(reader)
                if kind == RST.value[0]:
                    answers.append(f'RST_STREAM {ErrorCodes(int.from_bytes(payload[:4], "big")).name}')
                elif kind == GOAWAY.value[0]:
                    answers.append(f'GOAWAY {ErrorCodes(int.from_bytes(payload[4:8], "big")).name}')
                elif kind == PING.value[0] and flags & 1 and payload == b'p' * 8:
                    answers.append('ignored')
                else:
                    continue
                if len(answers) == count:
                    answered.set()

    async with peer(origin) as port:
        async with HTTP2Client('127.0.0.1', port) as client:
            request = asyncio.ensure_future(client.request('GET', '/'))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(answered.wait(), 3)
            request.cancel()
            with contextlib.suppress(BaseException):
                await request
    return ', '.join(answers) or 'no answer'


async def test_the_client_ignores_frames_after_refusing_a_response():
    """Its own response checks reset as the core's do (§5.1)."""
    bad_status = _make_headers_frame(1, end_headers=True, fields=[(b':status', b'abc')])
    assert await as_client((bad_status, F(DATA, 0, 1, b'x')), 2) == (
        'RST_STREAM PROTOCOL_ERROR, ignored')


async def test_with_its_frame_cap_off_the_client_resets_only_the_stream(monkeypatch):
    """§4.2: past 2^14 but read in full, so a stream error suffices."""
    monkeypatch.setenv('BB_CLIENT_H2_MAX_FRAME_SIZE', '0')
    assert await as_client((F(DATA, 0, 1, b'\x00' * 16385),)) == 'RST_STREAM FRAME_SIZE_ERROR'


async def test_a_response_the_caller_walked_away_from_is_ignored():
    """§5.1: what the server still sends on an abandoned stream costs the
    connection nothing; the next request on it succeeds."""
    late = asyncio.Event()
    response = (_make_headers_frame(1, end_headers=True, fields=[(b':status', b'200')])
                + F(DATA, 1, 1, b'late'))

    async def origin(reader, writer):
        await reader.readexactly(24)
        writer.write(F(SETTINGS))
        await writer.drain()
        sid = 0
        while sid != 3:
            kind, _, sid, _ = await read_h2_frame(reader)
            if kind == HEADERS.value[0] and sid == 1:
                await late.wait()
                writer.write(response)
                await writer.drain()
        writer.write(response.replace(b'\x00\x00\x00\x01', b'\x00\x00\x00\x03'))
        await writer.drain()
        with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError):
            await reader.read()

    async with peer(origin) as port:
        async with HTTP2Client('127.0.0.1', port) as client:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(client.request('GET', '/'), 0.1)
            late.set()
            await asyncio.sleep(0.1)
            assert (await asyncio.wait_for(client.request('GET', '/'), 3)).status == 200


async def test_a_raw_stream_unregistered_early_ignores_what_still_arrives():
    """A WebSocket session that stops listening first (a handshake timeout,
    its own CLOSE) must not turn the server's last frames into errors."""
    unregistered = asyncio.Event()

    def on(sid: int, status: bytes = b'200', end: bool = False) -> bytes:
        block = _make_headers_frame(sid, end_headers=True, fields=[(b':status', status)])
        return block + F(DATA, 1, sid, b'') if end else block

    async def origin(reader, writer):
        await reader.readexactly(24)
        writer.write(F(SETTINGS))
        await writer.drain()
        await unregistered.wait()
        writer.write(on(5, end=True))
        while (await read_h2_frame(reader))[:3:2] != (HEADERS.value[0], 1):
            pass
        writer.write(on(1, end=True))
        await writer.drain()
        with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError):
            await reader.read()

    async with peer(origin) as port:
        async with HTTP2Client('127.0.0.1', port) as client:
            client.register_raw_stream(5)
            client.unregister_raw_stream(5)
            unregistered.set()
            await asyncio.sleep(0.1)
            assert (await asyncio.wait_for(client.request('GET', '/'), 3)).status == 200


@pytest.mark.parametrize('frames,answer', RULES.values(), ids=RULES.keys())
async def test_the_server_answers(frames, answer):
    assert await as_server(frames, answer.count(',') + 1) == answer


@pytest.mark.parametrize('frames,answer', RULES.values(), ids=RULES.keys())
async def test_the_client_answers(frames, answer):
    assert await as_client(frames, answer.count(',') + 1) == answer

"""Caps on how slowly, or how often, a peer may do something."""
import asyncio
import contextlib
import struct

import pytest

from blackbull.client import HTTP2Client
from blackbull.protocol.frame_types import ErrorCodes, FrameTypes
from blackbull.server import deadline
from blackbull.server.ws_codec import WSOpcode
from tests.conformance.http2._harness import _make_h2_frame
from tests.harness import peer, read_h2_frame

from ._drive import (client_frame, client_h2, client_http1, client_ws, h2, h2_data,
                     h2_request, h2_response, head, http1_seen, server_frame, ws)
from ._table import Row, check, check_under, table

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(30)]

_SLOW = {'BB_MIN_BODY_RATE': '1000', 'BB_MIN_BODY_RATE_GRACE': '0.2'}
_CLIENT_SLOW = {'BB_CLIENT_MIN_BODY_RATE': '1000', 'BB_CLIENT_MIN_BODY_RATE_GRACE': '0.2'}


@pytest.fixture(autouse=True)
def _fine_scanner(monkeypatch):
    monkeypatch.setattr(deadline, 'TICK_S', 0.01)


def _ping(n: int) -> bytes:
    return _make_h2_frame(FrameTypes.PING, 0, 0, bytes([n]) * 8)


def _priority(sid: int, payload: bytes) -> bytes:
    return _make_h2_frame(FrameTypes.PRIORITY, 0, sid, payload)


#: Each frame makes the client answer with RST_STREAM (§5.3.1 self-dependency,
#: §6.3 length, §5.1 a frame on a stream the peer reset).
_SELF_DEPENDENT = [_priority(sid, struct.pack('>IB', sid, 15)) for sid in (3, 5, 7)]
_SHORT_PRIORITY = [_priority(sid, b'\0' * 4) for sid in (3, 5, 7)]
_PROMISES = [_make_h2_frame(FrameTypes.PUSH_PROMISE, 0x4, 1,
                            struct.pack('>I', sid) + b'\x82\x84\x87\x41\x09localhost')
             for sid in (2, 4, 6)]
_AFTER_PEER_RESET = [_make_h2_frame(FrameTypes.RST_STREAM, 0, 1, struct.pack('>I', 8)),
                     *(h2_data(1, b'x') for _ in range(3))]


async def _client_deadline_refusals(streams: int = 3) -> str:
    """The first GOAWAY the client sends while *streams* of its own requests
    are each refused by BB_CLIENT_MIN_BODY_RATE, or 'no GOAWAY'."""
    sent: list[str] = []
    resets = 0

    async def origin(reader, writer):
        nonlocal resets
        await reader.readexactly(24)
        writer.write(_make_h2_frame(FrameTypes.SETTINGS))
        opened = []
        while len(opened) < streams:
            kind, _, sid, _ = await read_h2_frame(reader)
            if kind == FrameTypes.HEADERS.value[0]:
                opened.append(sid)
        for sid in opened:
            writer.write(h2_response(sid))
        for chunk in (b'a', b'b', b'c'):
            writer.write(b''.join(h2_data(sid, chunk) for sid in opened))
            await writer.drain()
            await asyncio.sleep(0.15)
        with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError, TimeoutError):
            while True:
                kind, _, _, payload = await asyncio.wait_for(read_h2_frame(reader), 1)
                if kind == FrameTypes.GOAWAY.value[0]:
                    sent.append(f'GOAWAY {ErrorCodes(int.from_bytes(payload[4:8], "big")).name}')
                elif kind == FrameTypes.RST_STREAM.value[0]:
                    resets += 1

    async with peer(origin) as port:
        async with HTTP2Client('127.0.0.1', port) as client:
            results = await asyncio.gather(
                *(client.request('GET', '/') for _ in range(streams)), return_exceptions=True)
            assert all(isinstance(r, Exception) for r in results), results
            await asyncio.sleep(0.5)
    assert resets >= streams, f'only {resets} refusals reached the wire'
    return sent[0] if sent else 'no GOAWAY'


ROWS = [
    Row('min_body_rate', 'http1', 'http.disconnect, closed', setting='min_body_rate',
        documented='disconnects and closes', env=_SLOW,
        provoke=lambda: http1_seen(head(b'Content-Length: 100', method=b'POST', target=b'/seen'),
                                   b'a', b'b', b'c', pause=0.15)),
    Row('min_body_rate', 'http2', 'RST_STREAM ENHANCE_YOUR_CALM', setting='min_body_rate',
        documented='ENHANCE_YOUR_CALM', env=_SLOW,
        provoke=lambda: h2(h2_request(1, path=b'/hold'), h2_data(1, b'a'), h2_data(1, b'b'),
                           h2_data(1, b'c'), pause=0.15)),
    Row('client_min_body_rate', 'http1', 'TimeoutError', setting='client_min_body_rate',
        documented='abandons its connection', env=_CLIENT_SLOW,
        provoke=lambda: client_http1(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n',
                                     b'a', b'b', b'c', pause=0.15, hold=True)),
    Row('client_min_body_rate', 'http2', 'RST_STREAM CANCEL', setting='client_min_body_rate',
        documented='RST_STREAM(CANCEL)', env=_CLIENT_SLOW,
        provoke=lambda: client_h2(h2_response(1), h2_data(1, b'a'), h2_data(1, b'b'),
                                  h2_data(1, b'c'), pause=0.15, report='sent')),
    Row('frame_rate', 'http2', 'GOAWAY ENHANCE_YOUR_CALM', setting='frame_rate_limit',
        documented='GOAWAY(ENHANCE_YOUR_CALM)', env={'BB_FRAME_RATE_LIMIT': '2'},
        provoke=lambda: h2(*(_ping(i) for i in range(3)))),
    Row('frame_rate', 'ws', 'close 1008', setting='frame_rate_limit',
        documented='WS close 1008', env={'BB_FRAME_RATE_LIMIT': '2'},
        provoke=lambda: ws(*(client_frame(b'', WSOpcode.PING) for _ in range(3)))),
    Row('frame_rate', 'http2', 'GOAWAY ENHANCE_YOUR_CALM', setting='frame_rate_limit',
        documented='GOAWAY(ENHANCE_YOUR_CALM)', env={'BB_FRAME_RATE_LIMIT': '2'},
        provoke=lambda: client_h2(*(_ping(i) for i in range(3)), report='goaway')),
    Row('frame_rate', 'http2', 'GOAWAY ENHANCE_YOUR_CALM', setting='frame_rate_limit',
        documented='client-emitted', env={'BB_FRAME_RATE_LIMIT': '2', **_CLIENT_SLOW},
        provoke=lambda: client_h2(*_SELF_DEPENDENT, report='goaway'),
        under=_client_deadline_refusals, under_answer='no GOAWAY'),
    Row('frame_rate', 'http2', 'GOAWAY ENHANCE_YOUR_CALM', setting='frame_rate_limit',
        documented='client-emitted', env={'BB_FRAME_RATE_LIMIT': '2'},
        provoke=lambda: client_h2(*_SHORT_PRIORITY, report='goaway')),
    Row('frame_rate', 'http2', 'GOAWAY ENHANCE_YOUR_CALM', setting='frame_rate_limit',
        documented='client-emitted', env={'BB_FRAME_RATE_LIMIT': '2'},
        provoke=lambda: client_h2(*_AFTER_PEER_RESET, report='goaway')),
    Row('frame_rate', 'http2', 'GOAWAY ENHANCE_YOUR_CALM', setting='frame_rate_limit',
        documented='client-emitted', env={'BB_FRAME_RATE_LIMIT': '2'},
        provoke=lambda: client_h2(*_PROMISES, report='goaway')),
    Row('frame_rate', 'ws', 'close 1008', setting='frame_rate_limit',
        documented='WS close 1008', env={'BB_FRAME_RATE_LIMIT': '2'},
        provoke=lambda: client_ws(*(server_frame(b'', WSOpcode.PING) for _ in range(3)))),
]


@table(*ROWS)
async def test_exceeding_a_rate_cap_is_answered_as_documented(row, monkeypatch, caplog):
    await check(row, monkeypatch, caplog)


@table(*ROWS, under=True)
async def test_traffic_within_a_rate_cap_is_not_recorded(row, monkeypatch, caplog):
    await check_under(row, monkeypatch, caplog)

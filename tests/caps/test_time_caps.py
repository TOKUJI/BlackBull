"""Caps on how long one wait, or one phase, may take."""
import asyncio
import base64
import contextlib
import hashlib

import pytest

from blackbull.client import HTTP1Client, HTTP2Client, WebSocketClient
from blackbull.protocol.frame_types import FrameTypes
from blackbull.server import deadline
from tests.conformance.http2._harness import _make_h2_frame, _make_headers_frame, h2_connect
from tests.harness import peer, served

from ._drive import (_WS_GUID, _h2_answer, client_h2, client_http1, h2, h2_request, h2_response, head,
                     http1, http1_seen, make_app)
from ._table import Row, check, check_under, table

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(30)]


@pytest.fixture(autouse=True)
def _fine_scanner(monkeypatch):
    """Each bound here is a fraction of a second; the default tick would lag it."""
    monkeypatch.setattr(deadline, 'TICK_S', 0.01)


async def _unread_response() -> str:
    """A client that asks for a large response and stops reading."""
    async with served(make_app()) as s:
        wire = await s.wire()
        await wire.send(head(target=b'/big'))
        await asyncio.sleep(1.0)
        received = 0
        with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError, TimeoutError):
            while chunk := await asyncio.wait_for(wire.reader.read(1 << 20), 5):
                received += len(chunk)
        return 'truncated' if received < (64 << 20) else 'complete'


async def _block_on_a_stream_we_reset() -> str:
    """A field block opened on a stream the server reset, then silence: the
    block is still decoded (RFC 9113 §4.3), so the head clock still runs."""
    async with served(make_app()) as s:
        wire = await h2_connect(s)
        await wire.send(h2_request(1)
                        + _make_h2_frame(FrameTypes.PRIORITY, 0, 1, (1).to_bytes(4, 'big') + b'\x0f'))
        assert await _h2_answer(wire) == 'RST_STREAM PROTOCOL_ERROR'
        await wire.send(_make_headers_frame(1, end_headers=False, fields=[(b'x-a', b'1')]))
        return await asyncio.wait_for(_h2_answer(wire), 5)


async def _unwindowed_h2_response() -> str:
    """An HTTP/2 client that grants no stream window: the response waits for
    credit that never comes."""
    async with served(make_app()) as s:
        wire = await s.wire()
        await wire.send(b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'
                        + _make_h2_frame(FrameTypes.SETTINGS, 0, 0,
                                         b'\x00\x04' + (0).to_bytes(4, 'big'))
                        + h2_request(1, method=b'GET', path=b'/big', end_stream=True))
        return await _h2_answer(wire)


async def _client_write_stall(client_cls, protocol: str) -> str:
    """A peer that accepts the connection and never reads what it is sent."""
    async def origin(reader, writer):
        if protocol == 'http2':
            writer.write(_make_h2_frame(FrameTypes.SETTINGS))
            await writer.drain()
        await asyncio.sleep(5)

    async with peer(origin) as port:
        try:
            async with client_cls('127.0.0.1', port) as client:
                await client.request('POST', '/', body=b'x' * (64 << 20))
        except Exception as exc:
            return type(exc).__name__
    return 'ok'


async def _client_ws_write_stall() -> str:
    """A WebSocket peer that completes the handshake and never reads."""
    async def origin(reader, writer):
        request = await reader.readuntil(b'\r\n\r\n')
        key = next(line.split(b':', 1)[1].strip() for line in request.split(b'\r\n')
                   if line.lower().startswith(b'sec-websocket-key:'))
        writer.write(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
                     b'Connection: Upgrade\r\nSec-WebSocket-Accept: '
                     + base64.b64encode(hashlib.sha1(key + _WS_GUID).digest()) + b'\r\n\r\n')
        await writer.drain()
        await asyncio.sleep(4)

    async with peer(origin) as port:
        async with WebSocketClient('127.0.0.1', port) as client:
            session = await client.connect('/')
            try:
                await session.send_bytes(b'x' * (16 << 20))   # masking 64 MiB takes seconds
            except Exception as exc:
                return type(exc).__name__
    return 'ok'


async def _h2_request_deadline_during_handler_and_body() -> str:
    assert await h2(h2_request(1, method=b'GET', path=b'/hold', end_stream=True)) == 'RST_STREAM CANCEL'
    return await h2(h2_request(1, method=b'POST', path=b'/seen'))


ROWS = [
    Row('header_timeout', 'http1', '408', setting='header_timeout',
        env={'BB_HEADER_TIMEOUT': '0.2'},
        provoke=lambda: http1(b'GET / HTTP/1.1\r\nHost: x\r\n')),
    Row('header_timeout', 'http2', 'GOAWAY ENHANCE_YOUR_CALM', setting='header_timeout',
        documented='GOAWAY ENHANCE_YOUR_CALM', env={'BB_HEADER_TIMEOUT': '0.2'},
        provoke=lambda: h2(_make_headers_frame(1, end_headers=False, fields=[
            (b':method', b'GET'), (b':path', b'/')]))),
    Row('header_timeout', 'http2', 'GOAWAY ENHANCE_YOUR_CALM', setting='header_timeout',
        documented='GOAWAY ENHANCE_YOUR_CALM', env={'BB_HEADER_TIMEOUT': '0.2'},
        provoke=_block_on_a_stream_we_reset),
    Row('body_timeout', 'http1', 'http.disconnect, closed', setting='body_timeout',
        documented='http.disconnect', env={'BB_BODY_TIMEOUT': '0.2'},
        provoke=lambda: http1_seen(head(b'Content-Length: 10', method=b'POST', target=b'/seen')
                                   + b'ab'),
        under=lambda: http1(head(b'Content-Length: 10', method=b'POST') + b'x' * 10),
        under_answer='200',
        xfail='BLA-558: the connection stays open'),
    Row('request_timeout', 'http1', '408', setting='request_timeout',
        env={'BB_REQUEST_TIMEOUT': '0.2'},
        provoke=lambda: http1(head(target=b'/hold'))),
    Row('request_timeout', 'http2', 'RST_STREAM CANCEL', setting='request_timeout',
        documented='RST_STREAM(CANCEL)', env={'BB_REQUEST_TIMEOUT': '0.2', 'BB_BODY_TIMEOUT': '0.05'},
        provoke=_h2_request_deadline_during_handler_and_body),
    Row('write_timeout', 'transport', 'truncated', setting='write_timeout',
        documented='socket drain', env={'BB_WRITE_TIMEOUT': '0.2'},
        provoke=_unread_response, under=lambda: http1(head()), under_answer='200'),
    Row('write_timeout', 'http2', 'RST_STREAM INTERNAL_ERROR', setting='write_timeout',
        documented='flow-control wait', env={'BB_WRITE_TIMEOUT': '0.2'},
        provoke=_unwindowed_h2_response,
        xfail='BLA-557: sends HEADERS 200 twice and 500 before the reset'),
    Row('client_head_timeout', 'http1', 'TimeoutError', setting='client_head_timeout',
        env={'BB_CLIENT_HEAD_TIMEOUT': '0.2'},
        provoke=lambda: client_http1(b'', hold=True)),
    Row('client_head_timeout', 'http2', 'TimeoutError', setting='client_head_timeout',
        env={'BB_CLIENT_HEAD_TIMEOUT': '0.2'},
        provoke=lambda: client_h2()),
    Row('client_body_timeout', 'http1', 'TimeoutError', setting='client_body_timeout',
        env={'BB_CLIENT_BODY_TIMEOUT': '0.2'},
        provoke=lambda: client_http1(b'HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nab', hold=True)),
    Row('client_body_timeout', 'http2', 'TimeoutError', setting='client_body_timeout',
        env={'BB_CLIENT_BODY_TIMEOUT': '0.2'},
        provoke=lambda: client_h2(h2_response(1))),
    Row('client_write_timeout', 'http1', 'ConnectionResetError', setting='client_write_timeout',
        documented='client socket drain', env={'BB_CLIENT_WRITE_TIMEOUT': '0.2'},
        provoke=lambda: _client_write_stall(HTTP1Client, 'http1')),
    Row('client_write_timeout', 'ws', 'ConnectionResetError', setting='client_write_timeout',
        documented='client socket drain', env={'BB_CLIENT_WRITE_TIMEOUT': '0.2'},
        provoke=_client_ws_write_stall),
    Row('client_write_timeout', 'http2', 'FlowControlStalled', setting='client_write_timeout',
        documented='flow-control credit wait', env={'BB_CLIENT_WRITE_TIMEOUT': '0.2'},
        provoke=lambda: _client_write_stall(HTTP2Client, 'http2')),
]


@table(*ROWS)
async def test_exceeding_a_time_cap_is_answered_as_documented(row, monkeypatch, caplog):
    await check(row, monkeypatch, caplog)



@table(*ROWS, under=True)
async def test_time_within_a_cap_is_not_recorded(row, monkeypatch, caplog):
    await check_under(row, monkeypatch, caplog)

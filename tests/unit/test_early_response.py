"""A response sent while the request body is still arriving reaches the client.

The server may answer before it has read the whole request (an early error, a
size limit).  On HTTP/1.1 it then reads the rest of the body, up to a bound, so
the connection survives and the client's upload is never reset under the
response.  On HTTP/2 it ends the request with RST_STREAM(NO_ERROR) once the
handler has finished (RFC 9113 §8.1), and ignores what the peer had in flight,
up to the window it held.
"""
import asyncio
import time

import pytest
from hpack import Encoder

from blackbull.client import HTTP2Client
from tests.harness import peer, read_h2_frame, served

pytestmark = pytest.mark.asyncio

DATA, HEADERS, PRIORITY, RST_STREAM, SETTINGS, PING, GOAWAY = 0, 1, 2, 3, 4, 6, 7
END_STREAM, END_HEADERS, ACK = 0x1, 0x4, 0x1
NO_ERROR, FLOW_CONTROL_ERROR = 0, 3
_PREFACE = b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'
_KIB = 1024


async def _early(scope, receive, send):
    """Answer without reading the request body."""
    await send({'type': 'http.response.start', 'status': 413,
                'headers': [(b'content-length', b'4')]})
    await send({'type': 'http.response.body', 'body': b'nope'})


def _settings(monkeypatch, **env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    from blackbull.env import reset_settings_cache
    reset_settings_cache()


# ---------------------------------------------------------------------------
# HTTP/1.1
# ---------------------------------------------------------------------------

def _post(length: int) -> bytes:
    return (b'POST / HTTP/1.1\r\nHost: x\r\nContent-Length: '
            + str(length).encode() + b'\r\n\r\n')


async def test_h1_an_unread_body_within_the_bound_is_drained_and_the_connection_kept():
    body = b'x' * (200 * _KIB)
    async with served(_early) as s:
        w = await s.wire()
        await w.send(_post(len(body)) + body[:16 * _KIB])
        assert (await w.response()).status == 413
        await w.send(body[16 * _KIB:] + _post(0))
        assert (await w.response()).status == 413, 'the connection was not kept'


async def test_h1_a_client_still_uploading_past_the_bound_reads_the_response():
    body = b'x' * (256 * _KIB + 32 * _KIB)
    async with served(_early) as s:
        w = await s.wire()
        upload = asyncio.create_task(w.send(_post(len(body)) + body))
        try:
            assert (await w.response()).status == 413
            assert await w.closed()
        finally:
            upload.cancel()
            await asyncio.gather(upload, return_exceptions=True)


async def test_h1_the_drain_is_bounded_by_the_keep_alive_timeout(monkeypatch):
    _settings(monkeypatch, BB_KEEP_ALIVE_TIMEOUT='0.5')
    async with served(_early) as s:
        w = await s.wire()
        await w.send(_post(200 * _KIB))
        assert (await w.response()).status == 413
        started = time.monotonic()

        async def trickle():
            for _ in range(200):        # 1 KiB per 0.1 s: above the rate floor
                await w.send(b'x' * _KIB)
                await asyncio.sleep(0.1)

        task = asyncio.create_task(trickle())
        try:
            assert await w.closed()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert time.monotonic() - started < 3.0


# ---------------------------------------------------------------------------
# HTTP/2
# ---------------------------------------------------------------------------

def _frame(kind: int, flags: int, stream_id: int, payload: bytes = b'') -> bytes:
    return (len(payload).to_bytes(3, 'big') + bytes([kind, flags])
            + stream_id.to_bytes(4, 'big') + payload)


class _H2:
    def __init__(self, wire):
        self.wire = wire
        self.encoder = Encoder()

    async def open(self) -> None:
        await self.wire.send(_PREFACE + _frame(SETTINGS, 0, 0))

    async def request(self, sid: int, *, end_stream: bool = False) -> None:
        block = self.encoder.encode([(b':method', b'POST'), (b':path', b'/'),
                                     (b':scheme', b'http'), (b':authority', b'x')])
        await self.wire.send(_frame(
            HEADERS, END_HEADERS | (END_STREAM if end_stream else 0), sid, block))

    async def data(self, sid: int, size: int, *, frames: int = 1) -> None:
        await self.wire.send(_frame(DATA, 0, sid, b'x' * size) * frames)

    async def until(self, kind: int, sid: int | None = None) -> list:
        """Frames read up to and including the first *kind* frame on *sid*."""
        seen = []
        while True:
            frame = await read_h2_frame(self.wire.reader)
            seen.append((frame[0], frame[1], frame[2], frame[3], time.monotonic()))
            if frame[0] == kind and (sid is None or frame[2] == sid):
                return seen


def _rsts(frames, sid):
    return [int.from_bytes(f[3], 'big') for f in frames
            if f[0] == RST_STREAM and f[2] == sid]


async def _h2(s) -> _H2:
    h2 = _H2(await s.wire())
    await h2.open()
    return h2


async def test_h2_a_complete_response_ends_an_unfinished_request_with_no_error():
    async with served(_early) as s:
        h2 = await _h2(s)
        await h2.request(1)
        await h2.data(1, 100)
        frames = await h2.until(RST_STREAM, 1)
        ends = [f for f in frames if f[2] == 1 and f[1] & END_STREAM
                and f[0] in (HEADERS, DATA)]
        assert ends, 'the reset came before the response ended'
        assert _rsts(frames, 1) == [NO_ERROR]


async def test_h2_a_finished_request_is_not_reset():
    async with served(_early) as s:
        h2 = await _h2(s)
        await h2.request(1, end_stream=True)
        await h2.request(3)
        frames = await h2.until(RST_STREAM, 3)
        assert _rsts(frames, 1) == []


async def test_h2_data_in_flight_after_the_reset_is_ignored():
    async with served(_early) as s:
        h2 = await _h2(s)
        await h2.request(1)
        await h2.until(RST_STREAM, 1)
        await h2.data(1, 16 * _KIB, frames=3)
        await h2.wire.send(_frame(PING, 0, 0, b'p' * 8))
        frames = await h2.until(PING)
        assert [f[0] for f in frames if f[0] in (RST_STREAM, GOAWAY)] == []
        await h2.request(3, end_stream=True)
        await h2.until(HEADERS, 3)


async def test_h2_data_past_the_window_after_the_reset_is_a_flow_control_error():
    async with served(_early) as s:
        h2 = await _h2(s)
        await h2.request(1)
        await h2.until(RST_STREAM, 1)
        await h2.data(1, 16 * _KIB, frames=5)        # 80 KiB > the 64 KiB window
        frames = await h2.until(GOAWAY)
        assert int.from_bytes(frames[-1][3][4:8], 'big') == FLOW_CONTROL_ERROR


async def test_h2_ending_requests_early_does_not_spend_the_reset_budget(monkeypatch):
    _settings(monkeypatch, BB_FRAME_RATE_LIMIT='3', BB_FRAME_RATE_WINDOW='30')
    async with served(_early) as s:
        h2 = await _h2(s)
        for sid in range(1, 21, 2):
            await h2.request(sid)
            frames = await h2.until(RST_STREAM, sid)
            assert [f for f in frames if f[0] == GOAWAY] == []


async def test_h2_the_reset_waits_for_the_handler_to_finish():
    async def lingering(scope, receive, send):
        await _early(scope, receive, send)
        await asyncio.sleep(0.3)

    async with served(lingering) as s:
        h2 = await _h2(s)
        await h2.request(1)
        frames = await h2.until(RST_STREAM, 1)
        end = next(f[4] for f in frames if f[2] == 1 and f[1] & END_STREAM)
        assert frames[-1][4] - end >= 0.25


async def test_h2_a_flow_controlled_response_is_not_cut_short():
    body = b'y' * (100 * _KIB)

    async def large(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-length', str(len(body)).encode())]})
        await send({'type': 'http.response.body', 'body': body})

    async with served(large) as s:
        h2 = await _h2(s)
        await h2.request(1)
        received = 0
        while True:
            kind, flags, sid, payload = await read_h2_frame(h2.wire.reader)
            if sid == 1 and kind == RST_STREAM:
                break
            if sid == 1 and kind == DATA:
                received += len(payload)
                await h2.wire.send(
                    _frame(8, 0, 0, len(payload).to_bytes(4, 'big'))
                    + _frame(8, 0, 1, len(payload).to_bytes(4, 'big')))
        assert received == len(body)


async def test_h2_a_client_bounds_data_after_its_reset_by_the_window():
    """The same bound on the client role, which resets a stream in error."""
    goaway: asyncio.Future = asyncio.get_running_loop().create_future()

    async def origin(reader, writer):
        await reader.readexactly(len(_PREFACE))
        writer.write(_frame(SETTINGS, 0, 0))
        while not goaway.done():
            kind, _, sid, payload = await read_h2_frame(reader)
            if kind == HEADERS:
                writer.write(_frame(PRIORITY, 0, sid, b'\x00' * 4))   # §6.3: 5 octets
            elif kind == RST_STREAM:
                writer.write(_frame(DATA, 0, sid, b'x' * 16 * _KIB) * 5)
            elif kind == GOAWAY:
                goaway.set_result(int.from_bytes(payload[4:8], 'big'))

    async with peer(origin) as port:
        async with HTTP2Client('127.0.0.1', port) as client:
            request = asyncio.ensure_future(client.request('GET', '/'))
            try:
                assert await asyncio.wait_for(goaway, 3) == FLOW_CONTROL_ERROR
            finally:
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)

"""DATA handed to a writer spends credit even while its drain is suspended."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from http import HTTPStatus

import pytest

from blackbull.native import NativeResponse
from blackbull.client.exceptions import StreamReset
from blackbull.client.http2 import HTTP2Client
from blackbull.client.websocket_h2 import WebSocketH2Session
from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import ErrorCodes
from blackbull.server.http2_ws import HTTP2WSWriter
from blackbull.server.sender import AbstractWriter, ConnectionWindow, HTTP2Sender

pytestmark = pytest.mark.asyncio


class _Writer(AbstractWriter):
    """Record complete frames before blocking the first DATA write's drain."""

    def __init__(self):
        self.frames: list[tuple[int, int, int, bytes]] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.error: Exception | None = None

    async def write(self, data: bytes) -> None:
        offset = 0
        has_data = False
        while offset < len(data):
            length = int.from_bytes(data[offset:offset + 3], 'big')
            end = offset + 9 + length
            assert end <= len(data)
            kind, flags = data[offset + 3:offset + 5]
            sid = int.from_bytes(data[offset + 5:offset + 9], 'big')
            self.frames.append((kind, flags, sid, data[offset + 9:end]))
            has_data |= kind == 0 and length > 0
            offset = end
        if has_data and not self.entered.is_set():
            self.entered.set()
            await self.release.wait()
            if self.error is not None:
                raise self.error

    def body(self, sid: int | None = None) -> bytes:
        return b''.join(payload for kind, _, stream, payload in self.frames
                        if kind == 0 and (sid is None or sid == stream))


async def _turn() -> None:
    """Run the already-ready callbacks to their next suspension, without a timer."""
    done = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(done.set_result, None)
    await done


@asynccontextmanager
async def _rig(credit=5, stream_credit=100):
    writer = _Writer()
    window = ConnectionWindow(credit)
    factory = FrameFactory()
    senders = [HTTP2Sender(writer, factory, sid, conn_window=window,
                          initial_window=stream_credit,
                          flow_control_timeout=0.0) for sid in (1, 3)]
    tasks = []

    def start(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    try:
        yield writer, window, senders, start
    finally:
        tasks.extend(s._auto_flush_task for s in senders
                     if s._auto_flush_task is not None)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _emit(sender, mode, body=b'hello'):
    if mode == 'data':
        await sender._write_data(body, end_stream=True)
    elif mode == 'bytes':
        await sender(body)
    elif mode == 'dict':
        await sender({'type': 'http.response.start', 'status': 200})
        await sender({'type': 'http.response.body', 'body': body})
    elif mode == 'native':
        await sender(NativeResponse(status=200, header=[], body=body))
    elif mode == 'ws':
        await HTTP2WSWriter(sender).write(body)
    else:
        await sender(NativeResponse(status=200, header=[], expects_trailers=True))
        await sender(NativeResponse(body=body, more_body=True))
        if mode == 'trailers':
            await sender(NativeResponse(trailers=[(b'grpc-status', b'0')]))
        else:
            task = sender._auto_flush_task
            if task is not None:
                await task


@pytest.mark.parametrize('mode', ['data', 'bytes', 'dict', 'native',
                                 'trailers', 'auto_flush', 'ws'])
async def test_concurrent_stream_cannot_reuse_credit_during_drain(mode):
    async with _rig() as (writer, window, senders, start):
        first = start(_emit(senders[0], mode))
        await writer.entered.wait()
        second = start(_emit(senders[1], mode))
        await _turn()
        assert writer.body() == b'hello'
        assert not second.done()

        # Releasing transport backpressure is not a WINDOW_UPDATE.
        writer.release.set()
        await first
        await _turn()
        assert writer.body() == b'hello'
        window.size += 5
        for sender in senders:
            sender.wake_window()
        await second
        assert writer.body(1) == writer.body(3) == b'hello'


@pytest.mark.parametrize('mode', ['data', 'bytes', 'dict', 'native',
                                 'trailers', 'auto_flush', 'ws'])
async def test_sufficient_credit_allows_parallel_writes(mode):
    async with _rig(credit=10) as (writer, _, senders, start):
        first = start(_emit(senders[0], mode))
        await writer.entered.wait()
        second = start(_emit(senders[1], mode))
        await second
        assert not first.done(), 'a drain on one stream must not serialize all streams'
        assert writer.body(1) == writer.body(3) == b'hello'
        writer.release.set()
        await first
        await _turn()
        assert writer.body(1) == writer.body(3) == b'hello'


async def test_cancelled_drain_does_not_refund_bytes_already_handed_to_writer():
    async with _rig() as (writer, window, senders, start):
        first = start(senders[0]._write_data(b'hello-extra', end_stream=True))
        await writer.entered.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = start(_emit(senders[1], 'data'))
        await _turn()
        assert writer.body() == b'hello'
        assert not second.done()
        window.size += 5
        senders[1].wake_window()
        await second
        assert writer.body(3) == b'hello'


async def test_window_update_during_drain_grants_only_its_increment():
    async with _rig() as (writer, window, senders, start):
        first = start(_emit(senders[0], 'data'))
        await writer.entered.wait()
        window.size += 2
        second = start(_emit(senders[1], 'data'))
        await _turn()
        assert writer.body(1) == b'hello'
        assert writer.body(3) == b'he'
        writer.release.set()
        await first
        window.size += 3
        senders[1].wake_window()
        await second
        assert writer.body(3) == b'hello'


async def test_smaller_stream_window_does_not_block_other_stream():
    async with _rig(credit=100, stream_credit=2) as (writer, _, senders, start):
        first = start(_emit(senders[0], 'data'))
        await writer.entered.wait()
        second = start(_emit(senders[1], 'data'))
        await _turn()
        assert writer.body(1) == writer.body(3) == b'he'
        writer.release.set()
        await _turn()
        for sender in senders:
            sender.window_update(3)
        await asyncio.gather(first, second)
        assert writer.body(1) == writer.body(3) == b'hello'


async def test_settings_reduction_is_applied_to_already_committed_stream_credit():
    async with _rig(credit=100, stream_credit=5) as (writer, _, senders, start):
        sender = senders[0]
        first = start(sender._write_data(b'hello-more', end_stream=True))
        await writer.entered.wait()
        sender.adjust_initial_window(-5)
        sender.window_update(5)
        second = start(sender._write_data(b'!', end_stream=False))
        await _turn()
        assert writer.body() == b'hello'
        second.cancel()
        await asyncio.gather(second, return_exceptions=True)
        writer.release.set()
        await _turn()
        assert writer.body() == b'hello'
        sender.window_update(5)
        await first
        assert writer.body() == b'hello-more'


@pytest.mark.parametrize('error', [RuntimeError('writer failed'),
                                 ConnectionResetError('peer gone')])
async def test_failed_drain_does_not_reuse_committed_credit(error):
    async with _rig() as (writer, window, senders, start):
        writer.error = error
        first = start(_emit(senders[0], 'data'))
        await writer.entered.wait()
        writer.release.set()
        result = await asyncio.gather(first, return_exceptions=True)
        if isinstance(error, RuntimeError):
            assert result == [error]
        else:
            assert result == [None]
        # The caller cannot infer that a failed drain delivered no bytes.
        assert window.size == 0
        second = start(_emit(senders[1], 'data'))
        await _turn()
        assert writer.body() == b'hello'
        if isinstance(error, RuntimeError):
            window.size += 5
            senders[1].wake_window()
            await second
            assert writer.body(3) == b'hello'


@pytest.mark.parametrize('stream_credit', [0, -5])
async def test_control_and_empty_end_stream_do_not_need_data_credit(stream_credit):
    async with _rig(credit=0, stream_credit=stream_credit) as (writer, _, senders, _):
        sender = senders[0]
        await sender(sender._factory.rst_stream(3, ErrorCodes.CANCEL))
        await sender._write_data(b'', end_stream=True)
        assert [kind for kind, _, _, _ in writer.frames] == [3, 0]
        assert writer.frames[-1] == (0, 1, 1, b'')


async def test_partial_send_respects_maximum_frame_payload():
    async with _rig(credit=100) as (writer, _, senders, _):
        writer.release.set()
        sender = senders[0]
        sender.max_frame_size = 2
        await sender._write_data(b'hello', end_stream=True)
        assert writer.frames == [(0, 0, 1, b'he'), (0, 0, 1, b'll'), (0, 1, 1, b'o')]


async def test_second_body_takes_buffer_ownership_before_waiting_for_drain():
    async with _rig(credit=10) as (writer, _, senders, start):
        sender = senders[0]

        async def respond():
            await sender(NativeResponse(status=200, header=[], expects_trailers=True))
            await sender(NativeResponse(body=b'hello', more_body=True))
            await sender(NativeResponse(body=b'-more'))
            await sender(NativeResponse(trailers=[(b'grpc-status', b'0')]))

        task = start(respond())
        await writer.entered.wait()
        await _turn()
        assert writer.body() == b'hello', 'auto-flush must not send the same chunk'
        writer.release.set()
        await task
        assert writer.body() == b'hello-more'


async def test_client_stream_reset_during_upload_does_not_return_send_credit():
    writer = _Writer()
    client = HTTP2Client('localhost', 1)
    client._writer = writer
    client._conn_window.size = 5
    first = asyncio.create_task(client.request('POST', '/', body=b'hello-more'))
    second = None
    try:
        await writer.entered.wait()
        client._on_rst_stream(client.frame_factory.rst_stream(1, ErrorCodes.CANCEL))
        with pytest.raises(StreamReset):
            await first
        second = asyncio.create_task(client.request('POST', '/', body=b'world'))
        await _turn()
        await _turn()
        assert writer.body() == b'hello'
        client._on_window_update(client.frame_factory.window_update(0, 5))
        await _turn()
        await _turn()
        assert writer.body(3) == b'world'
        client._complete(3)
        await second
    finally:
        tasks = [first] + ([second] if second is not None else [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.__aexit__(None, None, None)


async def test_websocket_close_waits_for_credit_and_ends_stream():
    writer = _Writer()
    writer.release.set()
    client = HTTP2Client('localhost', 1)
    client._writer = writer
    client._control_sender = HTTP2Sender(writer, client.frame_factory, 0)
    client._conn_window.size = 0
    session = WebSocketH2Session(client, 1, client.register_raw_stream(1))
    session._disconnect_seen = True
    task = asyncio.create_task(session.close())
    try:
        await _turn()
        assert writer.body() == b''
        assert not task.done()
        client._on_window_update(client.frame_factory.window_update(0, 8))
        await task
        assert len(writer.body(1)) == 8  # masked CLOSE with the two-byte status code
        assert writer.frames[-1][:3] == (0, 1, 1)
        assert client._conn_window.size == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await session._recipient.shutdown()
        await client.__aexit__(None, None, None)


async def test_trailers_waiting_for_credit_preserve_connection_header_order():
    async with _rig() as (writer, window, senders, start):
        writer.release.set()
        sender, other = senders

        async def respond():
            await sender(NativeResponse(status=200, header=[], expects_trailers=True))
            await sender(NativeResponse(body=b'hello', more_body=True))
            # A different stream consumes the connection credit before the
            # trailers take the buffered chunk, without yielding to auto-flush.
            await other(b'world')
            await sender(NativeResponse(trailers=[(b'grpc-status', b'0')]))

        task = start(respond())
        await _turn()
        third = HTTP2Sender(writer, sender._factory, 5, conn_window=window)
        await third.send_response_headers(HTTPStatus.OK, [(b'grpc-status', b'0')])
        window.size += 5
        sender.wake_window()
        await task
        decoded = [(sid, sender._factory.decoder.decode(payload, raw=True))
                   for kind, _, sid, payload in writer.frames if kind == 1]
        third_headers = next(fields for sid, fields in decoded if sid == 5)
        assert (b':status', b'200') in third_headers
        assert (b'grpc-status', b'0') in third_headers
        assert decoded[-1] == (1, [(b'grpc-status', b'0')])


async def test_buffered_body_obeys_reduced_peer_frame_size():
    async with _rig(credit=40000, stream_credit=40000) as (writer, _, senders, _):
        writer.release.set()
        sender = senders[0]
        sender.apply_settings(max_frame_size=32768)
        payload = b'x' * 20000
        await sender(NativeResponse(status=200, header=[], expects_trailers=True))
        await sender(NativeResponse(body=payload, more_body=True))
        sender.apply_settings(max_frame_size=16384)
        await sender(NativeResponse(trailers=[(b'grpc-status', b'0')]))
        assert writer.body(1) == payload
        assert all(len(body) <= 16384 for kind, _, _, body in writer.frames if kind == 0)


@pytest.mark.parametrize('outcome', ['cancel', 'timeout'])
async def test_websocket_close_cleans_up_when_flow_control_wait_ends(outcome):
    writer = _Writer()
    client = HTTP2Client('localhost', 1)
    client._writer = writer
    client._conn_window.size = 0
    session = WebSocketH2Session(client, 1, client.register_raw_stream(1))
    session._disconnect_seen = True
    session._sender._flow_control_timeout = 0.01 if outcome == 'timeout' else 0.0
    task = asyncio.create_task(session.close())
    try:
        await _turn()
        assert not task.done()
        if outcome == 'cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        assert writer.body() == b''
        assert 1 not in client._raw_streams
        assert 1 not in client._senders
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.__aexit__(None, None, None)

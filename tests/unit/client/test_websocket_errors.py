import asyncio
from base64 import b64encode
from contextlib import asynccontextmanager
from hashlib import sha1
from pathlib import Path
import ssl
import struct

from hpack import Encoder
import pytest

from blackbull.client import (ClientConnection, ClientError, ProtocolError, WebSocketClient,
                              WebSocketH2Client)
from blackbull.client.http2 import HTTP2Client
from blackbull.native import NativeResponse
from blackbull.protocol.frame_types import FrameTypes
from blackbull.server import recipient
from blackbull.server.connection_protocol import BufferReader
from blackbull.server.http2_ws import HTTP2StreamReader
from blackbull.server.sender import AsyncioWriter, HTTP2Sender
from tests.conformance.http2._harness import _make_h2_frame
from tests.harness import Wire, peer, read_h2_frame


@asynccontextmanager
async def _session(transport, frames, replies=None, close_received=None, *,
                   close_session=True, end_streams=None, h2_window=None):
    completed = asyncio.get_running_loop().create_future()
    completed.add_done_callback(lambda future: future.exception())
    context = client_context = None
    if transport == 'h2':
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        certs = Path(__file__).resolve().parents[2]
        context.load_cert_chain(certs / 'cert.pem', certs / 'key.pem')
        context.set_alpn_protocols(['h2'])
        client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_context.check_hostname = False
        client_context.verify_mode = ssl.CERT_NONE
        client_context.set_alpn_protocols(['h2'])

    async def origin(reader, writer):
        try:
            wire = Wire(reader, writer)
            if transport == 'h1':
                request = await wire.recv_until()
                key = next(line.split(b':', 1)[1].strip()
                           for line in request.split(b'\r\n')
                           if line.lower().startswith(b'sec-websocket-key:'))
                accept = b64encode(sha1(
                    key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
                await wire.send(b'HTTP/1.1 101 Switching Protocols\r\n'
                                b'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                                b'Sec-WebSocket-Accept: ' + accept + b'\r\n\r\n'
                                + frames)
            else:
                assert await wire.recv_exactly(24) == b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'
                settings = b'\x00\x08\x00\x00\x00\x01'
                if h2_window is not None:
                    settings += b'\x00\x04' + h2_window.to_bytes(4, 'big')
                await wire.send(_make_h2_frame(FrameTypes.SETTINGS, payload=settings))
                while True:
                    kind, flags, sid, _ = await read_h2_frame(reader)
                    if kind == 4 and not flags & 1:
                        await wire.send(_make_h2_frame(FrameTypes.SETTINGS, 1))
                    if kind == 1:
                        break
                await wire.send(_make_h2_frame(FrameTypes.HEADERS, 4, sid,
                    Encoder().encode([(b':status', b'200')]))
                    + _make_h2_frame(FrameTypes.DATA, 0, sid, frames))
            outgoing = b''
            if close_received is not None:
                if transport == 'h1':
                    head = await wire.recv_exactly(2)
                    assert head[0] == 0x88
                    outgoing = head + await wire.recv_exactly(4 + (head[1] & 127))
                else:
                    while True:
                        kind, flags, stream, payload = await read_h2_frame(reader)
                        outgoing += _make_h2_frame(FrameTypes(bytes([kind])), flags, stream, payload)
                        if kind == 0 and stream == sid and payload[:1] == b'\x88':
                            break
                close_received.set()
            outgoing += await wire.recv_all()
            if replies is not None:
                if transport == 'h1':
                    replies.extend(outgoing)
                else:
                    while outgoing:
                        size = int.from_bytes(outgoing[:3], 'big')
                        if outgoing[3] == 0 and int.from_bytes(outgoing[5:9], 'big') == sid:
                            replies.extend(outgoing[9:9 + size])
                            if outgoing[4] & 1 and end_streams is not None:
                                end_streams.append(outgoing[9:9 + size])
                        outgoing = outgoing[9 + size:]
            completed.set_result(None)
        except Exception as exc:
            completed.set_exception(exc)

    client_type = WebSocketClient if transport == 'h1' else WebSocketH2Client
    async with peer(origin, ssl=context) as port:
        async with client_type('127.0.0.1', port, ssl=client_context) as client:
            session = await client.connect('/ws')
            try:
                yield session
            finally:
                if close_session:
                    await session.close(drain_timeout=0.05)
    await asyncio.wait_for(completed, 1)


async def _receive(session):
    return await asyncio.wait_for(session.receive(), 1)


def _assert_disconnect(transport, event, code):
    if transport == 'h1':
        assert event == {'type': 'websocket.disconnect', 'code': code}
    else:
        assert event == (8, code.to_bytes(2, 'big'))


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['h1', 'h2'])
@pytest.mark.parametrize('frames,code,message', [
    pytest.param(b'\x82\x05', 1009, 'payload', id='frame-limit'),
    pytest.param(b'\x82\x03abc', 1009, 'message', id='message-limit'),
    pytest.param(b'\x02\x02ab\x80\x01c', 1009, 'message', id='fragmented-message-limit'),
    pytest.param(b'\x81\x01\xff', 1007, 'UTF-8', id='invalid-utf8'),
    pytest.param(b'\x09\x00', 1002, 'fragmented control', id='fragmented-control'),
])
async def test_receive_protocol_errors_are_client_errors(monkeypatch, transport,
                                                        frames, code, message):
    monkeypatch.setenv('BB_CLIENT_WS_MAX_FRAME_PAYLOAD', '4')
    monkeypatch.setenv('BB_CLIENT_WS_MAX_MESSAGE_SIZE', '2')
    async with _session(transport, frames) as session:
        with pytest.raises(ClientError) as caught:
            await _receive(session)
        error = caught.value
        assert type(error) is ProtocolError
        assert type(error.__cause__) is recipient.ProtocolError
        assert error.__cause__.close_code == code
        assert message.lower() in str(error).lower()
        assert str(error) == str(error.__cause__)
        _assert_disconnect(transport, await _receive(session), code)


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['h1', 'h2'])
async def test_valid_messages_at_limits_and_close_keep_their_result(monkeypatch, transport):
    monkeypatch.setenv('BB_CLIENT_WS_MAX_FRAME_PAYLOAD', '4')
    monkeypatch.setenv('BB_CLIENT_WS_MAX_MESSAGE_SIZE', '4')
    async with _session(transport, b'\x81\x04text\x02\x02ab\x80\x02cd'
                        b'\x88\x02\x03\xe8') as session:
        text, binary = await _receive(session), await _receive(session)
        if transport == 'h1':
            assert text == {'type': 'websocket.receive', 'text': 'text', 'bytes': None}
            assert binary == {'type': 'websocket.receive', 'text': None, 'bytes': b'abcd'}
        else:
            assert text == (1, b'text')
            assert binary == (2, b'abcd')
        _assert_disconnect(transport, await _receive(session), 1000)


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['h1', 'h2'])
async def test_receive_timeout_is_not_translated(transport):
    async with _session(transport, b'') as session:
        with pytest.raises(TimeoutError):
            if transport == 'h1':
                await asyncio.wait_for(session.receive(), 0.01)
            else:
                await session.receive(timeout=0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['h1', 'h2'])
async def test_receive_cancellation_is_not_translated(transport):
    async with _session(transport, b'') as session:
        receiving = asyncio.create_task(session.receive())
        await asyncio.sleep(0)
        receiving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await receiving


def _control_replies(data):
    replies = []
    while data:
        opcode, length = data[0] & 15, data[1] & 127
        assert data[1] & 128, 'client control replies must be masked'
        assert length <= 125
        key, payload = data[2:6], data[6:6 + length]
        assert len(key) == 4 and len(payload) == length
        replies.append((opcode, bytes(b ^ key[i % 4] for i, b in enumerate(payload))))
        data = data[6 + length:]
    return replies


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['h1', 'h2'])
@pytest.mark.parametrize('first,payload', [(0x81, b'x'), (0x82, b'x'),
    (0x89, b'x'), (0x8a, b'x'), (0x88, b'\x03\xe8')])
@pytest.mark.parametrize('header_only', [False, True])
async def test_masked_server_frames_are_rejected_before_body(transport, first,
                                                            payload, header_only):
    frames = bytes([first, 128 | len(payload)])
    if not header_only:
        frames += b'\x00' * 4 + payload
    replies = bytearray()
    async with _session(transport, frames, replies) as session:
        with pytest.raises(ProtocolError, match='masked server frame') as error:
            await _receive(session)
        assert error.value.__cause__.close_code == 1002
        _assert_disconnect(transport, await _receive(session), 1002)
    outgoing = _control_replies(replies)
    assert outgoing and outgoing[0] == (8, b'\x03\xea')
    assert all(opcode == 8 for opcode, _ in outgoing)


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['h1', 'h2'])
@pytest.mark.parametrize('length_code', [126, 127])
async def test_masked_server_header_does_not_wait_for_extended_length(transport, length_code):
    async with _session(transport, bytes([0x82, 128 | length_code])) as session:
        with pytest.raises(ProtocolError, match='masked server frame'):
            await _receive(session)
        _assert_disconnect(transport, await _receive(session), 1002)


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['h1', 'h2'])
@pytest.mark.parametrize('frames,reply,code', [
    (b'\x89\x01p\x81\x01x\x88\x02\x03\xe8', (10, b'p'), 1000),
    (b'\x83\x00', (8, b'\x03\xea'), 1002),
    (b'\x82\x7e\x10\x00', (8, b'\x03\xf1'), 1009),
])
async def test_client_control_replies_keep_mask_on_every_path(monkeypatch, transport, frames,
                                                            reply, code):
    monkeypatch.setenv('BB_CLIENT_WS_MAX_FRAME_PAYLOAD', '1024')
    replies = bytearray()
    async with _session(transport, frames, replies) as session:
        if code == 1009:
            with pytest.raises(ProtocolError):
                await _receive(session)
        elif reply[0] == 10:
            event = await _receive(session)
            assert event['text'] == 'x' if transport == 'h1' else event == (1, b'x')
        _assert_disconnect(transport, await _receive(session), code)
    outgoing = _control_replies(replies)
    assert outgoing and outgoing[0] == reply


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['h1', 'h2'])
@pytest.mark.parametrize('operation', ['send_text', 'send_bytes', 'ping'])
@pytest.mark.parametrize('closed_by', ['local', 'peer', 'read-ahead'])
async def test_no_application_frames_after_close(transport, operation, closed_by):
    if transport == 'h2' and operation == 'ping':
        pytest.skip('H2 session has no public ping method')
    frames = b'\x81\x01x\x88\x02\x03\xe8' if closed_by != 'local' else b''
    replies = bytearray()
    echoed = asyncio.Event() if closed_by == 'read-ahead' else None
    async with _session(transport, frames, replies, echoed) as session:
        if closed_by == 'local':
            await session.close(drain_timeout=0.01)
        else:
            await _receive(session)
            if closed_by == 'peer':
                await _receive(session)
            else:
                await asyncio.wait_for(echoed.wait(), 1)
        argument = 'late' if operation == 'send_text' else b'late'
        with pytest.raises(RuntimeError, match='closed'):
            await getattr(session, operation)(argument)
    assert all(opcode == 8 for opcode, _ in _control_replies(replies))


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['h1', 'h2'])
async def test_normal_close_cleans_up_in_its_calling_task(monkeypatch, transport):
    cleanup_tasks = []
    original = recipient.WebSocketRecipient.shutdown

    async def shutdown(self):
        cleanup_tasks.append(asyncio.current_task())
        await original(self)

    monkeypatch.setattr(recipient.WebSocketRecipient, 'shutdown', shutdown)
    async with _session(transport, b'') as session:
        await session.close(drain_timeout=0.0)
        assert cleanup_tasks == [asyncio.current_task()]


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['h1', 'h2'])
@pytest.mark.parametrize('cancellations', [0, 1, 2])
async def test_concurrent_close_waits_for_cleanup(monkeypatch, transport, cancellations):
    entered, finish = asyncio.Event(), asyncio.Event()
    original = recipient.WebSocketRecipient.shutdown

    async def shutdown(self):
        entered.set()
        await finish.wait()
        await original(self)

    monkeypatch.setattr(recipient.WebSocketRecipient, 'shutdown', shutdown)
    async with _session(transport, b'') as session:
        first = asyncio.create_task(session.close(drain_timeout=0.0))
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(RuntimeError, match='closed'):
            await session.send_text('late')
        second = asyncio.create_task(session.close())
        try:
            await asyncio.sleep(0)
            for _ in range(cancellations):
                second.cancel()
                await asyncio.sleep(0)
            assert not second.done()
        finally:
            finish.set()
            results = await asyncio.gather(first, second, return_exceptions=True)
        assert results[0] is None
        if cancellations:
            assert isinstance(results[1], asyncio.CancelledError)
        else:
            assert results[1] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['h1', 'h2'])
async def test_repeated_cancellation_still_finishes_cleanup(monkeypatch, transport):
    entered, finish, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = recipient.WebSocketRecipient.shutdown

    async def shutdown(self):
        entered.set()
        await finish.wait()
        await original(self)
        cleaned.set()

    monkeypatch.setattr(recipient.WebSocketRecipient, 'shutdown', shutdown)
    async with _session(transport, b'') as session:
        closing = asyncio.create_task(session.close(drain_timeout=0.0))
        await asyncio.wait_for(entered.wait(), 1)
        try:
            for _ in range(2):
                closing.cancel()
                await asyncio.sleep(0)
        finally:
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await closing
        assert cleaned.is_set()
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['h1', 'h2'])
async def test_cancelled_reader_join_is_resumed_after_transport_cleanup(monkeypatch, transport):
    reader_cancelled, release_reader = asyncio.Event(), asyncio.Event()
    transport_waiting, release_transport = asyncio.Event(), asyncio.Event()
    retried_shutdown = asyncio.Event()
    reader_type = BufferReader if transport == 'h1' else HTTP2StreamReader
    original_read = reader_type.readexactly
    original_shutdown = recipient.WebSocketRecipient.shutdown
    original_wait_closed = ClientConnection.wait_closed
    shutdown_calls = 0

    async def read(self, n):
        try:
            return await original_read(self, n)
        except asyncio.CancelledError:
            reader_cancelled.set()
            while not release_reader.is_set():
                try:
                    await release_reader.wait()
                except asyncio.CancelledError:
                    pass
            raise

    async def shutdown(self):
        nonlocal shutdown_calls
        shutdown_calls += 1
        if shutdown_calls > 1:
            retried_shutdown.set()
        await original_shutdown(self)

    async def wait_closed(self):
        transport_waiting.set()
        await release_transport.wait()
        await original_wait_closed(self)

    monkeypatch.setattr(reader_type, 'readexactly', read)
    monkeypatch.setattr(recipient.WebSocketRecipient, 'shutdown', shutdown)
    if transport == 'h1':
        monkeypatch.setattr(ClientConnection, 'wait_closed', wait_closed)
    async with _session(transport, b'\x81\x01x') as session:
        await _receive(session)
        closing = asyncio.create_task(session.close(drain_timeout=0.0))
        try:
            await asyncio.wait_for(reader_cancelled.wait(), 1)
            closing.cancel()
            if transport == 'h1':
                await asyncio.wait_for(transport_waiting.wait(), 1)
                closing.cancel()
                await asyncio.sleep(0)
                release_transport.set()
            await asyncio.wait_for(retried_shutdown.wait(), 1)
            await asyncio.sleep(0)
            assert not closing.done()
        finally:
            release_reader.set()
            release_transport.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(closing, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['h1', 'h2'])
async def test_invalid_close_code_still_releases_reader(monkeypatch, transport):
    cleaned = asyncio.Event()
    original = recipient.WebSocketRecipient.shutdown

    async def shutdown(self):
        await original(self)
        cleaned.set()

    monkeypatch.setattr(recipient.WebSocketRecipient, 'shutdown', shutdown)
    async with _session(transport, b'') as session:
        with pytest.raises((OverflowError, ValueError, struct.error)):
            await session.close(code=65536)
        assert cleaned.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['h1', 'h2'])
async def test_client_context_releases_session_reader(monkeypatch, transport):
    cleaned = asyncio.Event()
    original = recipient.WebSocketRecipient.shutdown

    async def shutdown(self):
        await original(self)
        cleaned.set()

    monkeypatch.setattr(recipient.WebSocketRecipient, 'shutdown', shutdown)
    async with _session(transport, b'\x81\x01x', close_session=False) as session:
        await _receive(session)
    assert cleaned.is_set()
    with pytest.raises(RuntimeError, match='closed'):
        await session.send_text('late')


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['h1', 'h2'])
@pytest.mark.parametrize('failure', ['write-error', 'cancel-write'])
async def test_close_write_failure_still_releases_reader(monkeypatch, transport, failure):
    writing, cleaned = asyncio.Event(), asyncio.Event()
    writer_type = AsyncioWriter if transport == 'h1' else HTTP2Sender
    method = 'write' if transport == 'h1' else '__call__'
    original_write = getattr(writer_type, method)
    original_shutdown = recipient.WebSocketRecipient.shutdown

    async def write(self, message, *args, **kwargs):
        data = message.body if isinstance(message, NativeResponse) else message
        if isinstance(data, bytes) and data[:1] == b'\x88':
            writing.set()
            if failure == 'write-error':
                raise OSError('write failed')
            await asyncio.Future()
        return await original_write(self, message, *args, **kwargs)

    async def shutdown(self):
        await original_shutdown(self)
        cleaned.set()

    monkeypatch.setattr(writer_type, method, write)
    monkeypatch.setattr(recipient.WebSocketRecipient, 'shutdown', shutdown)
    async with _session(transport, b'') as session:
        closing = asyncio.create_task(session.close(drain_timeout=0.0))
        await asyncio.wait_for(writing.wait(), 1)
        if failure == 'cancel-write':
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
        else:
            await closing
        assert cleaned.is_set()
        await session.close()


@pytest.mark.asyncio
async def test_h2_context_unregisters_stream_even_if_reader_cleanup_fails(monkeypatch):
    unregistered = []
    original_shutdown = recipient.WebSocketRecipient.shutdown
    original_unregister = HTTP2Client.unregister_raw_stream

    async def shutdown(self):
        await original_shutdown(self)
        raise OSError('shutdown failed')

    def unregister(self, stream_id):
        original_unregister(self, stream_id)
        unregistered.append(stream_id)

    monkeypatch.setattr(recipient.WebSocketRecipient, 'shutdown', shutdown)
    monkeypatch.setattr(HTTP2Client, 'unregister_raw_stream', unregister)
    with pytest.raises(OSError, match='shutdown failed'):
        async with _session('h2', b'\x81\x01x', close_session=False) as session:
            await _receive(session)
    assert unregistered == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['h1', 'h2'])
@pytest.mark.parametrize('initiator', ['local', 'peer'])
async def test_close_writes_one_masked_close_and_one_h2_end(transport, initiator):
    replies, ends = bytearray(), []
    async with _session(transport, b'\x88\x02\x03\xe8', replies,
                        end_streams=ends) as session:
        if initiator == 'peer':
            await _receive(session)
        await session.close()
        await session.close()
    assert _control_replies(replies) == [(8, b'\x03\xe8')]
    assert len(ends) == (1 if transport == 'h2' else 0)
    if transport == 'h2' and initiator == 'peer':
        assert ends == [b'']


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['h1', 'h2'])
@pytest.mark.parametrize('write_error', [False, True])
async def test_peer_close_blocks_sends_before_echo_finishes(monkeypatch, transport, write_error):
    entered, finish = asyncio.Event(), asyncio.Event()
    writes, replies, ends = [], bytearray(), []
    writer_type = AsyncioWriter if transport == 'h1' else HTTP2Sender
    method = 'write' if transport == 'h1' else '__call__'
    original = getattr(writer_type, method)

    async def write(self, message, *args, **kwargs):
        data = message.body if isinstance(message, NativeResponse) else message
        if isinstance(data, bytes) and data[:1] == b'\x88':
            writes.append(data)
            entered.set()
            await finish.wait()
            if write_error:
                raise OSError('echo failed')
        return await original(self, message, *args, **kwargs)

    monkeypatch.setattr(writer_type, method, write)
    async with _session(transport, b'\x88\x02\x03\xe8', replies, end_streams=ends) as session:
        receiving = asyncio.create_task(session.receive())
        closing = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            with pytest.raises(RuntimeError, match='closed'):
                await session.send_text('late')
            closing = asyncio.create_task(session.close())
            await asyncio.sleep(0)
            assert not closing.done() and len(writes) == 1
        finally:
            finish.set()
            await asyncio.gather(receiving, *([closing] if closing else []))
    assert len(writes) == 1
    assert _control_replies(replies) == ([] if write_error else [(8, b'\x03\xe8')])
    assert len(ends) == (1 if transport == 'h2' else 0)


@pytest.mark.asyncio
async def test_h2_close_under_zero_send_window_can_be_cancelled(monkeypatch):
    writing = asyncio.Event()
    original = HTTP2Sender.__call__

    async def write(self, message, *args, **kwargs):
        if isinstance(message, NativeResponse) and message.body and message.body[:1] == b'\x88':
            writing.set()
        return await original(self, message, *args, **kwargs)

    monkeypatch.setattr(HTTP2Sender, '__call__', write)
    async with _session('h2', b'\x88\x02\x03\xe8', h2_window=0) as session:
        receiving = asyncio.create_task(session.receive())
        closing = None
        try:
            await asyncio.wait_for(writing.wait(), 1)
            closing = asyncio.create_task(session.close())
            await asyncio.sleep(0)
            with pytest.raises(RuntimeError, match='closed'):
                await asyncio.wait_for(session.send_bytes(b'late'), 0.1)
        finally:
            if closing is not None:
                closing.cancel()
                await asyncio.gather(closing, return_exceptions=True)
            receiving.cancel()
            await asyncio.gather(receiving, return_exceptions=True)

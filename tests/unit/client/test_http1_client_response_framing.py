"""RFC 9112 response framing through the HTTP/1.1 client API."""
from __future__ import annotations

import asyncio
from base64 import b64encode
from hashlib import sha1
from http import HTTPMethod

import pytest

from blackbull.client import ClientConnection
from blackbull.client.exceptions import (ConnectionError, ProtocolError,
                                         ResponseTooLarge)
from blackbull.client.http1 import Fate, HTTP1Client, HTTP1ResponseRecipient
from blackbull.client.websocket import WebSocketClient
from blackbull.server.sender import AbstractWriter

from tests.harness import feed, fed


def _Reader(data: bytes):
    return fed(data).reader


class _Writer(AbstractWriter):
    def __init__(self) -> None:
        self.data = bytearray()

    async def write(self, data: bytes) -> None:
        self.data.extend(data)


class _RawWriter(ClientConnection):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1

    async def wait_closed(self) -> None:
        return None


class _FailingWriter(_Writer):
    """Accept the request head, then fail once transmission has begun."""

    async def write(self, data: bytes) -> None:
        if self.data:
            raise RuntimeError('injected write failure')
        await super().write(data)


def _client(reader) -> HTTP1Client:
    client = HTTP1Client('example.test', 80)
    client._reader = reader
    client._writer = _Writer()
    return client


async def _assert_abandoned(client: HTTP1Client) -> None:
    with pytest.raises(ConnectionError, match='abandoned'):
        await client.request('GET', '/after')


async def _assert_usable(client: HTTP1Client) -> None:
    """The next request reaches the wire and finds the peer gone."""
    with pytest.raises(ConnectionError, match='closed before response'):
        await client.request('GET', '/after')


def _head(status: int, reason: str = 'OK', headers: bytes = b'', *,
          version: bytes = b'HTTP/1.1') -> bytes:
    return (version + f' {status} {reason}\r\n'.encode() + headers
            + b'\r\n')


def _chunked_body(*chunks: bytes) -> bytes:
    return b''.join(b'%x\r\n%s\r\n' % (len(chunk), chunk)
                    for chunk in chunks) + b'0\r\n\r\n'


@pytest.mark.asyncio
async def test_head_request_ignores_misleading_framing_and_preserves_bytes():
    reader = _Reader(
        _head(200, headers=b'Content-Length: 5\r\n')
        + b'HTTP/1.1 204 No Content\r\n\r\n'
    )
    client = _client(reader)

    response = await client.request('HEAD', '/')

    assert response.body == b''
    assert reader.peek().startswith(b'HTTP/1.1 204')


@pytest.mark.parametrize('status,reason', [
    (100, 'Continue'),
    (199, 'Early Hints'),
    (204, 'No Content'),
    (304, 'Not Modified'),
])
@pytest.mark.asyncio
async def test_bodyless_status_ignores_misleading_framing(status, reason):
    reader = _Reader(
        _head(status, reason,
              b'Content-Length: 5\r\nTransfer-Encoding: gzip, chunked\r\n')
        + b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok'
    )

    response = await HTTP1ResponseRecipient().receive(
        reader, method='GET', skip_interim=False)

    assert response.status == status
    assert response.body == b''
    assert reader.peek().startswith(b'HTTP/1.1 200')


@pytest.mark.parametrize('wire', [
    _head(204, 'No Content'),
    _head(200, headers=b'Content-Length: 0\r\n'),
    _head(200, headers=b'Transfer-Encoding: chunked\r\n') + b'0\r\nx: 1\r\n\r\n',
])
@pytest.mark.asyncio
async def test_a_response_never_ends_on_its_head_over_http1(wire):
    """``ended_on_head`` is an HTTP/2 frame fact; HTTP/1.1 has no frames."""
    response = await _client(_Reader(wire)).request('GET', '/')

    assert response.ended_on_head is False


@pytest.mark.asyncio
async def test_receive_close_delimited_body_until_eof_and_marks_nonreusable():
    reader = _Reader(_head(200) + b'close-delimited')
    recipient = HTTP1ResponseRecipient(request_method='GET')

    response = await recipient.receive(reader)

    assert response.body == b'close-delimited'
    assert recipient.fate is not Fate.REUSE


@pytest.mark.asyncio
async def test_stream_close_delimited_body_until_eof_and_marks_nonreusable():
    reader = _Reader(_head(200) + b'close-delimited')
    recipient = HTTP1ResponseRecipient(request_method='GET')

    chunks = [chunk async for chunk in recipient.stream(reader)]

    assert b''.join(chunks) == b'close-delimited'
    assert recipient.fate is not Fate.REUSE


@pytest.mark.asyncio
async def test_successful_connect_response_is_bodyless_and_nonreusable():
    reader = _Reader(_head(200) + b'tunnel-bytes')

    response = await HTTP1ResponseRecipient(request_method='CONNECT').receive(
        reader)

    assert response.body == b''
    assert reader.peek() == b'tunnel-bytes'
    assert response.status == 200


@pytest.mark.asyncio
async def test_successful_connect_retires_http_without_closing_raw_transport():
    reader = _Reader(_head(200) + b'tunnel-bytes')
    client = _client(reader)
    raw = _RawWriter()
    client._raw_writer = raw

    response = await client.request('CONNECT', '/')

    assert response.body == b''
    assert raw.close_calls == 0
    assert reader.peek() == b'tunnel-bytes'
    with pytest.raises(ConnectionError):
        await client.request('GET', '/')


@pytest.mark.asyncio
async def test_successful_connect_stream_preserves_raw_transport():
    reader = _Reader(_head(200) + b'stream-tunnel')
    client = _client(reader)
    raw = _RawWriter()
    client._raw_writer = raw

    chunks = [chunk async for chunk in client.stream('CONNECT', '/')]

    assert chunks == []
    assert raw.close_calls == 0
    assert reader.peek() == b'stream-tunnel'
    with pytest.raises(ConnectionError):
        await client.request('GET', '/')


@pytest.mark.asyncio
async def test_successful_connect_read_response_preserves_raw_transport():
    reader = _Reader(_head(200) + b'read-tunnel')
    client = _client(reader)
    raw = _RawWriter()
    client._raw_writer = raw

    response = await client.read_response(request_method='CONNECT')

    assert response.body == b''
    assert raw.close_calls == 0
    assert reader.peek() == b'read-tunnel'
    with pytest.raises(ConnectionError):
        await client.request('GET', '/')


@pytest.mark.asyncio
async def test_close_delimited_buffering_enforces_total_before_accumulating_excess(
        monkeypatch):
    monkeypatch.setenv('BB_CLIENT_BODY_MAX_TOTAL', '4')
    reader = _Reader(_head(200) + b'12345not-retained')
    recipient = HTTP1ResponseRecipient(request_method='GET')

    with pytest.raises(ResponseTooLarge):
        await recipient.receive(reader)

    assert reader.peek() == b'not-retained'
    assert recipient.fate is Fate.BROKEN


@pytest.mark.parametrize('method', [
    'head', 'HeAd', b'head', b'HeAd',
    'connect', 'CoNnEcT', b'connect', b'CoNnEcT',
])
@pytest.mark.asyncio
async def test_noncanonical_methods_are_not_head_or_connect(method):
    reader = _Reader(
        _head(200, headers=b'Content-Length: 4\r\n') + b'body'
        + _head(200, headers=b'Content-Length: 2\r\n') + b'ok')
    recipient = HTTP1ResponseRecipient(request_method=method)

    response = await recipient.receive(reader)

    assert response.body == b'body'
    assert reader.peek().startswith(b'HTTP/1.1 200')
    assert recipient.fate is Fate.REUSE


@pytest.mark.parametrize('method, bodyless', [
    (HTTPMethod.HEAD, True),
    (HTTPMethod.CONNECT, True),
    ('HEAD', True),
    (b'HEAD', True),
])
@pytest.mark.asyncio
async def test_canonical_head_and_connect_types_keep_exact_semantics(
        method, bodyless):
    reader = _Reader(_head(200, headers=b'Content-Length: 4\r\n')
                     + b'body')

    response = await HTTP1ResponseRecipient(request_method=method).receive(reader)

    assert (response.body == b'') is bodyless
    if bodyless:
        assert reader.peek() == b'body'


@pytest.mark.asyncio
async def test_malformed_transfer_encoding_poisoned_http1client():
    reader = _Reader(_head(200, headers=b'Transfer-Encoding: gzip;bad\r\n')
                     + b'body')
    client = _client(reader)

    with pytest.raises(ProtocolError):
        await client.request('GET', '/')

    with pytest.raises(ConnectionError, match='abandoned'):
        await client.request('GET', '/second')


@pytest.mark.asyncio
async def test_self_delimited_response_can_be_reused_by_http1client():
    reader = _Reader(
        _head(200, headers=b'Content-Length: 3\r\n') + b'one'
        + _head(200, headers=b'Content-Length: 3\r\n') + b'two'
    )
    client = _client(reader)

    first = await client.request('GET', '/')
    second = await client.request('GET', '/')

    assert first.body == b'one'
    assert second.body == b'two'
    await _assert_usable(client)


@pytest.mark.asyncio
async def test_http1client_request_marks_close_delimited_connection_unusable():
    reader = _Reader(_head(200) + b'body')
    client = _client(reader)

    response = await client.request('GET', '/')

    assert response.body == b'body'
    with pytest.raises(ConnectionError):
        await client.request('GET', '/second')


@pytest.mark.asyncio
async def test_http1client_stream_threads_head_method_to_framing():
    reader = _Reader(
        _head(200, headers=b'Content-Length: 4\r\n')
        + b'HTTP/1.1 204 No Content\r\n\r\n'
    )
    client = _client(reader)

    chunks = [chunk async for chunk in client.stream('HEAD', '/')]

    assert chunks == []
    assert reader.peek().startswith(b'HTTP/1.1 204')


@pytest.mark.asyncio
async def test_http1client_stream_marks_close_delimited_connection_unusable():
    reader = _Reader(_head(200) + b'body')
    client = _client(reader)

    chunks = [chunk async for chunk in client.stream('GET', '/')]

    assert b''.join(chunks) == b'body'
    with pytest.raises(ConnectionError):
        await client.request('GET', '/second')


@pytest.mark.asyncio
async def test_partial_declared_stream_leaves_body_unread_and_poison_closes():
    proto = fed(_head(200, headers=b'Content-Length: 6\r\n') + b'ab', eof=False)
    reader = proto.reader
    client = _client(reader)
    raw = _RawWriter()
    client._raw_writer = raw
    stream = client.stream('GET', '/')

    assert await anext(stream) == b'ab'
    feed(proto, b'cdef')
    assert reader.peek() == b'cdef'
    sent_before_refusal = bytes(client._writer.data)  # type: ignore[union-attr]
    with pytest.raises(ConnectionError):
        await client.request('GET', '/concurrent')
    assert bytes(client._writer.data) == sent_before_refusal  # type: ignore[union-attr]

    await stream.aclose()

    await _assert_abandoned(client)
    assert raw.close_calls == 1
    assert reader.peek() == b'cdef'


@pytest.mark.parametrize('body', [
    _head(200, headers=b'Transfer-Encoding: chunked\r\n')
    + _chunked_body(b'one', b'two'),
    _head(200) + b'one',
])
@pytest.mark.asyncio
async def test_partial_stream_owns_reader_and_aclose_poison_connection(body):
    reader = _Reader(body)
    client = _client(reader)
    stream = client.stream('GET', '/')

    assert await anext(stream) == b'one'
    with pytest.raises(ConnectionError):
        await client.request('GET', '/concurrent')

    await stream.aclose()

    assert client._active_response is None
    with pytest.raises(ConnectionError, match='abandoned'):
        await client.request('GET', '/second')


@pytest.mark.asyncio
async def test_breaking_async_for_keeps_connection_busy_until_stream_is_closed():
    reader = _Reader(_head(200, headers=b'Content-Length: 6\r\n') + b'abcdef')
    client = _client(reader)
    stream = client.stream('GET', '/')

    async for chunk in stream:
        assert chunk == b'abcdef'
        break

    with pytest.raises(ConnectionError):
        await client.read_response()
    await stream.aclose()
    await _assert_abandoned(client)


@pytest.mark.asyncio
async def test_partial_stream_cancellation_poison_connection():
    reader = fed(_head(200, headers=b'Content-Length: 10\r\n') + b'first', eof=False).reader
    client = _client(reader)
    stream = client.stream('GET', '/')

    assert await anext(stream) == b'first'
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert client._active_response is None
    with pytest.raises(ConnectionError, match='abandoned'):
        await client.request('GET', '/second')


@pytest.mark.asyncio
async def test_stream_cancellation_while_waiting_for_chunk_size_poison_closes():
    reader = fed(_head(200, headers=b'Transfer-Encoding: chunked\r\n')
                 + b'3\r\none\r\n', eof=False).reader
    client = _client(reader)
    raw = _RawWriter()
    client._raw_writer = raw
    stream = client.stream('GET', '/')

    assert await anext(stream) == b'one'
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    await _assert_abandoned(client)
    assert raw.close_calls == 1
    assert reader.peek() == b''


@pytest.mark.asyncio
async def test_stream_cancellation_while_waiting_for_close_eof_poison_closes():
    reader = fed(_head(200) + b'one', eof=False).reader
    client = _client(reader)
    raw = _RawWriter()
    client._raw_writer = raw
    stream = client.stream('GET', '/')

    assert await anext(stream) == b'one'
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    await _assert_abandoned(client)
    assert raw.close_calls == 1
    assert reader.peek() == b''


@pytest.mark.asyncio
async def test_complete_self_delimited_stream_remains_reusable():
    reader = _Reader(
        _head(200, headers=b'Content-Length: 3\r\n') + b'one'
        + _head(200, headers=b'Transfer-Encoding: chunked\r\n')
        + _chunked_body(b'two'))
    client = _client(reader)

    assert b''.join([chunk async for chunk in client.stream('GET', '/')]) == b'one'
    assert b''.join([chunk async for chunk in client.stream('GET', '/')]) == b'two'

    await _assert_usable(client)


@pytest.mark.asyncio
async def test_read_response_accepts_originating_method():
    reader = _Reader(_head(200, headers=b'Content-Length: 4\r\n') + b'next')
    client = _client(reader)

    response = await client.read_response(request_method='HEAD')

    assert response.body == b''
    assert reader.peek() == b'next'


@pytest.mark.asyncio
async def test_read_response_keeps_low_level_fault_injection_reusable_on_error():
    reader = _Reader(
        b'not-an-http-response\r\n\r\n'
        + _head(200, headers=b'Content-Length: 2\r\n') + b'ok')
    client = _client(reader)

    with pytest.raises(ProtocolError):
        await client.read_response()

    response = await client.read_response()

    assert response.body == b'ok'
    await _assert_usable(client)


@pytest.mark.asyncio
async def test_read_response_refuses_prebuffered_head_after_context_exit():
    reader = _Reader(_head(204) + b'following-bytes')
    client = _client(reader)
    raw = _RawWriter()
    client._raw_writer = raw

    await client.__aexit__(None, None, None)

    with pytest.raises(ConnectionError, match='HTTP1Client context is closed'):
        await client.read_response()
    assert reader.peek() == _head(204) + b'following-bytes'
    assert raw.close_calls == 1


@pytest.mark.asyncio
async def test_read_response_refuses_after_successful_close_delimited_response():
    reader = _Reader(_head(200) + b'body')
    client = _client(reader)

    response = await client.read_response()

    assert response.body == b'body'
    with pytest.raises(ConnectionError):
        await client.read_response()


@pytest.mark.parametrize('value', [
    b'\x0bvalue', b'value\x0b',
    b'\x0cvalue', b'value\x0c',
    b'value\rcontrol', b'value\ncontrol', b'value\x7fcontrol',
])
@pytest.mark.asyncio
async def test_response_field_prohibited_controls_retire_connection(value):
    reader = _Reader(
        b'HTTP/1.1 200 OK\r\nX-Test: ' + value
        + b'\r\nContent-Length: 0\r\n\r\n')
    client = _client(reader)
    raw = _RawWriter()
    client._raw_writer = raw

    with pytest.raises(ProtocolError):
        await client.request('GET', '/')

    await _assert_abandoned(client)
    assert raw.close_calls == 1


@pytest.mark.parametrize('api', ['request', 'stream'])
@pytest.mark.asyncio
async def test_preflight_content_length_failure_leaves_connection_reusable(api):
    reader = _Reader(_head(200, headers=b'Content-Length: 2\r\n') + b'ok')
    client = _client(reader)
    raw = _RawWriter()
    client._raw_writer = raw
    bad_headers = [(b'content-length', b'2')]

    if api == 'request':
        with pytest.raises(ProtocolError):
            await client.request('POST', '/', headers=bad_headers, body=b'x')
    else:
        body_stream = client.stream(
            'POST', '/', headers=bad_headers, body=b'x')
        with pytest.raises(ProtocolError):
            await anext(body_stream)

    assert bytes(client._writer.data) == b''  # type: ignore[union-attr]
    assert raw.close_calls == 0
    assert (await client.request('GET', '/next')).body == b'ok'


@pytest.mark.parametrize('api', ['request', 'stream'])
@pytest.mark.asyncio
async def test_preflight_invalid_connection_option_leaves_connection_reusable(api):
    reader = _Reader(_head(200, headers=b'Content-Length: 2\r\n') + b'ok')
    client = _client(reader)
    raw = _RawWriter()
    client._raw_writer = raw
    invalid_headers = [(b'connection', b'close invalid')]

    if api == 'request':
        with pytest.raises(ProtocolError):
            await client.request('GET', '/', headers=invalid_headers)
    else:
        body_stream = client.stream('GET', '/', headers=invalid_headers)
        with pytest.raises(ProtocolError):
            await anext(body_stream)

    assert bytes(client._writer.data) == b''  # type: ignore[union-attr]
    assert raw.close_calls == 0
    assert (await client.request('GET', '/next')).body == b'ok'


@pytest.mark.parametrize('api', ['request', 'stream'])
@pytest.mark.asyncio
async def test_failure_after_request_head_was_sent_retires_connection(api):
    reader = _Reader(_head(200, headers=b'Content-Length: 0\r\n'))
    client = _client(reader)
    writer = _FailingWriter()
    raw = _RawWriter()
    client._writer = writer
    client._raw_writer = raw

    if api == 'request':
        with pytest.raises(RuntimeError, match='injected write failure'):
            await client.request('POST', '/', body=b'x')
    else:
        body_stream = client.stream('POST', '/', body=b'x')
        with pytest.raises(RuntimeError, match='injected write failure'):
            await anext(body_stream)

    assert bytes(writer.data).startswith(b'POST / HTTP/1.1\r\n')
    await _assert_abandoned(client)
    assert raw.close_calls == 1


async def _consume_api(client: HTTP1Client, api: str, *,
                       headers=()) -> bytes:
    if api == 'request':
        return (await client.request('GET', '/', headers=headers)).body
    return b''.join([chunk async for chunk in
                     client.stream('GET', '/', headers=headers)])


@pytest.mark.parametrize('api', ['request', 'stream'])
@pytest.mark.parametrize('policy', ['inbound-close', 'outbound-close',
                                    'http10-default'])
@pytest.mark.asyncio
async def test_persistence_retirement_policies(api, policy):
    response_headers = b'Content-Length: 3\r\n'
    version = b'HTTP/1.1'
    request_headers = ()
    if policy == 'inbound-close':
        response_headers += b'Connection: keep-alive\r\nConnection: cLoSe\r\n'
    elif policy == 'outbound-close':
        request_headers = [(b'connection', b'foo, ClOsE')]
    else:
        version = b'HTTP/1.0'
    reader = _Reader(
        _head(200, headers=response_headers, version=version) + b'one'
        + _head(200, headers=b'Content-Length: 3\r\n') + b'two')
    client = _client(reader)
    raw = _RawWriter()
    client._raw_writer = raw

    assert await _consume_api(client, api, headers=request_headers) == b'one'

    assert raw.close_calls == 1
    assert reader.peek().startswith(b'HTTP/1.1 200')
    with pytest.raises(ConnectionError):
        await client.request('GET', '/next')


@pytest.mark.parametrize('api', ['request', 'stream'])
@pytest.mark.asyncio
async def test_http10_repeated_mixed_case_keep_alive_allows_reuse(api):
    reader = _Reader(
        _head(200, headers=(b'Content-Length: 3\r\n'
                            b'Connection: foo\r\n'
                            b'Connection: KeEp-AlIvE, BAR\r\n'),
              version=b'HTTP/1.0') + b'one'
        + _head(200, headers=b'Content-Length: 3\r\n') + b'two')
    client = _client(reader)
    raw = _RawWriter()
    client._raw_writer = raw

    assert await _consume_api(client, api) == b'one'
    assert (await client.request('GET', '/next')).body == b'two'
    assert raw.close_calls == 0


@pytest.mark.parametrize('api', ['request', 'stream', 'read_response'])
@pytest.mark.asyncio
async def test_generic_101_preserves_transport_and_refuses_more_http(api):
    offered = [(b'connection', b'upgrade'), (b'upgrade', b'example')]
    switched = b'protocol-prebuffer'
    reader = _Reader(
        _head(101, 'Switching Protocols',
              b'Connection: Upgrade\r\nUpgrade: example\r\n') + switched)
    client = _client(reader)
    raw = _RawWriter()
    client._raw_writer = raw

    if api == 'request':
        response = await client.request('GET', '/', headers=offered)
        assert response.status == 101
    elif api == 'stream':
        assert [chunk async for chunk in client.stream('GET', '/', headers=offered)] == []
    else:
        response = await client.read_response(request_method='GET', request_headers=offered)
        assert response.status == 101

    assert reader.peek() == switched
    assert raw.close_calls == 0
    with pytest.raises(ConnectionError):
        await client.request('GET', '/next')
    assert await client.handoff().read() == switched


@pytest.mark.parametrize('switch', ['101', 'connect'])
@pytest.mark.parametrize('api,policy', [
    ('request', 'inbound-close'),
    ('stream', 'inbound-close'),
    ('read_response', 'inbound-close'),
    ('request', 'outbound-close'),
    ('stream', 'outbound-close'),
    ('request', 'http10-default'),
    ('stream', 'http10-default'),
    ('read_response', 'http10-default'),
])
@pytest.mark.asyncio
async def test_nonpersistent_protocol_switch_policy(
        switch, api, policy):
    method = 'CONNECT' if switch == 'connect' else 'GET'
    status = 200 if switch == 'connect' else 101
    reason = 'OK' if switch == 'connect' else 'Switching Protocols'
    version = b'HTTP/1.0' if policy == 'http10-default' else b'HTTP/1.1'
    response_headers = (b'Connection: close\r\n'
                        if policy == 'inbound-close' else b'')
    request_headers = ([(b'connection', b'close')]
                       if policy == 'outbound-close' else ())
    switched = b'switched-protocol-bytes'
    reader = _Reader(
        _head(status, reason, response_headers, version=version) + switched)
    client = _client(reader)
    raw = _RawWriter()
    client._raw_writer = raw

    if switch == '101' and api != 'read_response':
        with pytest.raises(ProtocolError):
            if api == 'request':
                await client.request(method, '/', headers=request_headers)
            else:
                _ = [chunk async for chunk in client.stream(
                    method, '/', headers=request_headers)]
        assert raw.close_calls == 1
        with pytest.raises(ConnectionError):
            client.handoff()
        return

    if api == 'request':
        response = await client.request(
            method, '/', headers=request_headers)
        assert response.status == status
    elif api == 'stream':
        assert [chunk async for chunk in client.stream(
            method, '/', headers=request_headers)] == []
    else:
        response = await client.read_response(request_method=method)
        assert response.status == status

    assert reader.peek() == switched
    if switch == '101':
        assert raw.close_calls == 1
        with pytest.raises(ConnectionError):
            client.handoff()
    else:
        assert raw.close_calls == 0
        assert await client.handoff().read() == switched
    with pytest.raises(ConnectionError):
        await client.request('GET', '/next')


@pytest.mark.asyncio
async def test_connect_handoff_preserves_prebuffer_and_is_bidirectional_one_shot():
    reader = _Reader(_head(200) + b'tunnel-prebuffer')
    client = _client(reader)
    raw = _RawWriter()
    client._raw_writer = raw

    await client.request('CONNECT', '/')
    session = client.handoff()

    assert await session.read(6) == b'tunnel'
    assert await session.read() == b'-prebuffer'
    await session.write(b'client-tunnel-data')
    assert bytes(client._writer.data).endswith(  # type: ignore[union-attr]
        b'client-tunnel-data')
    with pytest.raises(ConnectionError):
        client.handoff()
    with pytest.raises(ConnectionError):
        await client.request('GET', '/')
    with pytest.raises(ConnectionError):
        await client.send_raw(b'not-owned')

    await session.close()
    await session.close()
    assert raw.close_calls == 1
    with pytest.raises(ConnectionError):
        await session.read()


def test_handoff_requires_completed_protocol_switch():
    client = _client(_Reader(b''))
    client._raw_writer = _RawWriter()

    with pytest.raises(ConnectionError, match='no completed CONNECT or 101'):
        client.handoff()


@pytest.mark.asyncio
async def test_upgrade_handoff_context_owns_close_after_client_exit():
    reader = _Reader(_head(101, 'Switching Protocols',
                          b'Connection: upgrade\r\nUpgrade: example\r\n') + b'upgraded')
    client = _client(reader)
    raw = _RawWriter()
    client._raw_writer = raw

    await client.request('GET', '/', headers=[
        (b'connection', b'upgrade'), (b'upgrade', b'example')])
    session = client.handoff()
    await client.__aexit__(None, None, None)
    assert raw.close_calls == 0

    async with session as owned:
        assert await owned.read() == b'upgraded'
        await owned.write(b'written')

    assert raw.close_calls == 1


@pytest.mark.asyncio
async def test_client_owns_switched_transport_until_handoff():
    client = _client(_Reader(_head(200)))
    raw = _RawWriter()
    client._raw_writer = raw

    await client.request('CONNECT', '/')
    await client.__aexit__(None, None, None)

    assert raw.close_calls == 1
    with pytest.raises(ConnectionError, match='context is closed'):
        client.handoff()


@pytest.mark.asyncio
async def test_websocket_client_handshake_preserves_101_prebuffer(monkeypatch):
    from blackbull.client import websocket as websocket_module

    nonce = b'a' * 16
    key = b64encode(nonce)
    accept = b64encode(sha1(
        key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
    frame = b'\x81\x00'
    reader = _Reader(
        _head(101, 'Switching Protocols', headers=(
            b'Connection: Upgrade\r\nUpgrade: websocket\r\n'
            b'Sec-WebSocket-Accept: ' + accept + b'\r\n')) + frame)
    writer = _Writer()
    raw = _RawWriter()
    client = WebSocketClient('example.test', 80)
    client._reader = reader
    client._writer = writer
    client._raw_writer = raw
    monkeypatch.setattr(websocket_module.os, 'urandom', lambda _n: nonce)

    session = await client.connect('/', response_timeout=None)

    assert session.subprotocol is None
    assert reader.peek() == frame
    assert raw.close_calls == 0
    await client.__aexit__(None, None, None)
    assert raw.close_calls == 1


class TestContentlessResponsesDoNotClaimABoundary:
    """RFC 9112 §6.3 rule 1 — a message without content declares no length.

    §6.1 forbids ``Content-Length`` and ``Transfer-Encoding`` in a 1xx or a
    204, so a peer sending one has declared a boundary this message cannot
    have.  The octets that would follow are indistinguishable from the next
    response, and a connection that cannot tell them apart is not reusable.
    A HEAD or 304 is different: there the length is metadata about the
    representation, and keep-alive survives it.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize('wire,reusable', [
        pytest.param(b'HTTP/1.1 204 No Content\r\nContent-Length: 3\r\n'
                     b'\r\nodd', False, id='204-claiming-content'),
        pytest.param(b'HTTP/1.1 204 No Content\r\n'
                     b'Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n', False,
                     id='204-claiming-chunked'),
        pytest.param(b'HTTP/1.1 204 No Content\r\n\r\n'
                     b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n', True,
                     id='204-without-boundary'),
    ])
    async def test_a_204_claiming_content_is_not_reusable(self, wire, reusable):
        reader = _Reader(wire)
        recipient = HTTP1ResponseRecipient(request_method='GET')

        response = await recipient.receive(reader)

        assert response.body == b''
        assert (recipient.fate is Fate.REUSE) is reusable

    @pytest.mark.asyncio
    @pytest.mark.parametrize('method,wire', [
        pytest.param('HEAD', b'HTTP/1.1 200 OK\r\nContent-Length: 17\r\n\r\n'
                             b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n',
                     id='head-keeps-length'),
        pytest.param('GET', b'HTTP/1.1 304 Not Modified\r\nContent-Length: 17\r\n'
                            b'\r\n',
                     id='304-keeps-length'),
    ])
    async def test_a_head_response_keeps_the_length_it_only_describes(self, method, wire):
        reader = _Reader(wire)
        recipient = HTTP1ResponseRecipient(request_method=method)

        response = await recipient.receive(reader)

        assert response.body == b''
        assert recipient.fate is Fate.REUSE

    @pytest.mark.asyncio
    async def test_a_204_claiming_zero_length_is_still_reusable(self):
        """``Content-Length: 0`` agrees with having no content, so nothing is
        claimed and no boundary is at stake. Real peers send it; retiring the
        connection would cost keep-alive for no ambiguity."""
        reader = _Reader(b'HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n'
                         b'\r\n')
        recipient = HTTP1ResponseRecipient(request_method='GET')

        response = await recipient.receive(reader)

        assert response.body == b''
        assert recipient.fate is Fate.REUSE

    @pytest.mark.asyncio
    async def test_the_octets_after_a_refused_204_are_left_unread(self):
        """Refusing is not consuming. If the reader ate the leftover before
        refusing, a silent regression would look like the right answer."""
        head = (b'HTTP/1.1 204 No Content\r\nContent-Length: 3\r\n\r\n')
        reader = _Reader(head + b'odd')
        recipient = HTTP1ResponseRecipient(request_method='GET')

        await recipient.receive(reader)

        assert recipient.fate is not Fate.REUSE
        assert reader.peek() == b'odd', 'the reader consumed what it refuses'

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_a_101_naming_a_boundary_gets_no_handoff(self):
        """RFC 9112 §6.1 forbids the field in any 1xx. A switch head that
        names a boundary it cannot have is no more a switch than a preceding
        one is."""
        reader = _Reader(b'HTTP/1.1 101 Switching Protocols\r\n'
                         b'Content-Length: 5\r\n\r\n')
        recipient = HTTP1ResponseRecipient(request_method='GET')

        response = await recipient.receive(reader)

        assert response.status == 101
        assert recipient.fate is Fate.RETIRE

    @pytest.mark.asyncio
    async def test_a_successful_connect_ignores_the_fields_it_must_ignore(self):
        """RFC 9110 §9.3.6: the tunnel begins at the header terminator and
        the client MUST ignore Content-Length and Transfer-Encoding in a 2xx
        to CONNECT. Ignoring is what makes them harmless — there is no
        boundary to be ambiguous about, so the handoff stands."""
        reader = _Reader(b'HTTP/1.1 200 Connection Established\r\n'
                         b'Transfer-Encoding: chunked\r\n\r\n')
        recipient = HTTP1ResponseRecipient(request_method='CONNECT')

        response = await recipient.receive(reader)

        assert response.status == 200
        assert recipient.fate is Fate.SWITCH

    @pytest.mark.asyncio
    async def test_an_interim_head_claiming_a_boundary_retires_the_connection(self):
        """The interim heads the production path skips are the ones a peer
        would hide a second response inside, so the boundary check belongs
        where they are read — not only where a framed 1xx could arrive."""
        reader = _Reader(b'HTTP/1.1 100 Continue\r\nContent-Length: 51\r\n'
                         b'\r\nHTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n')
        recipient = HTTP1ResponseRecipient(request_method='GET')

        response = await recipient.receive(reader)

        assert response.status == 200
        assert recipient.fate is not Fate.REUSE

    @pytest.mark.asyncio
    async def test_a_boundary_violation_survives_a_protocol_switch(self):
        """A peer that named a boundary it cannot have is not switching
        anything, so no handoff is offered. The response policy settles that
        once, with the same verdict that retires the connection."""
        reader = _Reader(b'HTTP/1.1 100 Continue\r\nContent-Length: 5\r\n'
                         b'\r\nHTTP/1.1 101 Switching Protocols\r\n\r\n')
        recipient = HTTP1ResponseRecipient(request_method='GET')
        response = await recipient.receive(reader)
        assert response.status == 101

        assert recipient.fate is Fate.RETIRE, \
            'an ambiguous connection is not switching anything'

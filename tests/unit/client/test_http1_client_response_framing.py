"""RFC 9112 response framing through the HTTP/1.1 client API."""
from __future__ import annotations

import asyncio
from base64 import b64encode
from hashlib import sha1
from http import HTTPMethod

import pytest

from blackbull.client.exceptions import (ConnectionError, ProtocolError,
                                         ResponseTooLarge)
from blackbull.client.http1 import HTTP1Client, HTTP1ResponseRecipient
from blackbull.client.websocket import WebSocketClient
from blackbull.server.recipient import AbstractReader
from blackbull.server.sender import AbstractWriter


class _Reader(AbstractReader):
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.read_sizes: list[int] = []

    async def read(self, n: int = -1) -> bytes:
        self.read_sizes.append(n)
        if n < 0:
            n = len(self.data) - self.pos
        end = min(self.pos + n, len(self.data))
        out = self.data[self.pos:end]
        self.pos = end
        return out

    @property
    def remaining(self) -> bytes:
        return self.data[self.pos:]


class _Writer(AbstractWriter):
    def __init__(self) -> None:
        self.data = bytearray()

    async def write(self, data: bytes) -> None:
        self.data.extend(data)


class _RawWriter(asyncio.StreamWriter):
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1

    async def wait_closed(self) -> None:
        return None

    def __del__(self) -> None:
        # This test double intentionally skips StreamWriter.__init__, so its
        # production destructor has no transport to inspect.
        return None


class _FailingWriter(_Writer):
    """Accept the request head, then fail once transmission has begun."""

    async def write(self, data: bytes) -> None:
        if self.data:
            raise RuntimeError('injected write failure')
        await super().write(data)


class _BlockingBodyReader(_Reader):
    """One body chunk, then a cancellable pending transport read."""

    def __init__(self, head: bytes) -> None:
        super().__init__(b'')
        self._head = head
        self._first = True

    async def read_head(self, limit: int) -> bytes:
        return self._head

    async def read(self, n: int = -1) -> bytes:
        if self._first:
            self._first = False
            return b'first'
        await asyncio.Future()


class _ShortReadingReader(_Reader):
    """Return at most two bytes from each body read."""

    async def read(self, n: int = -1) -> bytes:
        return await super().read(2 if n < 0 else min(n, 2))


class _BlockingAfterHeadReader(_Reader):
    """Return a parsed head, then drain a prefix and block at the boundary."""

    def __init__(self, head: bytes, data: bytes) -> None:
        super().__init__(data)
        self._head = head

    async def read_head(self, limit: int) -> bytes:
        return self._head

    async def read(self, n: int = -1) -> bytes:
        if self.pos < len(self.data):
            return await super().read(n)
        await asyncio.Future()


def _client(reader: _Reader) -> HTTP1Client:
    client = HTTP1Client('example.test', 80)
    client._reader = reader
    client._writer = _Writer()
    return client


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
    assert reader.remaining.startswith(b'HTTP/1.1 204')


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
    assert reader.remaining.startswith(b'HTTP/1.1 200')


@pytest.mark.asyncio
async def test_receive_close_delimited_body_until_eof_and_marks_nonreusable():
    reader = _Reader(_head(200) + b'close-delimited')
    recipient = HTTP1ResponseRecipient(request_method='GET')

    response = await recipient.receive(reader)

    assert response.body == b'close-delimited'
    assert recipient.reusable is False


@pytest.mark.asyncio
async def test_stream_close_delimited_body_until_eof_and_marks_nonreusable():
    reader = _Reader(_head(200) + b'close-delimited')
    recipient = HTTP1ResponseRecipient(request_method='GET')

    chunks = [chunk async for chunk in recipient.stream(reader)]

    assert b''.join(chunks) == b'close-delimited'
    assert recipient.reusable is False


@pytest.mark.asyncio
async def test_successful_connect_response_is_bodyless_and_nonreusable():
    reader = _Reader(_head(200) + b'tunnel-bytes')

    response = await HTTP1ResponseRecipient(request_method='CONNECT').receive(
        reader)

    assert response.body == b''
    assert reader.remaining == b'tunnel-bytes'
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
    assert reader.remaining == b'tunnel-bytes'
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
    assert reader.remaining == b'stream-tunnel'
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
    assert reader.remaining == b'read-tunnel'
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

    assert reader.read_sizes[-1] == 1
    assert recipient.framing_broken is True


@pytest.mark.asyncio
async def test_final_chunked_coding_in_a_transfer_encoding_list_is_chunked():
    reader = _Reader(
        _head(200, headers=b'Transfer-Encoding: gzip, chunked\r\n')
        + _chunked_body(b'encoded-payload')
    )

    response = await HTTP1ResponseRecipient(request_method='GET').receive(reader)

    assert response.body == b'encoded-payload'
    assert reader.remaining == b''


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
    assert reader.remaining.startswith(b'HTTP/1.1 200')
    assert recipient.reusable is True


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
        assert reader.remaining == b'body'


@pytest.mark.parametrize('header', [
    b'gzip, chunked,\r\n',
    b'gzip,,chunked\r\n',
    b'gzip;parameter="a,b", chunked\r\n',
    b'gzip;parameter="a\tb", chunked\r\n',
    b'gzip;parameter="a\\tb", chunked\r\n',
    b'gzip;parameter="a\\\"b", chunked\r\n',
    b'gzip;parameter="a\\\\b", chunked\r\n',
    b'GZip, ChUnKeD\r\n',
])
@pytest.mark.asyncio
async def test_transfer_encoding_http_list_forms_use_final_chunked(header):
    reader = _Reader(_head(200, headers=b'Transfer-Encoding: ' + header)
                     + _chunked_body(b'payload'))

    response = await HTTP1ResponseRecipient(request_method='GET').receive(reader)

    assert response.body == b'payload'
    assert reader.remaining == b''


@pytest.mark.asyncio
async def test_transfer_encoding_repeated_fields_are_combined_in_order():
    reader = _Reader(
        _head(200, headers=(b'Transfer-Encoding: gzip\r\n'
                           b'Transfer-Encoding: chunked\r\n'))
        + _chunked_body(b'payload'))

    response = await HTTP1ResponseRecipient(request_method='GET').receive(reader)

    assert response.body == b'payload'


@pytest.mark.asyncio
async def test_transfer_encoding_repeated_empty_members_are_ignored():
    reader = _Reader(
        _head(200, headers=(b'Transfer-Encoding: gzip,\r\n'
                           b'Transfer-Encoding: ,chunked\r\n'))
        + _chunked_body(b'payload'))

    response = await HTTP1ResponseRecipient(request_method='GET').receive(reader)

    assert response.body == b'payload'


@pytest.mark.parametrize('te_headers', [
    b'Transfer-Encoding:\r\n',
    b'Transfer-Encoding:\r\nTransfer-Encoding: \t\r\n',
])
@pytest.mark.asyncio
async def test_present_empty_transfer_encoding_with_content_length_is_refused(
        te_headers):
    """The server refuses Content-Length beside Transfer-Encoding on the
    presence of the field, not on what the field parses to, so an empty one
    counts.  RFC 9112 §6.3 item 3 gives the precedence *and* calls the message
    one that "ought to be handled as an error"; taking the precedence alone
    leaves the client trusting whichever field an intermediary did not."""
    reader = _Reader(
        _head(200, headers=te_headers + b'Content-Length: 4\r\n')
        + b'body-through-eof')
    recipient = HTTP1ResponseRecipient(request_method='GET')

    with pytest.raises(ProtocolError):
        await recipient.receive(reader)

    assert recipient.framing_broken is True


@pytest.mark.parametrize('te_headers', [
    b'Transfer-Encoding:\r\n',
    b'Transfer-Encoding:\r\nTransfer-Encoding: \t\r\n',
])
@pytest.mark.asyncio
async def test_present_empty_transfer_encoding_with_content_length_streaming(
        te_headers):
    """The streaming entry point answers the same field pair the same way."""
    reader = _Reader(
        _head(200, headers=te_headers + b'Content-Length: 4\r\n')
        + b'body-through-eof')
    recipient = HTTP1ResponseRecipient(request_method='GET')

    with pytest.raises(ProtocolError):
        [chunk async for chunk in recipient.stream(reader)]

    assert recipient.framing_broken is True


@pytest.mark.parametrize('header', [
    b'gzip;parameter="unterminated\r\n',
    b'gzip;parameter="a\x00b", chunked\r\n',
    b'gzip;parameter="a\\\x01b", chunked\r\n',
    b'gzip;parameter="a' + b'\\' + b'\r\n',
    b'gzip;parameter=\r\n',
    b'gzip;=value\r\n',
    b'gzip parameter=value\r\n',
])
@pytest.mark.asyncio
async def test_malformed_transfer_encoding_poisoned_recipient(header):
    reader = _Reader(_head(200, headers=b'Transfer-Encoding: ' + header)
                     + b'body')
    recipient = HTTP1ResponseRecipient(request_method='GET')

    with pytest.raises(ProtocolError):
        await recipient.receive(reader)

    assert recipient.framing_broken is True


@pytest.mark.asyncio
async def test_malformed_transfer_encoding_poisoned_http1client():
    reader = _Reader(_head(200, headers=b'Transfer-Encoding: gzip;bad\r\n')
                     + b'body')
    client = _client(reader)

    with pytest.raises(ProtocolError):
        await client.request('GET', '/')

    assert client._framing_broken is True
    with pytest.raises(ConnectionError):
        await client.request('GET', '/second')


@pytest.mark.asyncio
async def test_too_many_empty_transfer_encoding_members_are_rejected():
    reader = _Reader(_head(200, headers=b'Transfer-Encoding: ' + b',' * 17
                                  + b'\r\n'))

    with pytest.raises(ProtocolError):
        await HTTP1ResponseRecipient(request_method='GET').receive(reader)


@pytest.mark.asyncio
async def test_nonfinal_chunked_transfer_encoding_is_close_delimited():
    reader = _Reader(_head(200, headers=b'Transfer-Encoding: chunked, gzip\r\n')
                     + b'wire-payload')
    recipient = HTTP1ResponseRecipient(request_method='GET')

    response = await recipient.receive(reader)

    assert response.body == b'wire-payload'
    assert recipient.reusable is False


@pytest.mark.asyncio
async def test_stream_uses_final_chunked_coding_in_a_transfer_encoding_list():
    reader = _Reader(
        _head(200, headers=b'Transfer-Encoding: gzip, chunked\r\n')
        + _chunked_body(b'encoded-payload')
    )

    chunks = [chunk async for chunk in
              HTTP1ResponseRecipient(request_method='GET').stream(reader)]

    assert b''.join(chunks) == b'encoded-payload'
    assert reader.remaining == b''


@pytest.mark.asyncio
async def test_nonchunked_transfer_encoding_with_content_length_is_refused():
    """Whether or not chunked is the final coding, the two framing fields
    together are the response-splitting shape, and one repository should not
    hold two answers to one field pair."""
    reader = _Reader(
        _head(200, headers=(b'Transfer-Encoding: gzip\r\n'
                           b'Content-Length: 3\r\n'))
        + b'encoded-payload'
    )

    recipient = HTTP1ResponseRecipient(request_method='GET')

    with pytest.raises(ProtocolError):
        await recipient.receive(reader)

    assert recipient.framing_broken is True


@pytest.mark.asyncio
async def test_nonchunked_transfer_encoding_alone_is_close_delimited():
    """The control for the refusal above: Transfer-Encoding without
    Content-Length keeps §6.3 item 4's response branch — chunked is not the
    final coding, so the length is the connection's."""
    reader = _Reader(_head(200, headers=b'Transfer-Encoding: gzip\r\n')
                     + b'encoded-payload')

    recipient = HTTP1ResponseRecipient(request_method='GET')
    response = await recipient.receive(reader)

    assert response.body == b'encoded-payload'
    assert recipient.reusable is False


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
    assert client._framing_broken is False


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
    assert reader.remaining.startswith(b'HTTP/1.1 204')


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
    reader = _ShortReadingReader(
        _head(200, headers=b'Content-Length: 6\r\n') + b'abcdef')
    client = _client(reader)
    raw = _RawWriter()
    client._raw_writer = raw
    stream = client.stream('GET', '/')

    assert await anext(stream) == b'ab'
    assert reader.remaining == b'cdef'
    sent_before_refusal = bytes(client._writer.data)  # type: ignore[union-attr]
    with pytest.raises(ConnectionError):
        await client.request('GET', '/concurrent')
    assert bytes(client._writer.data) == sent_before_refusal  # type: ignore[union-attr]

    await stream.aclose()

    assert client._framing_broken is True
    assert raw.close_calls == 1
    assert reader.remaining == b'cdef'


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
    assert client._framing_broken is True
    with pytest.raises(ConnectionError):
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
    assert client._framing_broken is True


@pytest.mark.asyncio
async def test_partial_stream_cancellation_poison_connection():
    reader = _BlockingBodyReader(_head(200, headers=b'Content-Length: 10\r\n'))
    client = _client(reader)
    stream = client.stream('GET', '/')

    assert await anext(stream) == b'first'
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert client._active_response is None
    assert client._framing_broken is True
    with pytest.raises(ConnectionError):
        await client.request('GET', '/second')


@pytest.mark.asyncio
async def test_stream_cancellation_while_waiting_for_chunk_size_poison_closes():
    reader = _BlockingAfterHeadReader(
        _head(200, headers=b'Transfer-Encoding: chunked\r\n'),
        b'3\r\none\r\n')
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

    assert client._framing_broken is True
    assert raw.close_calls == 1
    assert reader.remaining == b''


@pytest.mark.asyncio
async def test_stream_cancellation_while_waiting_for_close_eof_poison_closes():
    reader = _BlockingAfterHeadReader(_head(200), b'one')
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

    assert client._framing_broken is True
    assert raw.close_calls == 1
    assert reader.remaining == b''


@pytest.mark.asyncio
async def test_complete_self_delimited_stream_remains_reusable():
    reader = _Reader(
        _head(200, headers=b'Content-Length: 3\r\n') + b'one'
        + _head(200, headers=b'Transfer-Encoding: chunked\r\n')
        + _chunked_body(b'two'))
    client = _client(reader)

    assert b''.join([chunk async for chunk in client.stream('GET', '/')]) == b'one'
    assert b''.join([chunk async for chunk in client.stream('GET', '/')]) == b'two'

    assert client._framing_broken is False


@pytest.mark.asyncio
async def test_read_response_accepts_originating_method():
    reader = _Reader(_head(200, headers=b'Content-Length: 4\r\n') + b'next')
    client = _client(reader)

    response = await client.read_response(request_method='HEAD')

    assert response.body == b''
    assert reader.remaining == b'next'


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
    assert client._framing_broken is False


@pytest.mark.asyncio
async def test_read_response_refuses_prebuffered_head_after_context_exit():
    reader = _Reader(_head(204) + b'following-bytes')
    client = _client(reader)
    raw = _RawWriter()
    client._raw_writer = raw

    await client.__aexit__(None, None, None)

    with pytest.raises(ConnectionError, match='HTTP1Client context is closed'):
        await client.read_response()
    assert reader.pos == 0
    assert reader.remaining == _head(204) + b'following-bytes'
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

    assert client._framing_broken is True
    assert raw.close_calls == 1


@pytest.mark.asyncio
async def test_every_octet_is_judged_by_the_rule_and_not_by_a_sample():
    """RFC 9110 §5.5 admits HTAB, VCHAR and obs-text — and nothing else.

    The samples above name the octets an attacker reaches for.  This one
    names the rule, so any rewrite of the check (a comprehension, a
    translation table) has to answer for all 256 rather than for the seven
    that were thought of.
    """
    for octet in range(256):
        value = b'a' + bytes([octet]) + b'b'
        reader = _Reader(_head(200, headers=b'X-Test: ' + value
                               + b'\r\nContent-Length: 0\r\n'))
        recipient = HTTP1ResponseRecipient('GET')

        if (octet < 0x20 and octet != 0x09) or octet == 0x7f:
            with pytest.raises(ProtocolError):
                await recipient.receive(reader)
        else:
            response = await recipient.receive(reader)
            assert response.headers.get(b'x-test') == value


@pytest.mark.asyncio
async def test_response_field_trims_only_legal_sp_and_htab_ows():
    reader = _Reader(
        _head(200, headers=(b'X-Test: \t value\tinside \t\r\n'
                            b'Content-Length: 0\r\n')))

    response = await HTTP1ResponseRecipient('GET').receive(reader)

    assert response.headers.get(b'x-test') == b'value\tinside'


@pytest.mark.asyncio
async def test_invalid_control_cannot_be_stripped_into_chunked_framing():
    reader = _Reader(
        _head(200, headers=b'Transfer-Encoding: \x0bchunked\x0c\r\n')
        + _chunked_body(b'must-remain'))
    client = _client(reader)
    raw = _RawWriter()
    client._raw_writer = raw

    with pytest.raises(ProtocolError):
        await client.request('GET', '/')

    assert reader.remaining == _chunked_body(b'must-remain')
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
        with pytest.raises(ValueError):
            await client.request('POST', '/', headers=bad_headers, body=b'x')
    else:
        body_stream = client.stream(
            'POST', '/', headers=bad_headers, body=b'x')
        with pytest.raises(ValueError):
            await anext(body_stream)

    assert bytes(client._writer.data) == b''  # type: ignore[union-attr]
    assert client._active_response is None
    assert client._framing_broken is False
    assert client._reusable is True
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
    assert client._active_response is None
    assert client._reusable is True
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
    assert client._framing_broken is True
    assert client._reusable is False
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
    assert reader.remaining.startswith(b'HTTP/1.1 200')
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
    switched = b'protocol-prebuffer'
    reader = _Reader(
        _head(101, 'Switching Protocols',
              b'Connection: Upgrade\r\nUpgrade: example\r\n') + switched)
    client = _client(reader)
    raw = _RawWriter()
    client._raw_writer = raw

    if api == 'request':
        response = await client.request('GET', '/')
        assert response.status == 101
    elif api == 'stream':
        assert [chunk async for chunk in client.stream('GET', '/')] == []
    else:
        response = await client.read_response(request_method='GET')
        assert response.status == 101

    assert reader.remaining == switched
    assert raw.close_calls == 0
    assert client._handoff_ready is True
    with pytest.raises(ConnectionError):
        await client.request('GET', '/next')


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
async def test_nonpersistent_protocol_switch_closes_without_handoff(
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

    assert reader.remaining == switched
    assert client._protocol_switched is True
    assert client._reusable is False
    assert client._handoff_ready is False
    assert raw.close_calls == 1
    with pytest.raises(ConnectionError, match='no completed CONNECT or 101'):
        client.handoff()
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
    reader = _Reader(_head(101, 'Switching Protocols') + b'upgraded')
    client = _client(reader)
    raw = _RawWriter()
    client._raw_writer = raw

    await client.request('GET', '/')
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
    assert client._handoff_ready is False
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
    assert reader.remaining == frame
    assert raw.close_calls == 0
    await client.__aexit__(None, None, None)
    assert raw.close_calls == 1

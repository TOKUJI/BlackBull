"""HTTP/1.1 protocol compliance tests — sender, keep-alive, body handling, trailers.

Each class protects a specific RFC 7230 / RFC 7231 contract observed on the wire.
Tests drive HTTP1Actor and HTTP1Sender end-to-end using in-process fakes.
"""

import asyncio
import pytest
from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock, patch
from hypothesis import given
from hypothesis import strategies as st

from blackbull.server.http1_actor import HTTP1Actor
from blackbull.server.sender import AbstractWriter, SenderFactory
from blackbull.server.recipient import AbstractReader, IncompleteReadError
from blackbull.server.websocket_actor import WebSocketActor


# ---------------------------------------------------------------------------
# In-process fakes
# ---------------------------------------------------------------------------

class _FakeReader(AbstractReader):
    def __init__(self, data: bytes):
        self._buf = bytearray(data)

    async def readline(self) -> bytes:
        idx = self._buf.find(b'\n')
        if idx == -1:
            chunk, self._buf = bytes(self._buf), bytearray()
            return chunk
        chunk = bytes(self._buf[:idx + 1])
        del self._buf[:idx + 1]
        return chunk

    async def read(self, n: int = -1) -> bytes:
        if n < 0:
            chunk, self._buf = bytes(self._buf), bytearray()
            return chunk
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    async def readuntil(self, sep: bytes) -> bytes:
        idx = self._buf.find(sep)
        if idx == -1:
            chunk, self._buf = bytes(self._buf), bytearray()
            return chunk + sep
        chunk = bytes(self._buf[:idx + len(sep)])
        del self._buf[:idx + len(sep)]
        return chunk

    async def readexactly(self, n: int) -> bytes:
        if len(self._buf) < n:
            raise IncompleteReadError(bytes(self._buf))
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk


class _FakeWriter(AbstractWriter):
    def __init__(self):
        self.written = bytearray()
        self.closed = False

    async def write(self, data: bytes) -> None:
        self.written += data

    async def close(self) -> None:
        self.closed = True


async def _noop_app(scope, receive, send):
    pass


def _http_request(method='GET', path='/', version='HTTP/1.1',
                  headers: dict | None = None) -> bytes:
    if headers is None:
        headers = {'Host': 'localhost:8000'}
    lines = [f'{method} {path} {version}']
    for k, v in headers.items():
        lines.append(f'{k}: {v}')
    lines += ['', '']
    return '\r\n'.join(lines).encode()


def _make_actor(raw: bytes, app=None, *,
                peername=('127.0.0.1', 54321),
                sockname=('0.0.0.0', 8000),
                ssl=False):
    if app is None:
        app = _noop_app
    writer = _FakeWriter()
    first_line, rest = raw.split(b'\r\n', 1)
    reader = _FakeReader(rest)
    actor = HTTP1Actor(
        reader, writer, app, None,
        request=first_line + b'\r\n',
        peername=peername, sockname=sockname, ssl=ssl,
    )
    return actor, writer


def _make_sender_and_writer():
    writer = _FakeWriter()
    send = SenderFactory.http1(writer)
    return send, writer


# ---------------------------------------------------------------------------
# SenderFactory.http1 — ASGI event dict → HTTP/1.1 wire bytes
# ---------------------------------------------------------------------------

class TestMakeSender:
    """SenderFactory.http1() must serialize ASGI event dicts to HTTP/1.1 wire bytes."""

    @pytest.mark.asyncio
    async def test_response_start_writes_status_line(self):
        send, writer = _make_sender_and_writer()
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})
        assert bytes(writer.written).startswith(b'HTTP/1.1 200')

    @pytest.mark.asyncio
    async def test_response_start_writes_reason_phrase(self):
        send, writer = _make_sender_and_writer()
        await send({'type': 'http.response.start', 'status': 404, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})
        assert b'Not Found' in bytes(writer.written)

    @pytest.mark.asyncio
    async def test_response_start_writes_headers(self):
        send, writer = _make_sender_and_writer()
        await send({
            'type': 'http.response.start', 'status': 200,
            'headers': [(b'content-type', b'text/plain'), (b'content-length', b'5')],
        })
        await send({'type': 'http.response.body', 'body': b''})
        written = bytes(writer.written)
        assert b'content-type: text/plain\r\n' in written
        assert b'content-length: 5\r\n' in written

    @pytest.mark.asyncio
    async def test_response_start_ends_with_blank_line(self):
        send, writer = _make_sender_and_writer()
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})
        assert b'\r\n\r\n' in bytes(writer.written)

    @pytest.mark.asyncio
    async def test_response_body_writes_bytes(self):
        send, writer = _make_sender_and_writer()
        body = b'Hello, world!'
        await send({'type': 'http.response.body', 'body': body, 'more_body': False})
        assert body in bytes(writer.written)

    @pytest.mark.asyncio
    async def test_full_response_is_valid_http11(self):
        send, writer = _make_sender_and_writer()
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-length', b'2')]})
        await send({'type': 'http.response.body', 'body': b'OK', 'more_body': False})
        wire = bytes(writer.written)
        assert wire.startswith(b'HTTP/1.1 200')
        header_part, body_part = wire.split(b'\r\n\r\n', 1)
        assert body_part == b'OK'

    @pytest.mark.asyncio
    async def test_response_start_is_not_dict_repr(self):
        send, writer = _make_sender_and_writer()
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})
        assert not bytes(writer.written).startswith(b'{')


# ---------------------------------------------------------------------------
# RFC 7230 §3.3.2 / RFC 7231 §7.1.1.2 — Auto Content-Length and Date headers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHTTP11AutoHeaders:
    """HTTP1Sender must auto-inject Content-Length and Date when absent."""

    async def test_content_length_injected_when_absent(self):
        send, writer = _make_sender_and_writer()
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'hello', 'more_body': False})
        assert b'content-length:' in bytes(writer.written).lower()

    async def test_content_length_value_matches_body(self):
        send, writer = _make_sender_and_writer()
        body = b'hello world'
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': body, 'more_body': False})
        assert f'content-length: {len(body)}'.encode() in bytes(writer.written).lower()

    async def test_date_header_injected(self):
        send, writer = _make_sender_and_writer()
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'hi', 'more_body': False})
        assert b'date:' in bytes(writer.written).lower()

    async def test_app_supplied_content_length_not_duplicated(self):
        send, writer = _make_sender_and_writer()
        body = b'hello'
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-length', str(len(body)).encode())]})
        await send({'type': 'http.response.body', 'body': body, 'more_body': False})
        assert bytes(writer.written).lower().count(b'content-length:') == 1

    @given(header_case=st.sampled_from([b'content-length', b'Content-Length', b'CONTENT-LENGTH']))
    def test_bytes_path_content_length_not_duplicated_any_case(self, header_case):
        """content-length must not appear twice regardless of the case the app provides."""
        async def _run():
            send, writer = _make_sender_and_writer()
            body = b'hello'
            await send(body, HTTPStatus.OK, [(header_case, str(len(body)).encode())])
            assert bytes(writer.written).lower().count(b'content-length:') == 1
        asyncio.run(_run())


# ---------------------------------------------------------------------------
# RFC 7230 §6.3 — Keep-Alive
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHTTP11KeepAlive:
    """HTTP1Actor must loop and process multiple requests on one connection."""

    async def test_two_requests_on_one_connection_calls_app_twice(self):
        req1 = _http_request(method='GET', path='/one',
                             headers={'Host': 'localhost:8000', 'Connection': 'keep-alive'})
        req2 = _http_request(method='GET', path='/two',
                             headers={'Host': 'localhost:8000', 'Connection': 'close'})
        req1_first_line, req1_rest = req1.split(b'\r\n', 1)
        reader = _FakeReader(req1_rest + req2)
        writer = _FakeWriter()
        call_count = 0

        async def counting_app(scope, receive, send):
            nonlocal call_count
            call_count += 1

        actor = HTTP1Actor(reader, writer, counting_app, None,
                           request=req1_first_line + b'\r\n')
        await actor.run()

        assert call_count == 2

    async def test_connection_close_ends_loop(self):
        req = _http_request(method='GET', path='/',
                            headers={'Host': 'localhost:8000', 'Connection': 'close'})
        first_line, rest = req.split(b'\r\n', 1)
        call_count = 0

        async def counting_app(scope, receive, send):
            nonlocal call_count
            call_count += 1

        actor = HTTP1Actor(_FakeReader(rest), _FakeWriter(), counting_app, None,
                           request=first_line + b'\r\n')
        await actor.run()
        assert call_count == 1

    async def test_keep_alive_paths_differ_between_requests(self):
        req1 = _http_request(method='GET', path='/alpha',
                             headers={'Host': 'localhost:8000', 'Connection': 'keep-alive'})
        req2 = _http_request(method='GET', path='/beta',
                             headers={'Host': 'localhost:8000', 'Connection': 'close'})
        req1_first_line, req1_rest = req1.split(b'\r\n', 1)
        paths = []

        async def capturing_app(scope, receive, send):
            paths.append(scope['path'])

        actor = HTTP1Actor(_FakeReader(req1_rest + req2), _FakeWriter(),
                           capturing_app, None, request=req1_first_line + b'\r\n')
        await actor.run()
        assert '/alpha' in paths and '/beta' in paths

    async def test_incomplete_read_on_first_request_closes_silently(self):
        req = _http_request(method='GET', path='/', headers={'Host': 'localhost:8000'})
        first_line, _ = req.split(b'\r\n', 1)
        reader = MagicMock(spec=AbstractReader)
        reader.readuntil = AsyncMock(side_effect=IncompleteReadError(b'Host: localh'))

        actor = HTTP1Actor(reader, _FakeWriter(), _noop_app, None,
                           request=first_line + b'\r\n')
        await actor.run()  # must return normally

    async def test_incomplete_read_after_first_request_app_called_once(self):
        req = _http_request(method='GET', path='/one',
                            headers={'Host': 'localhost:8000', 'Connection': 'keep-alive'})
        first_line, rest = req.split(b'\r\n', 1)
        call_count = 0

        async def counting_app(scope, receive, send):
            nonlocal call_count
            call_count += 1

        reader = MagicMock(spec=AbstractReader)
        reader.readuntil = AsyncMock(side_effect=[
            rest,
            IncompleteReadError(b'GET /two HTTP'),
        ])
        reader.read = AsyncMock(return_value=b'')

        actor = HTTP1Actor(reader, _FakeWriter(), counting_app, None,
                           request=first_line + b'\r\n')
        await actor.run()
        assert call_count == 1


# ---------------------------------------------------------------------------
# ASGI §2.3 — http.disconnect
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHTTPDisconnect:
    """HTTP1Actor must return http.disconnect when the client closes the connection."""

    async def test_receive_returns_disconnect_on_eof(self):
        raw = _http_request(headers={'Host': 'localhost:8000', 'Content-Length': '10'})
        disconnect_received = []

        async def app(scope, receive, send):
            event = await receive()
            disconnect_received.append(event)

        # Wrap the MagicMock in AsyncioReader: that's the production path
        # (the recipient factory does the same when it sees a non-AbstractReader),
        # and it converts asyncio.IncompleteReadError → blackbull's
        # IncompleteReadError that HTTP1Recipient catches.
        from blackbull.server.recipient import AsyncioReader as _AsyncioReader
        backing = MagicMock()
        backing.readuntil = AsyncMock(return_value=b'\r\n\r\n')
        backing.readexactly = AsyncMock(side_effect=asyncio.IncompleteReadError(b'', 10))
        reader = _AsyncioReader(backing)

        actor = HTTP1Actor(reader, _FakeWriter(), app, None,
                           request=b'GET / HTTP/1.1\r\n')
        actor._request = raw
        await actor.run()

        assert any(e.get('type') == 'http.disconnect' for e in disconnect_received)

    async def test_disconnect_event_has_correct_type(self):
        raw = _http_request(headers={'Host': 'localhost:8000', 'Content-Length': '5'})
        events = []

        async def app(scope, receive, send):
            events.append(await receive())

        from blackbull.server.recipient import AsyncioReader as _AsyncioReader
        backing = MagicMock()
        backing.readuntil = AsyncMock(return_value=b'\r\n\r\n')
        backing.readexactly = AsyncMock(side_effect=asyncio.IncompleteReadError(b'hel', 5))
        reader = _AsyncioReader(backing)

        actor = HTTP1Actor(reader, _FakeWriter(), app, None,
                           request=b'GET / HTTP/1.1\r\n')
        actor._request = raw
        await actor.run()

        assert 'http.disconnect' in [e.get('type') for e in events]


# ---------------------------------------------------------------------------
# RFC 7231 §5.1.1 — Expect: 100-continue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHTTP11Expect100Continue:
    """Server must send 100 Continue before reading body when Expect header present."""

    def _make_expect_request(self, body: bytes = b'hello') -> bytes:
        """Return only the header bytes (terminated by CRLFCRLF).

        The real reader returns headers via ``readuntil(\\r\\n\\r\\n)`` and
        the body separately via ``readexactly(N)``, so the request-bytes
        the actor parses MUST NOT contain the body.  The body is supplied
        to the per-test ``reader.readexactly`` mock.
        """
        lines = ['POST /upload HTTP/1.1', 'Host: localhost:8000',
                 f'Content-Length: {len(body)}', 'Expect: 100-continue', '', '']
        return '\r\n'.join(lines).encode()

    async def test_100_continue_sent_before_body(self):
        body = b'hello'
        raw = self._make_expect_request(body)
        writer = _FakeWriter()
        body_read_position = None

        async def app(scope, receive, send):
            nonlocal body_read_position
            body_read_position = bytes(writer.written)
            await receive()

        reader = MagicMock(spec=AbstractReader)
        reader.readuntil = AsyncMock(return_value=b'\r\n\r\n')
        reader.readexactly = AsyncMock(return_value=body)

        actor = HTTP1Actor(reader, writer, app, None,
                           request=b'POST /upload HTTP/1.1\r\n')
        actor._request = raw
        await actor.run()

        assert body_read_position is not None
        assert b'100' in body_read_position

    async def test_body_is_read_after_100(self):
        body = b'payload'
        raw = self._make_expect_request(body)
        received_body = None

        async def app(scope, receive, send):
            nonlocal received_body
            event = await receive()
            received_body = event.get('body')

        reader = MagicMock(spec=AbstractReader)
        reader.readuntil = AsyncMock(return_value=b'\r\n\r\n')
        reader.readexactly = AsyncMock(return_value=body)

        actor = HTTP1Actor(reader, _FakeWriter(), app, None,
                           request=b'POST /upload HTTP/1.1\r\n')
        actor._request = raw
        await actor.run()

        assert received_body == body

    async def test_no_100_without_expect_header(self):
        body = b'data'
        lines = ['POST / HTTP/1.1', 'Host: localhost:8000',
                 f'Content-Length: {len(body)}', '', '']
        raw = '\r\n'.join(lines).encode() + body
        writer = _FakeWriter()

        async def noop_app(scope, receive, send):
            await receive()

        reader = MagicMock(spec=AbstractReader)
        reader.readuntil = AsyncMock(return_value=b'\r\n\r\n')
        reader.readexactly = AsyncMock(return_value=body)

        actor = HTTP1Actor(reader, writer, noop_app, None,
                           request=b'POST / HTTP/1.1\r\n')
        actor._request = raw
        await actor.run()

        assert b'100' not in bytes(writer.written)

    async def test_case_insensitive_expect(self):
        lines = ['POST /upload HTTP/1.1', 'Host: localhost:8000',
                 'Content-Length: 5', 'Expect: 100-Continue', '', '']
        raw = '\r\n'.join(lines).encode()
        writer = _FakeWriter()

        async def app(scope, receive, send):
            await receive()

        reader = MagicMock(spec=AbstractReader)
        reader.readuntil = AsyncMock(return_value=b'\r\n\r\n')
        reader.readexactly = AsyncMock(return_value=b'hello')

        actor = HTTP1Actor(reader, writer, app, None,
                           request=b'POST /upload HTTP/1.1\r\n')
        actor._request = raw
        await actor.run()

        assert b'100' in bytes(writer.written)

    async def test_final_response_after_100(self):
        body = b'data'
        raw = self._make_expect_request(body)
        writer = _FakeWriter()

        async def app(scope, receive, send):
            await receive()
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})

        reader = MagicMock(spec=AbstractReader)
        reader.readuntil = AsyncMock(return_value=b'\r\n\r\n')
        reader.readexactly = AsyncMock(return_value=body)

        actor = HTTP1Actor(reader, writer, app, None,
                           request=b'POST /upload HTTP/1.1\r\n')
        actor._request = raw
        await actor.run()

        wire = bytes(writer.written)
        assert wire.count(b'HTTP/1.1') == 2
        assert b'HTTP/1.1 200' in wire


# ---------------------------------------------------------------------------
# RFC 7230 §4.1 — Streaming request body (chunked Transfer-Encoding)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestStreamingRequestBody:
    """HTTP1Recipient must yield one http.request event per chunk."""

    def _make_recipient(self, chunked_wire: bytes):
        from blackbull.server.recipient import HTTP1Recipient, AsyncioReader
        from blackbull.headers import Headers
        scope = {'headers': Headers([(b'transfer-encoding', b'chunked')])}
        reader = AsyncioReader(_FakeReader(chunked_wire))
        return HTTP1Recipient(reader, scope)

    async def test_first_chunk_has_more_body_true(self):
        wire = b'5\r\nhello\r\n5\r\nworld\r\n0\r\n\r\n'
        recipient = self._make_recipient(wire)
        event = await recipient()
        assert event['type'] == 'http.request'
        assert event.get('more_body') is True
        assert event['body'] == b'hello'

    async def test_last_chunk_has_more_body_false(self):
        wire = b'5\r\nhello\r\n0\r\n\r\n'
        recipient = self._make_recipient(wire)
        events = []
        while True:
            event = await recipient()
            events.append(event)
            if not event.get('more_body', False):
                break
        assert events[-1].get('more_body') is False

    async def test_chunks_are_not_concatenated(self):
        wire = b'3\r\nfoo\r\n3\r\nbar\r\n0\r\n\r\n'
        recipient = self._make_recipient(wire)
        events = []
        while True:
            event = await recipient()
            events.append(event)
            if not event.get('more_body', False):
                break
        bodies = [e['body'] for e in events if e.get('body')]
        assert len(bodies) >= 2
        assert b'foo' in bodies and b'bar' in bodies


# ---------------------------------------------------------------------------
# RFC 7230 §4.1.2 — http.response.trailers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHTTPResponseTrailers:
    """HTTP1Sender must handle the http.response.trailers ASGI send event."""

    async def test_trailers_event_does_not_raise(self):
        send, _writer = _make_sender_and_writer()
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'transfer-encoding', b'chunked')]})
        await send({'type': 'http.response.body', 'body': b'hello', 'more_body': True})
        await send({'type': 'http.response.trailers',
                    'headers': [(b'x-checksum', b'abc123')], 'more_trailers': False})

    async def test_trailer_header_appears_in_wire_output(self):
        send, writer = _make_sender_and_writer()
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'transfer-encoding', b'chunked')]})
        await send({'type': 'http.response.body', 'body': b'hello', 'more_body': True})
        await send({'type': 'http.response.trailers',
                    'headers': [(b'x-checksum', b'abc123')], 'more_trailers': False})
        assert b'x-checksum: abc123' in bytes(writer.written).lower()

    async def test_trailer_comes_after_body(self):
        send, writer = _make_sender_and_writer()
        body = b'response-body'
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'transfer-encoding', b'chunked')]})
        await send({'type': 'http.response.body', 'body': body, 'more_body': True})
        await send({'type': 'http.response.trailers',
                    'headers': [(b'x-trailer', b'value')], 'more_trailers': False})
        wire = bytes(writer.written)
        body_pos = wire.find(body)
        trailer_pos = wire.lower().find(b'x-trailer')
        assert body_pos != -1 and trailer_pos != -1
        assert trailer_pos > body_pos


# ---------------------------------------------------------------------------
# Recipient unit tests
# ---------------------------------------------------------------------------

class TestAsyncioReader:
    def test_invalid_stream_raises_typeerror(self):
        from blackbull.server.recipient import AsyncioReader
        with pytest.raises(TypeError, match='read()'):
            AsyncioReader(object())


class TestHTTP1Recipient:
    def _make_reader(self, data: bytes):
        from blackbull.server.recipient import AbstractReader as _AR

        class _BufReader(_AR):
            def __init__(self, buf):
                self._buf = buf

            async def read(self, n):
                chunk, self._buf = self._buf[:n], self._buf[n:]
                return chunk

            async def readuntil(self, sep):
                idx = self._buf.find(sep)
                if idx == -1:
                    chunk, self._buf = self._buf, b''
                    return chunk
                chunk, self._buf = self._buf[:idx + len(sep)], self._buf[idx + len(sep):]
                return chunk

            async def readexactly(self, n):
                chunk, self._buf = self._buf[:n], self._buf[n:]
                return chunk

        return _BufReader(data)

    @pytest.mark.asyncio
    async def test_unsupported_transfer_encoding_raises(self):
        from blackbull.server.recipient import HTTP1Recipient
        scope = {'headers': [(b'transfer-encoding', b'gzip')]}
        reader = self._make_reader(b'')
        with pytest.raises(NotImplementedError, match='not supported'):
            HTTP1Recipient(reader, scope)

    @pytest.mark.asyncio
    async def test_second_call_returns_disconnect(self):
        from blackbull.server.recipient import HTTP1Recipient
        scope = {'headers': [(b'content-length', b'5')]}
        reader = self._make_reader(b'hello')
        r = HTTP1Recipient(reader, scope)
        first = await r()
        assert first['type'] == 'http.request'
        second = await r()
        assert second == {'type': 'http.disconnect'}

    # -- P4: Content-Length body streaming (chunk_size slices) --------------

    @pytest.mark.asyncio
    async def test_content_length_streams_in_chunks(self):
        """A body larger than chunk_size arrives as several http.request events."""
        from blackbull.server.recipient import HTTP1Recipient
        scope = {'headers': [(b'content-length', b'10')]}
        reader = self._make_reader(b'0123456789')
        r = HTTP1Recipient(reader, scope, chunk_size=4)
        events = []
        while True:
            e = await r()
            events.append(e)
            if not e.get('more_body', False):
                break
        assert [e['body'] for e in events] == [b'0123', b'4567', b'89']
        assert [e['more_body'] for e in events] == [True, True, False]
        assert b''.join(e['body'] for e in events) == b'0123456789'

    @pytest.mark.asyncio
    async def test_single_chunk_more_body_false(self):
        from blackbull.server.recipient import HTTP1Recipient
        scope = {'headers': [(b'content-length', b'5')]}
        reader = self._make_reader(b'hello')
        r = HTTP1Recipient(reader, scope, chunk_size=65536)
        event = await r()
        assert event == {'type': 'http.request', 'body': b'hello', 'more_body': False}

    @pytest.mark.asyncio
    async def test_exact_multiple_of_chunk_size(self):
        """No spurious empty trailing event when the body divides evenly."""
        from blackbull.server.recipient import HTTP1Recipient
        scope = {'headers': [(b'content-length', b'8')]}
        reader = self._make_reader(b'abcdefgh')
        r = HTTP1Recipient(reader, scope, chunk_size=4)
        events = []
        while True:
            e = await r()
            events.append(e)
            if not e.get('more_body', False):
                break
        assert [e['body'] for e in events] == [b'abcd', b'efgh']
        assert events[-1]['more_body'] is False

    @pytest.mark.asyncio
    async def test_empty_content_length_one_event(self):
        from blackbull.server.recipient import HTTP1Recipient
        scope = {'headers': [(b'content-length', b'0')]}
        reader = self._make_reader(b'')
        r = HTTP1Recipient(reader, scope, chunk_size=4)
        event = await r()
        assert event == {'type': 'http.request', 'body': b'', 'more_body': False}

    @pytest.mark.asyncio
    async def test_no_body_headers_one_empty_event(self):
        from blackbull.server.recipient import HTTP1Recipient
        scope = {'headers': []}
        reader = self._make_reader(b'')
        r = HTTP1Recipient(reader, scope, chunk_size=4)
        event = await r()
        assert event == {'type': 'http.request', 'body': b'', 'more_body': False}

    @pytest.mark.asyncio
    async def test_body_chunk_size_from_env(self, monkeypatch):
        """When chunk_size is not injected, BB_BODY_CHUNK_SIZE drives slicing."""
        import blackbull.env as env
        from blackbull.server.recipient import HTTP1Recipient
        monkeypatch.setenv('BB_BODY_CHUNK_SIZE', '3')
        env.get_settings.cache_clear()
        try:
            scope = {'headers': [(b'content-length', b'7')]}
            reader = self._make_reader(b'abcdefg')
            r = HTTP1Recipient(reader, scope)        # no chunk_size → env
            sizes = []
            while True:
                e = await r()
                sizes.append(len(e['body']))
                if not e.get('more_body', False):
                    break
            assert sizes == [3, 3, 1]
        finally:
            env.get_settings.cache_clear()


class TestHTTP2Recipient:
    @pytest.mark.asyncio
    async def test_with_initial_frame_enqueues_event(self):
        from blackbull.server.recipient import HTTP2Recipient
        from blackbull.protocol.frame_types import Data
        frame = MagicMock(spec=Data)
        frame.payload = b'body'
        frame.end_stream = True
        r = HTTP2Recipient(frame)
        event = await r()
        assert event == {'type': 'http.request', 'body': b'body', 'more_body': False}


class TestWebSocketRecipientUnsupportedOpcode:
    @pytest.mark.asyncio
    async def test_unknown_opcode_sends_close_and_disconnects(self):
        """RFC 6455 §5.2: unknown opcode must send CLOSE(1002) and return disconnect."""
        from blackbull.server.recipient import WebSocketRecipient
        from blackbull.server.ws_codec import WSFrameHeader, encode_frame

        fake_header = WSFrameHeader(fin=True, rsv1=False, rsv2=False, rsv3=False,
                                    opcode=0x03, masked=False, length=2)

        writer = MagicMock(spec_set=AbstractWriter)
        writer.write = AsyncMock()

        r = WebSocketRecipient(_FakeReader(b''), writer, require_masked=False)
        r._connect_sent = True

        eof = asyncio.IncompleteReadError(b'', 2)
        with patch('blackbull.server.recipient.read_frame_header',
                   AsyncMock(side_effect=[fake_header, eof])):
            with patch('blackbull.server.recipient.read_payload',
                       AsyncMock(return_value=b'\x00\x00')):
                event = await r()

        assert event['type'] == 'websocket.disconnect'
        assert event['code'] == 1002
        close_1002 = encode_frame((1002).to_bytes(2, 'big'), opcode=0x8)
        writer.write.assert_called_once_with(close_1002)

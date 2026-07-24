"""
Tests for HTTP/1.1 chunked Transfer-Encoding, StreamingResponse,
Compression streaming safety, and app.use() registration.
"""
import functools
import warnings

import pytest

from blackbull.server.sender import HTTP1Sender, AbstractWriter
from blackbull.response import StreamingResponse
from blackbull.middleware.compression import Compression
from blackbull.app import BlackBull


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class BytesWriter(AbstractWriter):
    """Collects all written bytes in order for assertion."""
    def __init__(self):
        self.data = b''

    async def write(self, data: bytes) -> None:
        self.data += data


class _SendfileBytesWriter(BytesWriter):
    """Captures the sendfile call and also reads the file into ``data``
    so tests can assert on the on-the-wire byte stream.

    *raises_notimplemented* (default False) makes ``sendfile`` raise
    NotImplementedError to exercise the chunked fallback the sender
    falls back to on TLS transports.
    """

    def __init__(self, raises_notimplemented: bool = False):
        super().__init__()
        self.sendfile_calls: list[tuple[int, int]] = []
        self.raises_notimplemented = raises_notimplemented

    async def sendfile(self, file, offset: int, count: int) -> int:
        if self.raises_notimplemented:
            raise NotImplementedError('TLS transport not supported')
        self.sendfile_calls.append((offset, count))
        file.seek(offset)
        self.data += file.read(count)
        return count


def _make_scope(type_: str = 'http'):
    scope = {'type': type_, 'method': 'GET', 'path': '/', 'headers': []}
    if type_ == 'http':
        from blackbull.connection import Connection
        return Connection.from_scope(scope)
    return scope


# ---------------------------------------------------------------------------
# TestHTTP1SenderChunked
# ---------------------------------------------------------------------------

class TestHTTP1SenderChunked:
    @pytest.mark.asyncio
    async def test_single_body_uses_content_length(self):
        w = BytesWriter()
        s = HTTP1Sender(w)
        await s({'type': 'http.response.start', 'status': 200, 'headers': []})
        await s({'type': 'http.response.body', 'body': b'hello', 'more_body': False})
        assert b'content-length: 5' in w.data.lower()
        assert b'transfer-encoding' not in w.data.lower()
        assert b'hello' in w.data

    @pytest.mark.asyncio
    async def test_chunked_header_added_when_more_body(self):
        w = BytesWriter()
        s = HTTP1Sender(w)
        await s({'type': 'http.response.start', 'status': 200, 'headers': []})
        await s({'type': 'http.response.body', 'body': b'chunk1', 'more_body': True})
        await s({'type': 'http.response.body', 'body': b'', 'more_body': False})
        assert b'transfer-encoding: chunked' in w.data.lower()
        assert b'content-length' not in w.data.lower()

    @pytest.mark.asyncio
    async def test_first_chunk_is_framed(self):
        w = BytesWriter()
        s = HTTP1Sender(w)
        await s({'type': 'http.response.start', 'status': 200, 'headers': []})
        await s({'type': 'http.response.body', 'body': b'hello', 'more_body': True})
        # "hello" is 5 bytes → "5\r\nhello\r\n"
        assert b'5\r\nhello\r\n' in w.data

    @pytest.mark.asyncio
    async def test_subsequent_chunks_are_framed(self):
        w = BytesWriter()
        s = HTTP1Sender(w)
        await s({'type': 'http.response.start', 'status': 200, 'headers': []})
        await s({'type': 'http.response.body', 'body': b'chunk1', 'more_body': True})
        await s({'type': 'http.response.body', 'body': b'chunk2', 'more_body': True})
        await s({'type': 'http.response.body', 'body': b'', 'more_body': False})
        assert b'6\r\nchunk1\r\n' in w.data
        assert b'6\r\nchunk2\r\n' in w.data

    @pytest.mark.asyncio
    async def test_terminal_zero_chunk_on_final(self):
        w = BytesWriter()
        s = HTTP1Sender(w)
        await s({'type': 'http.response.start', 'status': 200, 'headers': []})
        await s({'type': 'http.response.body', 'body': b'data', 'more_body': True})
        await s({'type': 'http.response.body', 'body': b'', 'more_body': False})
        assert w.data.endswith(b'0\r\n\r\n')

    @pytest.mark.asyncio
    async def test_existing_transfer_encoding_not_duplicated(self):
        w = BytesWriter()
        s = HTTP1Sender(w)
        await s({'type': 'http.response.start', 'status': 200,
                 'headers': [(b'transfer-encoding', b'chunked')]})
        await s({'type': 'http.response.body', 'body': b'x', 'more_body': True})
        await s({'type': 'http.response.body', 'body': b'', 'more_body': False})
        assert w.data.lower().count(b'transfer-encoding') == 1


# ---------------------------------------------------------------------------
# TestHTTP1SenderPathsend  (Sprint 31)
# ---------------------------------------------------------------------------

class TestHTTP1SenderPathsend:
    """``http.response.pathsend`` ASGI extension — the sender writes the
    buffered ``http.response.start`` headers, then hands the file path to
    ``writer.sendfile`` (zero-copy on cleartext transports)."""

    @staticmethod
    def _write_tmp(tmp_path, name: str, payload: bytes) -> str:
        p = tmp_path / name
        p.write_bytes(payload)
        return str(p)

    @pytest.mark.asyncio
    async def test_writes_headers_then_sendfiles_body(self, tmp_path):
        path = self._write_tmp(tmp_path, 'body.bin', b'A' * 4096)
        w = _SendfileBytesWriter()
        s = HTTP1Sender(w)
        await s({'type': 'http.response.start', 'status': 200,
                 'headers': [(b'content-type', b'application/octet-stream'),
                             (b'content-length', b'4096')]})
        await s({'type': 'http.response.pathsend', 'path': path})

        assert w.sendfile_calls == [(0, 4096)]
        # Status line + headers precede the body bytes on the wire.
        head_end = w.data.find(b'\r\n\r\n') + 4
        assert head_end > 4
        assert w.data[:head_end].startswith(b'HTTP/1.1 200')
        assert b'content-length: 4096' in w.data[:head_end].lower()
        assert w.data[head_end:] == b'A' * 4096

    @pytest.mark.asyncio
    async def test_computes_content_length_when_caller_omitted_it(self, tmp_path):
        """ASGI spec says the application should set Content-Length, but
        forgetting it is the most common mistake.  Make it work anyway —
        the sender knows the file size and the cost is one stat syscall
        we'd do regardless."""
        path = self._write_tmp(tmp_path, 'body.bin', b'B' * 99)
        w = _SendfileBytesWriter()
        s = HTTP1Sender(w)
        await s({'type': 'http.response.start', 'status': 200,
                 'headers': [(b'content-type', b'application/octet-stream')]})
        await s({'type': 'http.response.pathsend', 'path': path})

        head = w.data.split(b'\r\n\r\n', 1)[0].lower()
        assert b'content-length: 99' in head

    @pytest.mark.asyncio
    async def test_falls_back_to_chunked_read_on_tls(self, tmp_path):
        """When ``writer.sendfile`` raises NotImplementedError (SSL
        transport, mocked test) the sender reads the file in chunks
        via ``asyncio.to_thread`` and writes them through the normal
        ``_write`` path so TLS connections still serve the body."""
        payload = b'C' * 200000   # > one fallback chunk (64 KiB)
        path = self._write_tmp(tmp_path, 'body.bin', payload)
        w = _SendfileBytesWriter(raises_notimplemented=True)
        s = HTTP1Sender(w)
        await s({'type': 'http.response.start', 'status': 200,
                 'headers': [(b'content-length', str(len(payload)).encode())]})
        await s({'type': 'http.response.pathsend', 'path': path})

        # No successful sendfile (the call raised).  Body still arrived.
        assert w.sendfile_calls == []
        head_end = w.data.find(b'\r\n\r\n') + 4
        assert w.data[head_end:] == payload

    @pytest.mark.asyncio
    async def test_head_request_writes_headers_only(self, tmp_path):
        """HEAD response carries no body even when the path is sent.
        Content-Length still reflects the file size so caches and
        proxies don't get confused."""
        path = self._write_tmp(tmp_path, 'body.bin', b'X' * 1234)
        w = _SendfileBytesWriter()
        s = HTTP1Sender(w)
        s._head_mode = True   # the actor sets this for HEAD requests
        await s({'type': 'http.response.start', 'status': 200,
                 'headers': [(b'content-length', b'1234')]})
        await s({'type': 'http.response.pathsend', 'path': path})

        assert w.sendfile_calls == []
        assert b'content-length: 1234' in w.data.lower()
        assert b'X' * 10 not in w.data   # body bytes never written

    @pytest.mark.asyncio
    async def test_pathsend_without_buffered_start_is_a_noop_warning(self, tmp_path, caplog):
        """Defensive: misuse (pathsend without prior start) logs a
        warning and drops the event rather than crashing the
        connection."""
        path = self._write_tmp(tmp_path, 'body.bin', b'Z')
        w = _SendfileBytesWriter()
        s = HTTP1Sender(w)
        with caplog.at_level('WARNING', logger='blackbull.server.sender'):
            await s({'type': 'http.response.pathsend', 'path': path})
        assert w.data == b''
        assert any('pathsend without buffered start' in r.message
                   for r in caplog.records)


# ---------------------------------------------------------------------------
# TestStreamingResponse
# ---------------------------------------------------------------------------

class TestStreamingResponse:
    @pytest.mark.asyncio
    async def test_sends_start_then_chunks_then_final(self):
        events: list = []

        async def send(event):
            events.append(event)

        async def gen():
            yield b'part1'
            yield b'part2'

        await StreamingResponse(gen())(_make_scope(), None, send)

        assert events[0]['type'] == 'http.response.start'
        body_events = [e for e in events if e['type'] == 'http.response.body']
        assert all(e['more_body'] for e in body_events[:-1])
        assert not body_events[-1]['more_body']
        bodies = b''.join(e['body'] for e in body_events)
        assert bodies == b'part1part2'

    @pytest.mark.asyncio
    async def test_str_chunks_encoded_to_bytes(self):
        events: list = []

        async def send(event):
            events.append(event)

        async def gen():
            yield 'hello'
            yield 'world'

        await StreamingResponse(gen())(_make_scope(), None, send)
        body_bytes = b''.join(e['body'] for e in events if e['type'] == 'http.response.body')
        assert body_bytes == b'helloworld'

    @pytest.mark.asyncio
    async def test_default_content_type_header(self):
        events: list = []

        async def send(event):
            events.append(event)

        async def gen():
            yield b'x'

        await StreamingResponse(gen())(_make_scope(), None, send)
        start = events[0]
        header_keys = [k for k, _ in start['headers']]
        assert b'content-type' in header_keys

    @pytest.mark.asyncio
    async def test_custom_status_and_headers(self):
        events: list = []

        async def send(event):
            events.append(event)

        async def gen():
            yield b'x'

        await StreamingResponse(gen(), status=201,
                                headers=[(b'x-custom', b'yes')])(_make_scope(), None, send)
        start = events[0]
        assert start['status'] == 201
        header_names = [k for k, _ in start['headers']]
        assert b'x-custom' in header_names


# ---------------------------------------------------------------------------
# TestCompressionStreaming
# ---------------------------------------------------------------------------

class TestCompressionStreaming:
    @pytest.mark.asyncio
    async def test_streaming_not_compressed(self):
        mw = Compression(min_size=1)
        from blackbull.connection import Connection
        scope = Connection.from_scope({
            'type': 'http',
            'headers': [(b'accept-encoding', b'gzip')],
        })
        received: list = []

        async def call_next(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'x' * 200, 'more_body': True})
            await send({'type': 'http.response.body', 'body': b'y' * 200, 'more_body': False})

        async def send(event):
            received.append(event)

        await mw(scope, None, send, call_next)

        start = next(e for e in received if e.get('type') == 'http.response.start')
        encoding_headers = [v for k, v in start.get('headers', []) if k == b'content-encoding']
        assert not encoding_headers

    @pytest.mark.asyncio
    async def test_single_body_still_compressed(self):
        import gzip
        mw = Compression(min_size=1)
        from blackbull.connection import Connection
        scope = Connection.from_scope({
            'type': 'http',
            'headers': [(b'accept-encoding', b'gzip')],
        })
        received: list = []

        async def call_next(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'hello world', 'more_body': False})

        async def send(event):
            received.append(event)

        await mw(scope, None, send, call_next)

        start = next(e for e in received if e.get('type') == 'http.response.start')
        encoding_headers = [v for k, v in start.get('headers', []) if k == b'content-encoding']
        assert b'gzip' in encoding_headers

        body_event = next(e for e in received if e.get('type') == 'http.response.body')
        assert gzip.decompress(body_event['body']) == b'hello world'


# ---------------------------------------------------------------------------
# TestUseWarning
# ---------------------------------------------------------------------------

class TestUseWarning:
    def test_function_middleware_no_warn(self):
        async def fn_mw(scope, receive, send, call_next):
            await call_next(scope, receive, send)

        app = BlackBull()
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter('always')
            app.use(fn_mw)
        user_warnings = [x for x in w if issubclass(x.category, UserWarning)]
        assert not user_warnings

    def test_partial_no_warn(self):
        async def base_mw(scope, receive, send, call_next, extra=None):
            await call_next(scope, receive, send)

        app = BlackBull()
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter('always')
            app.use(functools.partial(base_mw, extra='value'))
        user_warnings = [x for x in w if issubclass(x.category, UserWarning)]
        assert not user_warnings


# ---------------------------------------------------------------------------
# TestHTTP1SenderRenderStart  (Sprint refactor — lock the wire format)
# ---------------------------------------------------------------------------

class TestHTTP1SenderRenderStart:
    def test_status_line_and_headers_format(self):
        from http import HTTPStatus
        s = HTTP1Sender(BytesWriter())
        out = s._render_start(
            HTTPStatus.OK,
            [(b'content-length', b'5'), (b'Date', b'Mon, 01 Jan 2024 00:00:00 GMT')],
        )
        assert out == (
            b'HTTP/1.1 200 OK\r\n'
            b'content-length: 5\r\n'
            b'Date: Mon, 01 Jan 2024 00:00:00 GMT\r\n'
            b'\r\n'
        )

    def test_empty_headers(self):
        from http import HTTPStatus
        s = HTTP1Sender(BytesWriter())
        out = s._render_start(HTTPStatus.NO_CONTENT, [])
        assert out == b'HTTP/1.1 204 No Content\r\n\r\n'

    def test_reset_per_request_state_clears_all_slots(self):
        # Sprint refactor — guard against the "I added a slot and forgot to
        # reset it" regression that bit Sprint 38 (BB_REQUEST_TIMEOUT 408
        # skipped on the second keep-alive request because `_started`
        # stayed True).
        from http import HTTPStatus
        from blackbull.headers import Headers
        s = HTTP1Sender(BytesWriter())
        s._started = True
        s._chunked = True
        s._buffered_status = HTTPStatus.OK
        s._buffered_headers = Headers([(b'a', b'b')])
        s._expect_trailers = True
        s._head_mode = True
        s._log_record = object()
        s.reset_per_request_state()
        assert s._started is False
        assert s._chunked is False
        assert s._buffered_status is None
        assert s._buffered_headers is None
        assert s._expect_trailers is False
        assert s._head_mode is False
        assert s._log_record is None

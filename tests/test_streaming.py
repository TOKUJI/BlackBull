"""
Tests for HTTP/1.1 chunked Transfer-Encoding, StreamingResponse,
StreamingAwareMiddleware, CompressionMiddleware streaming safety,
and the use() warning for class-based middlewares.
"""
import functools
import warnings

import pytest

from blackbull.server.sender import HTTP1Sender, AbstractWriter
from blackbull.response import StreamingResponse
from blackbull.middleware.base import StreamingAwareMiddleware
from blackbull.middleware.compression import CompressionMiddleware
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


def _make_scope(type_: str = 'http') -> dict:
    return {'type': type_, 'method': 'GET', 'path': '/', 'headers': []}


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
# TestStreamingAwareMiddleware
# ---------------------------------------------------------------------------

class TestStreamingAwareMiddleware:
    @pytest.mark.asyncio
    async def test_noop_passes_through_single_body(self):
        mw = StreamingAwareMiddleware()
        received: list = []

        async def call_next(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'hello', 'more_body': False})

        async def send(event):
            received.append(event)

        await mw(_make_scope(), None, send, call_next)

        assert received[0]['type'] == 'http.response.start'
        body_event = next(e for e in received if e['type'] == 'http.response.body')
        assert body_event['body'] == b'hello'

    @pytest.mark.asyncio
    async def test_on_response_body_hook_transforms(self):
        class UpperMiddleware(StreamingAwareMiddleware):
            async def on_response_body(self, start, body):
                return start, body.upper()

        mw = UpperMiddleware()
        received: list = []

        async def call_next(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'hello', 'more_body': False})

        async def send(event):
            received.append(event)

        await mw(_make_scope(), None, send, call_next)

        body_event = next(e for e in received if e['type'] == 'http.response.body')
        assert body_event['body'] == b'HELLO'

    @pytest.mark.asyncio
    async def test_streaming_bypasses_on_response_body(self):
        called: list = []

        class TrackingMiddleware(StreamingAwareMiddleware):
            async def on_response_body(self, start, body):
                called.append('on_response_body')
                return start, body

            async def on_response_start(self, start):
                called.append('on_response_start')
                return start

        mw = TrackingMiddleware()
        received: list = []

        async def call_next(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'chunk', 'more_body': True})
            await send({'type': 'http.response.body', 'body': b'', 'more_body': False})

        async def send(event):
            received.append(event)

        await mw(_make_scope(), None, send, call_next)

        assert 'on_response_start' in called
        assert 'on_response_body' not in called

    @pytest.mark.asyncio
    async def test_streaming_chunks_passed_through(self):
        mw = StreamingAwareMiddleware()
        received: list = []

        async def call_next(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'a', 'more_body': True})
            await send({'type': 'http.response.body', 'body': b'b', 'more_body': False})

        async def send(event):
            received.append(event)

        await mw(_make_scope(), None, send, call_next)

        body_events = [e for e in received if e['type'] == 'http.response.body']
        bodies = b''.join(e['body'] for e in body_events)
        assert bodies == b'ab'

    @pytest.mark.asyncio
    async def test_non_http_scope_passed_through(self):
        mw = StreamingAwareMiddleware()
        received_scope: list = []

        async def call_next(scope, receive, send):
            received_scope.append(scope)

        async def send(event):
            pass

        scope = {'type': 'websocket'}
        await mw(scope, None, send, call_next)
        assert received_scope[0] is scope


# ---------------------------------------------------------------------------
# TestCompressionMiddlewareStreaming
# ---------------------------------------------------------------------------

class TestCompressionMiddlewareStreaming:
    @pytest.mark.asyncio
    async def test_streaming_not_compressed(self):
        mw = CompressionMiddleware(min_size=1)
        scope = {
            'type': 'http',
            'headers': [(b'accept-encoding', b'gzip')],
        }
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
        mw = CompressionMiddleware(min_size=1)
        scope = {
            'type': 'http',
            'headers': [(b'accept-encoding', b'gzip')],
        }
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
    def test_class_middleware_warns(self):
        class BareMiddleware:
            async def __call__(self, scope, receive, send, call_next):
                await call_next(scope, receive, send)

        app = BlackBull()
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter('always')
            app.use(BareMiddleware())
        user_warnings = [x for x in w if issubclass(x.category, UserWarning)]
        assert user_warnings
        assert any('StreamingAwareMiddleware' in str(x.message) for x in user_warnings)

    def test_streaming_aware_no_warn(self):
        class GoodMiddleware(StreamingAwareMiddleware):
            pass

        app = BlackBull()
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter('always')
            app.use(GoodMiddleware())
        user_warnings = [x for x in w if issubclass(x.category, UserWarning)]
        assert not user_warnings

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

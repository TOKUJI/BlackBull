"""
Tests for blackbull/middleware/
================================

websocket()
-----------
The ``websocket`` middleware must:
  * Accept a first ``receive`` event whose ``type`` is ``'websocket.connect'``
    regardless of any additional keys in the dict (ASGI spec allows extra fields).
  * Reject (raise ``ValueError``) when the first event has a different or absent
    ``type``.
  * Send ``websocket.accept`` before delegating to ``inner``.
  * Send ``websocket.close`` after ``inner`` returns.

P1 bug: the original implementation uses ``msg != {'type': 'websocket.connect'}``
(exact dict equality), which incorrectly rejects valid ASGI connect events that
carry extra fields such as ``headers`` or ``subprotocols``.  The fix is to check
``msg.get('type') != 'websocket.connect'`` instead.
"""

import gzip
import pytest
from unittest.mock import AsyncMock
from blackbull.middleware import websocket
from blackbull.middleware.compression import CompressionMiddleware, compress
from blackbull.server.headers import Headers

try:
    import brotli as _brotli
    _HAVE_BROTLI = True
except ImportError:
    _HAVE_BROTLI = False

try:
    import zstandard as _zstd
    _HAVE_ZSTD = True
except ImportError:
    _HAVE_ZSTD = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_receive(*events):
    """Return an async callable that pops and returns events in order."""
    queue = list(events)

    async def receive():
        return queue.pop(0)

    return receive


async def noop_send(event):
    pass


async def noop_inner(scope, receive, send):
    pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestWebsocketMiddleware:

    # --- happy-path acceptance -------------------------------------------------

    async def test_plain_connect_event_is_accepted(self):
        """Exact ``{'type': 'websocket.connect'}`` must be accepted."""
        called = []

        async def inner(scope, receive, send):
            called.append(True)

        receive = make_receive({'type': 'websocket.connect'})
        await websocket({}, receive, noop_send, inner)
        assert called

    async def test_connect_event_with_extra_headers_is_accepted(self):
        """Connect event carrying a ``headers`` field must still be accepted.

        This is the P1 bug: exact dict equality rejects this valid ASGI event.
        The fix (``msg.get('type') != 'websocket.connect'``) accepts it.
        """
        called = []

        async def inner(scope, receive, send):
            called.append(True)

        connect = {
            'type': 'websocket.connect',
            'headers': [(b'host', b'localhost'), (b'origin', b'https://example.com')],
        }
        receive = make_receive(connect)
        await websocket({}, receive, noop_send, inner)
        assert called

    async def test_connect_event_with_subprotocols_is_accepted(self):
        """Connect event carrying a ``subprotocols`` field must be accepted."""
        called = []

        async def inner(scope, receive, send):
            called.append(True)

        connect = {
            'type': 'websocket.connect',
            'subprotocols': ['chat', 'superchat'],
        }
        receive = make_receive(connect)
        await websocket({}, receive, noop_send, inner)
        assert called

    async def test_connect_event_with_multiple_extra_keys_is_accepted(self):
        """Any combination of extra ASGI fields must not cause rejection."""
        called = []

        async def inner(scope, receive, send):
            called.append(True)

        connect = {
            'type': 'websocket.connect',
            'headers': [(b'host', b'localhost')],
            'subprotocols': ['v1'],
            'extensions': {'permessage-deflate': {}},
        }
        receive = make_receive(connect)
        await websocket({}, receive, noop_send, inner)
        assert called

    # --- rejection -------------------------------------------------------------

    async def test_non_connect_type_raises_value_error(self):
        """First event with wrong type must raise ``ValueError``."""
        receive = make_receive({'type': 'websocket.receive', 'text': 'hi'})
        with pytest.raises(ValueError):
            await websocket({}, receive, noop_send, noop_inner)

    async def test_missing_type_key_raises_value_error(self):
        """First event with no ``type`` key must raise ``ValueError``."""
        receive = make_receive({})
        with pytest.raises(ValueError):
            await websocket({}, receive, noop_send, noop_inner)

    async def test_http_request_event_raises_value_error(self):
        """An HTTP request event must raise ``ValueError`` (wrong protocol)."""
        receive = make_receive({'type': 'http.request', 'body': b''})
        with pytest.raises(ValueError):
            await websocket({}, receive, noop_send, noop_inner)

    # --- ordering of accept / inner / close ------------------------------------

    async def test_accept_is_sent_before_inner_is_called(self):
        """``websocket.accept`` must be sent before ``inner`` is invoked."""
        order = []

        async def tracking_send(event):
            order.append(event.get('type'))

        async def inner(scope, receive, send):
            order.append('inner')

        receive = make_receive({'type': 'websocket.connect'})
        await websocket({}, receive, tracking_send, inner)

        assert 'websocket.accept' in order
        assert 'inner' in order
        assert order.index('websocket.accept') < order.index('inner')

    async def test_close_is_sent_after_inner_returns(self):
        """``websocket.close`` must be sent after ``inner`` returns."""
        order = []

        async def tracking_send(event):
            order.append(event.get('type'))

        async def inner(scope, receive, send):
            order.append('inner')

        receive = make_receive({'type': 'websocket.connect'})
        await websocket({}, receive, tracking_send, inner)

        assert 'websocket.close' in order
        assert 'inner' in order
        assert order.index('inner') < order.index('websocket.close')

    async def test_full_sequence_is_accept_then_inner_then_close(self):
        """Complete event sequence must be: accept → inner → close."""
        order = []

        async def tracking_send(event):
            order.append(event.get('type'))

        async def inner(scope, receive, send):
            order.append('inner')

        receive = make_receive({'type': 'websocket.connect'})
        await websocket({}, receive, tracking_send, inner)

        assert order == ['websocket.accept', 'inner', 'websocket.close']


# ---------------------------------------------------------------------------
# compress() — Accept-Encoding parsing
# ---------------------------------------------------------------------------

class TestAcceptEncodingParsing:
    """CompressionMiddleware._parse_accept_encoding must return codec names
    sorted by descending q-value."""

    def _parse(self, header: str) -> list[str]:
        return CompressionMiddleware._parse_accept_encoding(header.encode())

    def test_single_codec(self):
        assert self._parse('gzip') == ['gzip']

    def test_multiple_no_q(self):
        result = self._parse('gzip, br')
        assert set(result) == {'gzip', 'br'}

    def test_q_values_sorted_descending(self):
        result = self._parse('gzip;q=0.8, br;q=1.0, zstd;q=0.9')
        assert result == ['br', 'zstd', 'gzip']

    def test_implicit_q1_beats_explicit_lower(self):
        result = self._parse('br, gzip;q=0.5')
        assert result[0] == 'br'
        assert result[-1] == 'gzip'

    def test_empty_header_returns_empty(self):
        assert self._parse('') == []

    def test_wildcard_included(self):
        result = self._parse('*;q=0.1, gzip')
        assert 'gzip' in result
        assert '*' in result


class TestCodecSelection:
    """CompressionMiddleware._select_codec must prefer br > zstd > gzip
    among codecs that are both accepted by the client and available on
    the server."""

    def _mw(self, available: list[str]) -> CompressionMiddleware:
        import gzip as _gz
        mw = CompressionMiddleware()
        mw._available = {name: _gz.compress for name in available}
        return mw

    def test_prefers_br_over_gzip(self):
        mw = self._mw(['br', 'gzip'])
        codec, _ = mw._select_codec(b'br, gzip')
        assert codec == 'br'

    def test_prefers_zstd_over_gzip(self):
        mw = self._mw(['zstd', 'gzip'])
        codec, _ = mw._select_codec(b'zstd, gzip')
        assert codec == 'zstd'

    def test_falls_back_to_gzip_when_br_unavailable(self):
        mw = self._mw(['gzip'])
        codec, _ = mw._select_codec(b'br, gzip')
        assert codec == 'gzip'

    def test_returns_none_when_no_overlap(self):
        mw = self._mw(['gzip'])
        assert mw._select_codec(b'br, zstd') is None

    def test_returns_none_for_empty_accept(self):
        mw = self._mw(['gzip'])
        assert mw._select_codec(b'') is None


# ---------------------------------------------------------------------------
# compress() — end-to-end middleware behaviour
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHTTPCompression:
    """compress() must compress response bodies based on the client's
    Accept-Encoding header (gzip / br).

    BlackBull middleware convention: async def compress(scope, receive, send, call_next)
    """

    async def test_gzip_body_compressed_when_accept_encoding_gzip(self):
        """When Accept-Encoding: gzip, response body must be gzip-compressed."""
        body = b'Hello, world!' * 50  # repetitive data compresses well

        async def handler(_scope, _receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': body, 'more_body': False})

        scope = {
            'type': 'http',
            'headers': Headers([(b'accept-encoding', b'gzip')]),
        }
        events = []

        async def capture_send(event):
            events.append(event)

        await compress(scope, AsyncMock(return_value={'type': 'http.disconnect'}),
                       capture_send, call_next=handler)

        body_event = next(e for e in events if e.get('type') == 'http.response.body')
        decompressed = gzip.decompress(body_event['body'])
        assert decompressed == body, (
            f'Decompressed body must match original; got {decompressed!r}'
        )

    async def test_content_encoding_header_added_when_gzip(self):
        """Gzip-compressed response must include Content-Encoding: gzip."""
        async def handler(_scope, _receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'data' * 100, 'more_body': False})

        scope = {
            'type': 'http',
            'headers': Headers([(b'accept-encoding', b'gzip')]),
        }
        events = []

        async def capture_send(event):
            events.append(event)

        await compress(scope, AsyncMock(return_value={'type': 'http.disconnect'}),
                       capture_send, call_next=handler)

        start = next(e for e in events if e.get('type') == 'http.response.start')
        header_dict = {k.lower(): v.lower() for k, v in start.get('headers', [])}
        assert b'content-encoding' in header_dict, (
            f'Expected Content-Encoding header; got {header_dict}'
        )
        assert header_dict[b'content-encoding'] == b'gzip', (
            f'Expected content-encoding: gzip; got {header_dict[b"content-encoding"]!r}'
        )

    async def test_no_compression_without_accept_encoding(self):
        """Without Accept-Encoding, the body must be sent as-is."""
        body = b'plain response'

        async def handler(_scope, _receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': body, 'more_body': False})

        scope = {'type': 'http', 'headers': Headers([])}
        events = []

        async def capture_send(event):
            events.append(event)

        await compress(scope, AsyncMock(return_value={'type': 'http.disconnect'}),
                       capture_send, call_next=handler)

        body_event = next(e for e in events if e.get('type') == 'http.response.body')
        assert body_event['body'] == body, (
            f'Body must be unmodified without Accept-Encoding; got {body_event["body"]!r}'
        )

    async def test_small_body_not_compressed(self):
        """Bodies below a minimum size threshold must not be compressed
        (compression overhead exceeds savings for tiny payloads)."""
        body = b'hi'

        async def handler(_scope, _receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': body, 'more_body': False})

        scope = {
            'type': 'http',
            'headers': Headers([(b'accept-encoding', b'gzip')]),
        }
        events = []

        async def capture_send(event):
            events.append(event)

        await compress(scope, AsyncMock(return_value={'type': 'http.disconnect'}),
                       capture_send, call_next=handler)

        body_event = next(e for e in events if e.get('type') == 'http.response.body')
        assert body_event['body'] == body, (
            f'Tiny body must not be compressed; got {body_event["body"]!r}'
        )


@pytest.mark.asyncio
@pytest.mark.skipif(not _HAVE_BROTLI, reason='brotli not installed')
class TestBrotliCompression:
    """compress() must use Brotli when the client accepts it and brotli is installed."""

    async def test_br_body_decompresses_correctly(self):
        body = b'Hello, Brotli!' * 50

        async def handler(_scope, _receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': body, 'more_body': False})

        scope = {'type': 'http', 'headers': Headers([(b'accept-encoding', b'br')])}
        events = []

        async def capture_send(event):
            events.append(event)

        await compress(scope, AsyncMock(return_value={'type': 'http.disconnect'}),
                       capture_send, call_next=handler)

        body_event = next(e for e in events if e.get('type') == 'http.response.body')
        assert _brotli.decompress(body_event['body']) == body

    async def test_br_content_encoding_header(self):
        async def handler(_scope, _receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'x' * 200, 'more_body': False})

        scope = {'type': 'http', 'headers': Headers([(b'accept-encoding', b'br')])}
        events = []

        async def capture_send(event):
            events.append(event)

        await compress(scope, AsyncMock(return_value={'type': 'http.disconnect'}),
                       capture_send, call_next=handler)

        start = next(e for e in events if e.get('type') == 'http.response.start')
        header_dict = {k: v for k, v in start.get('headers', [])}
        assert header_dict.get(b'content-encoding') == b'br'

    async def test_br_preferred_over_gzip(self):
        """When client accepts both br and gzip, br must be chosen."""
        async def handler(_scope, _receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'x' * 200, 'more_body': False})

        scope = {'type': 'http', 'headers': Headers([(b'accept-encoding', b'gzip, br')])}
        events = []

        async def capture_send(event):
            events.append(event)

        await compress(scope, AsyncMock(return_value={'type': 'http.disconnect'}),
                       capture_send, call_next=handler)

        start = next(e for e in events if e.get('type') == 'http.response.start')
        header_dict = {k: v for k, v in start.get('headers', [])}
        assert header_dict.get(b'content-encoding') == b'br'


@pytest.mark.asyncio
@pytest.mark.skipif(not _HAVE_ZSTD, reason='zstandard not installed')
class TestZstdCompression:
    """compress() must use Zstandard when the client accepts it and zstandard is installed."""

    async def test_zstd_body_decompresses_correctly(self):
        body = b'Hello, Zstandard!' * 50

        async def handler(_scope, _receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': body, 'more_body': False})

        scope = {'type': 'http', 'headers': Headers([(b'accept-encoding', b'zstd')])}
        events = []

        async def capture_send(event):
            events.append(event)

        await compress(scope, AsyncMock(return_value={'type': 'http.disconnect'}),
                       capture_send, call_next=handler)

        body_event = next(e for e in events if e.get('type') == 'http.response.body')
        dctx = _zstd.ZstdDecompressor()
        assert dctx.decompress(body_event['body']) == body

    async def test_zstd_content_encoding_header(self):
        async def handler(_scope, _receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'x' * 200, 'more_body': False})

        scope = {'type': 'http', 'headers': Headers([(b'accept-encoding', b'zstd')])}
        events = []

        async def capture_send(event):
            events.append(event)

        await compress(scope, AsyncMock(return_value={'type': 'http.disconnect'}),
                       capture_send, call_next=handler)

        start = next(e for e in events if e.get('type') == 'http.response.start')
        header_dict = {k: v for k, v in start.get('headers', [])}
        assert header_dict.get(b'content-encoding') == b'zstd'

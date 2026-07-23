"""Unit tests for the opt-in HTTP handler context object — now
:class:`blackbull.connection.Connection` (Sprint 79 Phase 5; ``Request`` is a
deprecated alias of it).

Pure-unit layer: hand-built scope dicts and fake receive callables, no I/O.
The context object is built from an ASGI scope via ``Connection.from_scope``
and exposes the request-body helpers (read_body / read_json / read_text /
parse_cookies) as cached methods; these tests pin the property surface, the
dual header shapes (plain ASGI pair-list vs blackbull.headers.Headers), and the
single-drain body cache.
"""
import pytest

from blackbull.headers import Headers
from blackbull.connection import Connection, ClientDisconnected


def _conn(scope=None, receive=None) -> Connection:
    """Build a Connection from a partial scope, filling the ASGI-required
    identity keys so ``from_scope`` has a complete record to work from."""
    scope = dict(scope or {})
    scope.setdefault('type', 'http')
    scope.setdefault('method', 'GET')
    scope.setdefault('path', '/')
    scope.setdefault('headers', [])
    return Connection.from_scope(scope, receive)


def make_receive(events):
    """Return (receive, calls) where calls counts how often receive ran."""
    calls = []
    it = iter(events)

    async def receive():
        calls.append(True)
        return next(it)

    return receive, calls


def single_body_receive(body: bytes):
    return make_receive([{'type': 'http.request', 'body': body, 'more_body': False}])


# ---------------------------------------------------------------------------
# Scope-backed properties
# ---------------------------------------------------------------------------

class TestScopeProperties:
    def test_method_path_scheme_client(self):
        req = _conn({
            'type': 'http', 'method': 'POST', 'path': '/items/7',
            'scheme': 'https', 'client': ['127.0.0.1', 54321], 'headers': [],
        })
        assert req.method == 'POST'
        assert req.path == '/items/7'
        assert req.scheme == 'https'
        assert req.client == ('127.0.0.1', 54321)

    def test_client_absent_is_none(self):
        req = _conn({'type': 'http', 'method': 'GET', 'path': '/', 'headers': []})
        assert req.client is None

    def test_as_scope_round_trips_identity(self):
        # Connection has no raw-scope escape hatch; as_scope() is the derived
        # view and must faithfully reflect the request identity.
        req = _conn({'type': 'http', 'method': 'GET', 'path': '/x', 'headers': []})
        scope = req.as_scope()
        assert scope['type'] == 'http'
        assert scope['method'] == 'GET'
        assert scope['path'] == '/x'


# ---------------------------------------------------------------------------
# Headers — both scope shapes (ASGI pair-list and Headers instance)
# ---------------------------------------------------------------------------

class TestHeaders:
    def test_pair_list_wrapped_in_headers(self):
        req = _conn({'headers': [(b'Content-Type', b'text/plain'), (b'x-a', b'1')]})
        assert isinstance(req.headers, Headers)
        assert req.headers.get(b'content-type') == b'text/plain'

    def test_headers_instance_passed_through(self):
        h = Headers([(b'host', b'example.com')])
        req = _conn({'headers': h})
        assert req.headers is h

    def test_multi_value_lookup(self):
        req = _conn({'headers': [(b'set-thing', b'a'), (b'set-thing', b'b')]})
        assert [v for _, v in req.headers.getlist(b'set-thing')] == [b'a', b'b']


# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------

class TestCookies:
    def test_cookie_header_parsed(self):
        req = _conn({'headers': [(b'cookie', b'a=1; b=2')]})
        assert req.cookies == {'a': '1', 'b': '2'}

    def test_no_cookie_header_empty_dict(self):
        req = _conn({'headers': []})
        assert req.cookies == {}

    def test_cookies_cached(self):
        req = _conn({'headers': [(b'cookie', b'a=1')]})
        assert req.cookies is req.cookies


# ---------------------------------------------------------------------------
# body() — single-drain cache; json()/text() build on it
# ---------------------------------------------------------------------------

class TestBody:
    @pytest.mark.asyncio
    async def test_body_returns_full_payload(self):
        receive, _ = single_body_receive(b'hello world')
        req = _conn({'headers': []}, receive)
        assert await req.body() == b'hello world'

    @pytest.mark.asyncio
    async def test_body_drains_receive_exactly_once(self):
        receive, calls = single_body_receive(b'payload')
        req = _conn({'headers': []}, receive)
        assert await req.body() == b'payload'
        assert await req.body() == b'payload'
        assert await req.text() == 'payload'
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_empty_body_is_cached_too(self):
        receive, calls = single_body_receive(b'')
        req = _conn({'headers': []}, receive)
        assert await req.body() == b''
        assert await req.body() == b''
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_multi_chunk_body_joined(self):
        receive, _ = make_receive([
            {'type': 'http.request', 'body': b'chunk1', 'more_body': True},
            {'type': 'http.request', 'body': b'chunk2', 'more_body': False},
        ])
        req = _conn({'headers': []}, receive)
        assert await req.body() == b'chunk1chunk2'

    @pytest.mark.asyncio
    async def test_mid_body_disconnect_raises(self):
        receive, _ = make_receive([
            {'type': 'http.request', 'body': b'partial', 'more_body': True},
            {'type': 'http.disconnect'},
        ])
        req = _conn({'headers': []}, receive)
        with pytest.raises(ClientDisconnected):
            await req.body()


class TestStream:
    @pytest.mark.asyncio
    async def test_stream_yields_chunks_without_buffering(self):
        receive, _ = make_receive([
            {'type': 'http.request', 'body': b'chunk1', 'more_body': True},
            {'type': 'http.request', 'body': b'chunk2', 'more_body': False},
        ])
        req = _conn({'headers': []}, receive)
        chunks = [c async for c in req.stream()]
        assert chunks == [b'chunk1', b'chunk2']

    @pytest.mark.asyncio
    async def test_stream_counts_full_body(self):
        receive, _ = make_receive([
            {'type': 'http.request', 'body': b'a' * 100, 'more_body': True},
            {'type': 'http.request', 'body': b'b' * 40, 'more_body': False},
        ])
        req = _conn({'headers': []}, receive)
        total = 0
        async for chunk in req.stream():
            total += len(chunk)
        assert total == 140

    @pytest.mark.asyncio
    async def test_stream_after_body_raises(self):
        """stream() and body() both drain once; buffering first then streaming
        must fail loudly rather than yield an empty/partial stream."""
        receive, _ = single_body_receive(b'payload')
        req = _conn({'headers': []}, receive)
        assert await req.body() == b'payload'
        with pytest.raises(RuntimeError):
            async for _ in req.stream():
                pass

    @pytest.mark.asyncio
    async def test_body_after_stream_raises(self):
        """The reverse: once stream() has drained the channel, body() must not
        silently return b'' from the exhausted receive."""
        receive, _ = single_body_receive(b'payload')
        req = _conn({'headers': []}, receive)
        async for _ in req.stream():
            pass
        with pytest.raises(RuntimeError):
            await req.body()

    @pytest.mark.asyncio
    async def test_stream_mid_body_disconnect_raises(self):
        receive, _ = make_receive([
            {'type': 'http.request', 'body': b'partial', 'more_body': True},
            {'type': 'http.disconnect'},
        ])
        req = _conn({'headers': []}, receive)
        with pytest.raises(ClientDisconnected):
            async for _ in req.stream():
                pass


class TestJson:
    @pytest.mark.asyncio
    async def test_valid_json_parsed(self):
        receive, _ = single_body_receive(b'{"k": [1, 2]}')
        req = _conn({'headers': []}, receive)
        assert await req.json() == {'k': [1, 2]}

    @pytest.mark.asyncio
    async def test_invalid_json_returns_none(self):
        receive, _ = single_body_receive(b'{not json')
        req = _conn({'headers': []}, receive)
        assert await req.json() is None

    @pytest.mark.asyncio
    async def test_empty_body_returns_none(self):
        receive, _ = single_body_receive(b'')
        req = _conn({'headers': []}, receive)
        assert await req.json() is None

    @pytest.mark.asyncio
    async def test_json_shares_body_cache(self):
        receive, calls = single_body_receive(b'[1]')
        req = _conn({'headers': []}, receive)
        assert await req.body() == b'[1]'
        assert await req.json() == [1]
        assert len(calls) == 1


class TestText:
    @pytest.mark.asyncio
    async def test_utf8_decoded(self):
        receive, _ = single_body_receive('héllo'.encode())
        req = _conn({'headers': []}, receive)
        assert await req.text() == 'héllo'

    @pytest.mark.asyncio
    async def test_undecodable_bytes_replaced(self):
        receive, _ = single_body_receive(b'\xff\xfe caf\xe9')
        req = _conn({'headers': []}, receive)
        assert '�' in await req.text()

    @pytest.mark.asyncio
    async def test_explicit_encoding(self):
        receive, _ = single_body_receive('caf\xe9'.encode('latin-1'))
        req = _conn({'headers': []}, receive)
        assert await req.text(encoding='latin-1') == 'café'

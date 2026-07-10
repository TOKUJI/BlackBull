"""Unit tests for blackbull.request.Request — the opt-in HTTP handler context object.

Pure-unit layer: hand-built scope dicts and fake receive callables, no I/O.
The Request object wraps (scope, receive) and turns the module's free
functions (read_body / read_json / read_text / parse_cookies) into cached
methods; these tests pin the property surface, the dual header shapes
(plain ASGI pair-list vs blackbull.headers.Headers), and the single-drain
body cache.
"""
import pytest

from blackbull.headers import Headers
from blackbull.request import ClientDisconnected, Request


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
        scope = {
            'type': 'http', 'method': 'POST', 'path': '/items/7',
            'scheme': 'https', 'client': ('127.0.0.1', 54321), 'headers': [],
        }
        req = Request(scope, None)
        assert req.method == 'POST'
        assert req.path == '/items/7'
        assert req.scheme == 'https'
        assert req.client == ('127.0.0.1', 54321)

    def test_client_absent_is_none(self):
        req = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': []}, None)
        assert req.client is None

    def test_scope_escape_hatch_is_raw_dict(self):
        scope = {'type': 'http', 'method': 'GET', 'path': '/', 'headers': []}
        req = Request(scope, None)
        assert req.scope is scope


# ---------------------------------------------------------------------------
# Headers — both scope shapes (ASGI pair-list and Headers instance)
# ---------------------------------------------------------------------------

class TestHeaders:
    def test_pair_list_wrapped_in_headers(self):
        scope = {'headers': [(b'Content-Type', b'text/plain'), (b'x-a', b'1')]}
        req = Request(scope, None)
        assert isinstance(req.headers, Headers)
        assert req.headers.get(b'content-type') == b'text/plain'

    def test_headers_instance_passed_through(self):
        h = Headers([(b'host', b'example.com')])
        req = Request({'headers': h}, None)
        assert req.headers is h

    def test_headers_view_is_cached(self):
        scope = {'headers': [(b'host', b'example.com')]}
        req = Request(scope, None)
        assert req.headers is req.headers

    def test_multi_value_lookup(self):
        scope = {'headers': [(b'set-thing', b'a'), (b'set-thing', b'b')]}
        req = Request(scope, None)
        assert [v for _, v in req.headers.getlist(b'set-thing')] == [b'a', b'b']


# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------

class TestCookies:
    def test_cookie_header_parsed(self):
        scope = {'headers': [(b'cookie', b'a=1; b=2')]}
        req = Request(scope, None)
        assert req.cookies == {'a': '1', 'b': '2'}

    def test_no_cookie_header_empty_dict(self):
        req = Request({'headers': []}, None)
        assert req.cookies == {}

    def test_cookies_cached(self):
        scope = {'headers': [(b'cookie', b'a=1')]}
        req = Request(scope, None)
        assert req.cookies is req.cookies


# ---------------------------------------------------------------------------
# body() — single-drain cache; json()/text() build on it
# ---------------------------------------------------------------------------

class TestBody:
    @pytest.mark.asyncio
    async def test_body_returns_full_payload(self):
        receive, _ = single_body_receive(b'hello world')
        req = Request({'headers': []}, receive)
        assert await req.body() == b'hello world'

    @pytest.mark.asyncio
    async def test_body_drains_receive_exactly_once(self):
        receive, calls = single_body_receive(b'payload')
        req = Request({'headers': []}, receive)
        assert await req.body() == b'payload'
        assert await req.body() == b'payload'
        assert await req.text() == 'payload'
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_empty_body_is_cached_too(self):
        receive, calls = single_body_receive(b'')
        req = Request({'headers': []}, receive)
        assert await req.body() == b''
        assert await req.body() == b''
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_multi_chunk_body_joined(self):
        receive, _ = make_receive([
            {'type': 'http.request', 'body': b'chunk1', 'more_body': True},
            {'type': 'http.request', 'body': b'chunk2', 'more_body': False},
        ])
        req = Request({'headers': []}, receive)
        assert await req.body() == b'chunk1chunk2'

    @pytest.mark.asyncio
    async def test_mid_body_disconnect_raises(self):
        receive, _ = make_receive([
            {'type': 'http.request', 'body': b'partial', 'more_body': True},
            {'type': 'http.disconnect'},
        ])
        req = Request({'headers': []}, receive)
        with pytest.raises(ClientDisconnected):
            await req.body()


class TestJson:
    @pytest.mark.asyncio
    async def test_valid_json_parsed(self):
        receive, _ = single_body_receive(b'{"k": [1, 2]}')
        req = Request({'headers': []}, receive)
        assert await req.json() == {'k': [1, 2]}

    @pytest.mark.asyncio
    async def test_invalid_json_returns_none(self):
        receive, _ = single_body_receive(b'{not json')
        req = Request({'headers': []}, receive)
        assert await req.json() is None

    @pytest.mark.asyncio
    async def test_empty_body_returns_none(self):
        receive, _ = single_body_receive(b'')
        req = Request({'headers': []}, receive)
        assert await req.json() is None

    @pytest.mark.asyncio
    async def test_json_shares_body_cache(self):
        receive, calls = single_body_receive(b'[1]')
        req = Request({'headers': []}, receive)
        assert await req.body() == b'[1]'
        assert await req.json() == [1]
        assert len(calls) == 1


class TestText:
    @pytest.mark.asyncio
    async def test_utf8_decoded(self):
        receive, _ = single_body_receive('héllo'.encode())
        req = Request({'headers': []}, receive)
        assert await req.text() == 'héllo'

    @pytest.mark.asyncio
    async def test_undecodable_bytes_replaced(self):
        receive, _ = single_body_receive(b'\xff\xfe caf\xe9')
        req = Request({'headers': []}, receive)
        assert '�' in await req.text()

    @pytest.mark.asyncio
    async def test_explicit_encoding(self):
        receive, _ = single_body_receive('caf\xe9'.encode('latin-1'))
        req = Request({'headers': []}, receive)
        assert await req.text(encoding='latin-1') == 'café'

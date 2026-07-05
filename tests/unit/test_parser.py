"""Tests for blackbull/server/parser.py — HTTP2ParserBase registration guards."""
import pytest
from unittest.mock import MagicMock
from hypothesis import given
from hypothesis import strategies as st

from blackbull.server.parser import HTTP2ParserBase


def test_subclass_frame_type_none_not_registered():
    before = dict(HTTP2ParserBase._registry)

    class AbstractSub(HTTP2ParserBase):
        FRAME_TYPE = None

    assert HTTP2ParserBase._registry == before


def test_subclass_duplicate_frame_type_raises():
    with pytest.raises(ValueError, match='Duplicate FRAME_TYPE'):
        class DupA(HTTP2ParserBase):
            FRAME_TYPE = 'parser_test_sentinel'

        class DupB(HTTP2ParserBase):
            FRAME_TYPE = 'parser_test_sentinel'

    HTTP2ParserBase._registry.pop('parser_test_sentinel', None)


def test_base_parse_not_implemented():
    frame = MagicMock()
    frame.stream_id = 1
    stream = MagicMock()
    parser = HTTP2ParserBase(frame, stream)
    with pytest.raises(NotImplementedError):
        parser.parse()


# ---------------------------------------------------------------------------
# Migrated from test_server_dispatch.py — HTTP/1.1 and HTTP/2 scope parsing
# ---------------------------------------------------------------------------

from blackbull.server.http1_actor import HTTP1Actor as _HTTP1Actor
from blackbull.server.parser import _make_scope as _make_http2_scope, ParserFactory as _ParserFactory


def _get_scope(raw_request: bytes) -> dict:
    """Parse raw HTTP/1.1 request bytes and return the resulting scope dict."""
    actor = object.__new__(_HTTP1Actor)
    return actor._parse(raw_request)


def _http_request(method='GET', path='/', version='HTTP/1.1',
                  headers: dict | None = None) -> bytes:
    if headers is None:
        headers = {'Host': 'localhost:8000'}
    lines = [f'{method} {path} {version}']
    for k, v in headers.items():
        lines.append(f'{k}: {v}')
    lines.append('')
    lines.append('')
    return '\r\n'.join(lines).encode()


def _ws_request_dispatch(path='/ws', host='localhost:8000') -> bytes:
    return _http_request(
        method='GET', path=path,
        headers={
            'Host': host,
            'Upgrade': 'websocket',
            'Connection': 'Upgrade',
            'Sec-WebSocket-Key': 'dGhlIHNhbXBsZSBub25jZQ==',
            'Sec-WebSocket-Version': '13',
        }
    )


class TestParse:
    """Unit tests for HTTP1Actor._parse() — HTTP/1.1 scope building."""

    def test_method_is_extracted(self):
        assert _get_scope(_http_request(method='GET'))['method'] == 'GET'

    def test_post_method(self):
        assert _get_scope(_http_request(method='POST'))['method'] == 'POST'

    def test_path_is_extracted(self):
        assert _get_scope(_http_request(path='/hello'))['path'] == '/hello'

    def test_root_path(self):
        assert _get_scope(_http_request(path='/'))['path'] == '/'

    def test_http_version_is_extracted(self):
        assert _get_scope(_http_request(version='HTTP/1.1'))['http_version'] == '1.1'

    def test_http_version_is_extracted_10(self):
        assert _get_scope(_http_request(version='HTTP/1.0'))['http_version'] == '1.0'

    def test_http2_version_string_is_spec_compliant(self):
        """ASGI spec: http_version must be '2' for HTTP/2, not '2.0'."""
        scope = _make_http2_scope()
        assert scope['http_version'] == '2'

    def test_type_is_http_by_default(self):
        assert _get_scope(_http_request())['type'] == 'http'

    def test_asgi_version_is_set(self):
        assert _get_scope(_http_request()).get('asgi', {}).get('version') == '3.0'

    def test_server_host_and_port_are_parsed(self):
        scope = _get_scope(_http_request(headers={'Host': 'localhost:9000'}))
        assert scope['server'] == ['localhost', 9000]

    def test_ipv6_bracketed_host_and_port_are_parsed(self):
        """RFC 3986 §3.2.2 — ``[::1]:8100`` must not crash Host parsing.

        Regression: ``b'[::1]:8100'.split(b':')`` gave ``int(b'')`` →
        ValueError, which closed the connection with no response ("empty
        reply from server") for every IPv6 request.
        """
        scope = _get_scope(_http_request(headers={'Host': '[::1]:8100'}))
        assert scope['server'] == ['::1', 8100]

    def test_ipv6_bracketed_host_without_port(self):
        scope = _get_scope(_http_request(headers={'Host': '[2001:db8::1]'}))
        assert scope['server'] == ['2001:db8::1', 80]

    def test_websocket_upgrade_sets_type_websocket(self):
        scope = _get_scope(_ws_request_dispatch())
        assert scope['type'] == 'websocket', (
            f"Expected type='websocket', got {scope['type']!r}."
        )

    def test_websocket_upgrade_sets_scheme_ws(self):
        assert _get_scope(_ws_request_dispatch())['scheme'] == 'ws'

    def test_websocket_path_is_preserved(self):
        scope = _get_scope(_ws_request_dispatch(path='/chat'))
        assert scope['path'] == '/chat'

    def test_headers_is_iterable_of_pairs(self):
        from blackbull.headers import Headers
        scope = _get_scope(_http_request())
        assert isinstance(scope['headers'], Headers)
        for name, value in scope['headers']:
            assert isinstance(name, bytes)
            assert isinstance(value, bytes)

    def test_root_path_is_present_in_scope(self):
        assert 'root_path' in _get_scope(_http_request())

    def test_root_path_default_is_empty_string(self):
        assert _get_scope(_http_request()).get('root_path') == ''

    @given(prefix=st.from_regex(r'/[a-zA-Z0-9/_-]{0,40}', fullmatch=True))
    def test_root_path_preserved_from_x_forwarded_prefix(self, prefix):
        scope = _get_scope(_http_request(
            headers={'Host': 'localhost:8000', 'X-Forwarded-Prefix': prefix}
        ))
        assert scope.get('root_path') == prefix

    def test_root_path_not_set_when_prefix_absent(self):
        assert _get_scope(_http_request(headers={'Host': 'localhost:8000'})).get('root_path') == ''

    def test_path_excludes_query_string(self):
        assert _get_scope(_http_request(path='/tasks?done=true'))['path'] == '/tasks'

    def test_query_string_populated_when_present(self):
        scope = _get_scope(_http_request(path='/tasks?done=true&page=2'))
        assert scope['query_string'] == b'done=true&page=2'

    def test_query_string_empty_bytes_when_absent(self):
        assert _get_scope(_http_request(path='/tasks'))['query_string'] == b''

    # ------------------------------------------------------------------
    # Sprint 25 Phase A — _parse URL splitter boundary cases.
    #
    # Replaced urllib.parse.urlparse with bytes.partition chain in
    # http1_actor._parse.  The replacement preserves urlparse semantics
    # for the .path + .query attributes used by the scope dict:
    # strip #fragment, separate ?query, drop ;params from the path.
    # These tests pin the observable behaviour so future refactors
    # cannot silently drift.
    # ------------------------------------------------------------------

    def test_path_strips_fragment(self):
        """RFC 7230 §5.3 — fragment must not appear in request-target;
        urlparse silently strips it.  Pre-Sprint-25 behaviour preserved."""
        scope = _get_scope(_http_request(path='/api#frag'))
        assert scope['path'] == '/api'
        assert scope['query_string'] == b''

    def test_path_strips_semicolon_params(self):
        """urllib's URL grammar separates ;params from the path."""
        scope = _get_scope(_http_request(path='/cart;sid=abc'))
        assert scope['path'] == '/cart'
        assert scope['query_string'] == b''

    def test_path_strips_params_keeps_query(self):
        scope = _get_scope(_http_request(path='/cart;sid=abc?x=1'))
        assert scope['path'] == '/cart'
        assert scope['query_string'] == b'x=1'

    def test_path_strips_fragment_after_query(self):
        scope = _get_scope(_http_request(path='/api?x=1#frag'))
        assert scope['path'] == '/api'
        assert scope['query_string'] == b'x=1'

    def test_path_strips_all_three(self):
        scope = _get_scope(_http_request(path='/cart;sid=abc?x=1#frag'))
        assert scope['path'] == '/cart'
        assert scope['query_string'] == b'x=1'

    def test_multiple_question_marks_kept_in_query(self):
        """Per RFC 3986 the first ? terminates the path; subsequent
        ? characters are part of the query."""
        scope = _get_scope(_http_request(path='/api?x=1?y=2'))
        assert scope['path'] == '/api'
        assert scope['query_string'] == b'x=1?y=2'

    def test_only_query_string(self):
        """Edge case: request-target is just ?query (no leading path)."""
        scope = _get_scope(_http_request(path='?onlyq'))
        assert scope['path'] == ''
        assert scope['query_string'] == b'onlyq'

    def test_trailing_question_mark_yields_empty_query(self):
        scope = _get_scope(_http_request(path='/empty?'))
        assert scope['path'] == '/empty'
        assert scope['query_string'] == b''

    def test_raw_path_unchanged_by_splitter(self):
        """raw_path must be the original bytes — splitter changes
        only `path` + `query_string`."""
        scope = _get_scope(_http_request(path='/cart;sid=abc?x=1'))
        assert scope['raw_path'] == b'/cart;sid=abc?x=1'

    def test_http_version_10(self):
        assert _get_scope(_http_request(version='HTTP/1.0'))['http_version'] == '1.0'

    def test_http_version_11(self):
        assert _get_scope(_http_request(version='HTTP/1.1'))['http_version'] == '1.1'

    # ------------------------------------------------------------------
    # Sprint 25 Phase B — compiled-regex header validators.
    #
    # Replaced per-byte `any(...)` scans in _parse with compiled
    # `_FIELD_NAME_INVALID_RE.search(...)` and
    # `_FIELD_VALUE_INVALID_RE.search(...)`.  The regex character
    # classes must match the exact RFC 9110 §5.6.2 tchar negation
    # and the §5.5 CTL-except-HTAB allow-list respectively.  These
    # tests exercise the character-boundary cases that would diverge
    # if the regex were one byte off.
    # ------------------------------------------------------------------

    def test_tchar_accepted_in_method(self):
        """All non-alphanumeric tchar characters accepted in method."""
        for ch in b"!#$%&'*+-.^_`|~":
            method = b'GE' + bytes([ch])
            req = b'%b /x HTTP/1.1\r\nHost: x:80\r\n\r\n' % method
            scope = _get_scope(req)
            assert scope['method'] == method.decode()

    @pytest.mark.parametrize('bad_byte', [
        0x20,  # space — request-line delimiter, not tchar
        0x22,  # " — separator
        0x28,  # ( — separator
        0x40,  # @ — separator
        0x7B,  # { — separator (Sprint 25 Phase B boundary)
        0x7F,  # DEL — CTL
    ])
    def test_non_tchar_rejected_in_method(self, bad_byte):
        from blackbull.server.http1_actor import BadRequestError
        method = b'GE' + bytes([bad_byte])
        req = b'%b /x HTTP/1.1\r\nHost: x:80\r\n\r\n' % method
        with pytest.raises(BadRequestError):
            _get_scope(req)

    def test_tchar_accepted_in_header_name(self):
        """tchar boundary chars accepted in field-name."""
        req = b'GET / HTTP/1.1\r\nHost: x:80\r\nx-^_|: ok\r\n\r\n'
        scope = _get_scope(req)
        names = [n for n, _ in scope['headers']]
        assert b'x-^_|' in names

    @pytest.mark.parametrize('bad_byte', [0x20, 0x28, 0x40, 0x7B, 0x7F])
    def test_non_tchar_rejected_in_header_name(self, bad_byte):
        from blackbull.server.http1_actor import BadRequestError
        bad_name = b'bad' + bytes([bad_byte])
        req = b'GET /x HTTP/1.1\r\nHost: x:80\r\n' + bad_name + b': v\r\n\r\n'
        with pytest.raises(BadRequestError):
            _get_scope(req)

    def test_htab_allowed_in_field_value(self):
        """RFC 9110 §5.5 — HTAB (0x09) is the one CTL allowed in value."""
        req = b'GET / HTTP/1.1\r\nHost: x:80\r\nx-tab: a\tb\r\n\r\n'
        scope = _get_scope(req)
        kv = [(n, v) for n, v in scope['headers']]
        assert (b'x-tab', b'a\tb') in kv

    @pytest.mark.parametrize('ctl_byte', [
        0x00,  # NUL
        0x01,  # SOH
        0x08,  # BS — last CTL before HTAB
        0x0A,  # LF — first CTL above HTAB
        0x1F,  # US — last C0 control
        0x7F,  # DEL
    ])
    def test_ctl_rejected_in_field_value(self, ctl_byte):
        from blackbull.server.http1_actor import BadRequestError
        bad_value = b'val' + bytes([ctl_byte]) + b'ue'
        req = b'GET / HTTP/1.1\r\nHost: x:80\r\nx-h: ' + bad_value + b'\r\n\r\n'
        with pytest.raises(BadRequestError):
            _get_scope(req)

    @pytest.mark.parametrize('ok_byte', [
        0x20,  # space — allowed mid-value
        0x21,  # ! — first printable
        0x7E,  # ~ — last printable below DEL
    ])
    def test_printable_accepted_in_field_value(self, ok_byte):
        """Visible ASCII 0x20-0x7E is value-legal."""
        good_value = b'v' + bytes([ok_byte]) + b'v'
        req = b'GET / HTTP/1.1\r\nHost: x:80\r\nx-h: ' + good_value + b'\r\n\r\n'
        scope = _get_scope(req)
        kv = [(n, v) for n, v in scope['headers']]
        assert (b'x-h', good_value) in kv


def _make_h2_headers_frame_dispatch(extra_headers: list | None = None) -> object:
    from hpack import Encoder
    from blackbull.protocol.frame import FrameFactory
    from blackbull.protocol.frame_types import FrameTypes, HeaderFrameFlags

    encoder = Encoder()
    header_list = [
        (b':method', b'GET'),
        (b':path',   b'/'),
        (b':scheme', b'https'),
    ]
    if extra_headers:
        header_list.extend(extra_headers)

    block = encoder.encode(header_list)
    flags = HeaderFrameFlags.END_HEADERS | HeaderFrameFlags.END_STREAM
    stream_id = 1
    raw = (len(block).to_bytes(3, 'big')
           + FrameTypes.HEADERS
           + bytes([flags])
           + stream_id.to_bytes(4, 'big')
           + block)
    return FrameFactory().load(raw)


class _FakeStream:
    identifier = 1


class TestHTTP2ScopeFields:
    """HTTP2HEADParser.parse() must populate scope fields correctly."""

    def test_root_path_default_empty_string(self):
        frame = _make_h2_headers_frame_dispatch()
        scope = _ParserFactory.Get(frame, _FakeStream()).parse()
        assert scope.get('root_path') == ''

    @given(prefix=st.from_regex(r'/[a-zA-Z0-9/_-]{0,40}', fullmatch=True))
    def test_root_path_preserved_from_x_forwarded_prefix(self, prefix):
        frame = _make_h2_headers_frame_dispatch(
            extra_headers=[(b'x-forwarded-prefix', prefix.encode())]
        )
        scope = _ParserFactory.Get(frame, _FakeStream()).parse()
        assert scope.get('root_path') == prefix


class TestHTTP11DuplicateHeaders:
    """HTTP1Actor._parse() must preserve every occurrence of a repeated header."""

    def _raw_with_duplicate(self, name: str, values: list[str]) -> bytes:
        lines = ['GET / HTTP/1.1', 'Host: localhost:8000']
        for v in values:
            lines.append(f'{name}: {v}')
        lines += ['', '']
        return '\r\n'.join(lines).encode()

    def test_single_header_preserved(self):
        scope = _get_scope(_http_request(headers={'Host': 'localhost:8000', 'Accept': 'text/html'}))
        keys = [k for k, _ in scope['headers']]
        assert b'accept' in keys

    def test_two_set_cookie_both_in_headers(self):
        raw = self._raw_with_duplicate('Set-Cookie', ['a=1', 'b=2'])
        scope = _get_scope(raw)
        cookie_values = [v for k, v in scope['headers'] if k == b'set-cookie']
        assert len(cookie_values) == 2

    def test_duplicate_accept_both_preserved(self):
        raw = self._raw_with_duplicate('Accept', ['text/html', 'application/json'])
        scope = _get_scope(raw)
        accept_values = [v for k, v in scope['headers'] if k == b'accept']
        assert len(accept_values) == 2

    def test_first_value_not_overwritten(self):
        raw = self._raw_with_duplicate('X-Custom', ['first', 'second'])
        scope = _get_scope(raw)
        values = [v for k, v in scope['headers'] if k == b'x-custom']
        assert b'first' in values

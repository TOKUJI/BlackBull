"""Tests for blackbull/server/parser.py — HTTP/1.1 and HTTP/2 scope parsing."""
import pytest
from hypothesis import given
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# Migrated from test_server_dispatch.py — HTTP/1.1 and HTTP/2 scope parsing
# ---------------------------------------------------------------------------

from blackbull.server.http1_actor import HTTP1Actor as _HTTP1Actor
from blackbull.server.parser import parse_headers as _real_parse_headers


def _parse_headers(frame) -> dict:
    """Parse an HTTP/2 HEADERS frame and return the derived ASGI scope.

    Sprint 79 Phase 4 — ``parse_headers`` now returns a native ``Connection``;
    this suite asserts on the derived ASGI scope shape (the H/2 actor bridge
    materializes the same view), so convert via the single ``as_scope()``
    builder (headers therefore appear in the ASGI ``list[tuple]`` form).

    ``parse_headers`` returns ``None`` on malformed input (Sprint 79 — no
    throwaway Connection on the error path); this wrapper is only fed
    well-formed frames, so a ``None`` here is a test-setup error.
    """
    conn = _real_parse_headers(frame)
    assert conn is not None, 'parse_headers returned None (unexpectedly malformed frame)'
    return conn.as_scope()


def _get_scope(raw_request: bytes) -> dict:
    """Parse raw HTTP/1.1 request bytes and return the derived ASGI scope.

    Sprint 79 Phase 3 — ``_parse`` now returns a native ``Connection``; this
    suite asserts on the derived ASGI scope shape, so materialize it via the
    single ``as_scope()`` builder (headers therefore appear in the ASGI
    ``list[tuple]`` form, not as a ``Headers`` object).
    """
    actor = object.__new__(_HTTP1Actor)
    return actor._parse(raw_request).as_scope()


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
        conn = _real_parse_headers(_make_h2_headers_frame_dispatch())
        assert conn is not None
        assert conn.http_version == '2'

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
        # as_scope() emits headers in the ASGI list[tuple] form.
        scope = _get_scope(_http_request())
        assert isinstance(scope['headers'], list)
        for name, value in scope['headers']:
            assert isinstance(name, bytes)
            assert isinstance(value, bytes)

    def test_root_path_is_present_in_scope(self):
        assert 'root_path' in _get_scope(_http_request())

    def test_root_path_default_is_empty_string(self):
        assert _get_scope(_http_request()).get('root_path') == ''

    @given(prefix=st.from_regex(r'/[a-zA-Z0-9/_-]{0,40}', fullmatch=True))
    def test_root_path_ignores_x_forwarded_prefix_off_the_wire(self, prefix):
        # Spec change (audit bug 1.16, Sprint 63): a client-controlled
        # X-Forwarded-Prefix must NOT set root_path at the parser layer —
        # it is only honoured behind TrustedProxy (see
        # tests/unit/test_audit_sprint63.py::TestXForwardedPrefixTrust).
        scope = _get_scope(_http_request(
            headers={'Host': 'localhost:8000', 'X-Forwarded-Prefix': prefix}
        ))
        assert scope.get('root_path') == ''

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
    # Sprint 25 Phase A / Sprint 68 — _parse URL splitter boundary cases.
    #
    # http1_actor._parse splits the byte request-target with a partition
    # chain: strip #fragment, separate ?query.  Sprint 68 stopped dropping
    # ;params — RFC 3986 treats ';' as an ordinary path sub-delimiter (the
    # ;params grammar is obsolete RFC 2396), so both `path` and `raw_path`
    # now preserve it (uvicorn parity; the two fields must be the same
    # component at different decode stages).  These tests pin the observable
    # behaviour so future refactors cannot silently drift.
    # ------------------------------------------------------------------

    def test_path_strips_fragment(self):
        """RFC 7230 §5.3 — fragment must not appear in request-target;
        it is silently stripped.  Pre-Sprint-25 behaviour preserved."""
        scope = _get_scope(_http_request(path='/api#frag'))
        assert scope['path'] == '/api'
        assert scope['query_string'] == b''

    def test_path_keeps_semicolon_params(self):
        """Sprint 68 — ';' is an RFC 3986 path sub-delimiter, not a params
        separator; it stays in the path (and in raw_path)."""
        scope = _get_scope(_http_request(path='/cart;sid=abc'))
        assert scope['path'] == '/cart;sid=abc'
        assert scope['raw_path'] == b'/cart;sid=abc'
        assert scope['query_string'] == b''

    def test_path_keeps_params_keeps_query(self):
        scope = _get_scope(_http_request(path='/cart;sid=abc?x=1'))
        assert scope['path'] == '/cart;sid=abc'
        assert scope['query_string'] == b'x=1'

    def test_path_strips_fragment_after_query(self):
        scope = _get_scope(_http_request(path='/api?x=1#frag'))
        assert scope['path'] == '/api'
        assert scope['query_string'] == b'x=1'

    def test_path_keeps_params_strips_fragment_keeps_query(self):
        scope = _get_scope(_http_request(path='/cart;sid=abc?x=1#frag'))
        assert scope['path'] == '/cart;sid=abc'
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

    def test_raw_path_is_undecoded_path_component(self):
        """ASGI: raw_path is the path component only — undecoded bytes,
        query string excluded, ;params kept (spec change, Sprint 68 W1;
        previously the full request target including the query)."""
        scope = _get_scope(_http_request(path='/cart;sid=abc?x=1'))
        assert scope['raw_path'] == b'/cart;sid=abc'

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


def _make_h2_headers_frame_dispatch(extra_headers: list | None = None,
                                    path: str = '/') -> object:
    from hpack import Encoder
    from blackbull.protocol.frame import FrameFactory
    from blackbull.protocol.frame_types import FrameTypes, HeaderFrameFlags

    encoder = Encoder()
    header_list = [
        (b':method', b'GET'),
        (b':path',   path.encode('utf-8')),
        (b':scheme', b'https'),
        (b':authority', b'example.com'),
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


class TestHTTP2ScopeFields:
    """parse_headers() must populate scope fields correctly."""

    def test_root_path_default_empty_string(self):
        frame = _make_h2_headers_frame_dispatch()
        scope = _parse_headers(frame)
        assert scope.get('root_path') == ''

    @given(prefix=st.from_regex(r'/[a-zA-Z0-9/_-]{0,40}', fullmatch=True))
    def test_x_forwarded_prefix_ignored_off_the_wire(self, prefix):
        # Bug 1.16 (Sprint 63): same contract on the H2 path — the frame-level
        # parser must not trust a client-supplied X-Forwarded-Prefix.  Only
        # the TrustedProxy middleware may set root_path, after verifying the
        # peer (see tests/unit/test_audit_sprint63.py::TestXForwardedPrefixTrust).
        frame = _make_h2_headers_frame_dispatch(
            extra_headers=[(b'x-forwarded-prefix', prefix.encode())]
        )
        scope = _parse_headers(frame)
        assert scope.get('root_path') == ''


class TestParseHeadersNoneContract:
    """Sprint 80 alloc hygiene (`connection-alloc-hygiene.md` Phase 1,
    2026-07-24): ``parse_headers`` returns ``None`` on every path that marks
    the frame malformed, uniformly — including host-authority validation
    failure, which the pre-refactor version answered with a half-built
    ``Connection`` instead (never observed by the real actor, which checks
    ``frame.malformed`` before reading the result, but a real difference for
    any direct caller). Pins ``result is None ⟺ frame.malformed``."""

    def test_missing_authority_and_host_returns_none(self):
        from hpack import Encoder
        from blackbull.protocol.frame import FrameFactory
        from blackbull.protocol.frame_types import FrameTypes, HeaderFrameFlags

        block = Encoder().encode([
            (b':method', b'GET'), (b':path', b'/'), (b':scheme', b'https'),
        ])  # no :authority, no Host — https requires one of the two
        flags = HeaderFrameFlags.END_HEADERS | HeaderFrameFlags.END_STREAM
        raw = (len(block).to_bytes(3, 'big') + FrameTypes.HEADERS
               + bytes([flags]) + (1).to_bytes(4, 'big') + block)
        frame = FrameFactory().load(raw)
        assert _real_parse_headers(frame) is None
        assert frame.malformed

    def test_empty_authority_returns_none(self):
        from hpack import Encoder
        from blackbull.protocol.frame import FrameFactory
        from blackbull.protocol.frame_types import FrameTypes, HeaderFrameFlags

        block = Encoder().encode([
            (b':method', b'GET'), (b':path', b'/'), (b':scheme', b'https'),
            (b':authority', b''),
        ])
        flags = HeaderFrameFlags.END_HEADERS | HeaderFrameFlags.END_STREAM
        raw = (len(block).to_bytes(3, 'big') + FrameTypes.HEADERS
               + bytes([flags]) + (1).to_bytes(4, 'big') + block)
        frame = FrameFactory().load(raw)
        assert _real_parse_headers(frame) is None
        assert frame.malformed

    def test_well_formed_frame_returns_connection_not_none(self):
        conn = _real_parse_headers(_make_h2_headers_frame_dispatch())
        assert conn is not None


class TestPathPercentDecodingH1:
    """Sprint 68 W1 — ASGI conformance: ``scope['path']`` carries the
    percent-decoded (UTF-8) path component; ``scope['raw_path']`` carries
    the undecoded path-component bytes (query excluded); the query string
    stays raw on the wire encoding.

    Decode semantics deliberately match uvicorn (``urllib.parse.unquote``
    with ``errors='replace'``): ``+`` is not special in paths, malformed
    escapes pass through literally, a truncated multi-byte sequence
    becomes U+FFFD rather than a 4xx/5xx.
    """

    def test_percent_encoded_unreserved_decodes(self):
        # RFC 3986 §2.3 — %41 and A are the same URI.
        assert _get_scope(_http_request(path='/a/%41/b'))['path'] == '/a/A/b'

    def test_multibyte_utf8_decodes(self):
        assert _get_scope(_http_request(path='/caf%C3%A9'))['path'] == '/café'

    def test_encoded_slash_becomes_real_slash(self):
        # Documented consequence (uvicorn/Starlette parity): %2F merges
        # into path segmentation; raw_path keeps the distinction.
        scope = _get_scope(_http_request(path='/p/a%2Fb'))
        assert scope['path'] == '/p/a/b'
        assert scope['raw_path'] == b'/p/a%2Fb'

    def test_malformed_escape_passes_through_literally(self):
        assert _get_scope(_http_request(path='/x/%ZZ'))['path'] == '/x/%ZZ'

    def test_truncated_multibyte_becomes_replacement_char(self):
        assert _get_scope(_http_request(path='/x/%C3'))['path'] == '/x/�'

    def test_plus_is_not_decoded_in_path(self):
        assert _get_scope(_http_request(path='/a+b'))['path'] == '/a+b'

    def test_no_percent_path_unchanged(self):
        # Fast path: no '%' anywhere → byte-identical decode.
        assert _get_scope(_http_request(path='/plain/path'))['path'] == '/plain/path'

    def test_query_string_stays_raw(self):
        scope = _get_scope(_http_request(path='/a%41?x=%41'))
        assert scope['path'] == '/aA'
        assert scope['query_string'] == b'x=%41'

    def test_raw_path_keeps_percent_encoding(self):
        assert _get_scope(_http_request(path='/a%41b'))['raw_path'] == b'/a%41b'

    def test_raw_path_excludes_query_string(self):
        assert _get_scope(_http_request(path='/a%41b?x=1'))['raw_path'] == b'/a%41b'

    def test_websocket_upgrade_path_decodes(self):
        scope = _get_scope(_ws_request_dispatch(path='/chat/%41'))
        assert scope['type'] == 'websocket'
        assert scope['path'] == '/chat/A'


class TestPathPercentDecodingH2:
    """Same W1 contract on the HTTP/2 ``:path`` pseudo-header path."""

    def _scope(self, path: str) -> dict:
        return _parse_headers(_make_h2_headers_frame_dispatch(path=path))

    def test_percent_encoded_unreserved_decodes(self):
        assert self._scope('/a/%41/b')['path'] == '/a/A/b'

    def test_multibyte_utf8_decodes(self):
        assert self._scope('/caf%C3%A9')['path'] == '/café'

    def test_encoded_slash_becomes_real_slash(self):
        scope = self._scope('/p/a%2Fb')
        assert scope['path'] == '/p/a/b'
        assert scope['raw_path'] == b'/p/a%2Fb'

    def test_malformed_escape_passes_through_literally(self):
        assert self._scope('/x/%ZZ')['path'] == '/x/%ZZ'

    def test_no_percent_path_unchanged(self):
        assert self._scope('/plain/path')['path'] == '/plain/path'

    def test_query_string_stays_raw(self):
        scope = self._scope('/a%41?x=%41')
        assert scope['path'] == '/aA'
        assert scope['query_string'] == b'x=%41'

    def test_raw_path_keeps_percent_encoding(self):
        assert self._scope('/a%41b?x=1')['raw_path'] == b'/a%41b'

    def test_semicolon_params_preserved(self):
        # Sprint 68 — urlsplit (not urlparse) keeps ';params' in the path,
        # so both path and raw_path carry the sub-delimiter (the latent H2
        # raw_path-stripping bug is fixed).
        scope = self._scope('/cart;sid=abc?x=1')
        assert scope['path'] == '/cart;sid=abc'
        assert scope['raw_path'] == b'/cart;sid=abc'
        assert scope['query_string'] == b'x=1'


class TestH1H2PathParity:
    """The same encoded request target must produce identical
    ``path`` / ``raw_path`` / ``query_string`` on both transports."""

    @pytest.mark.parametrize('target', [
        '/a%41/caf%C3%A9/p%2Fq?q=%41&y=2',
        '/plain?x=1',
        '/x/%ZZ',
        '/cart;sid=abc/items;v=2?x=1',
    ])
    def test_scope_fields_identical(self, target):
        h1 = _get_scope(_http_request(path=target))
        h2 = _parse_headers(_make_h2_headers_frame_dispatch(path=target))
        assert h1['path'] == h2['path']
        assert h1['raw_path'] == h2['raw_path']
        assert h1['query_string'] == h2['query_string']


class TestEncodedSlashRouting:
    """Pin the routing consequence of %2F decoding: the decoded slash
    participates in segmentation, so a param route shaped for one segment
    does not match — applications needing the distinction read raw_path."""

    def test_decoded_slash_404s_on_segment_shaped_route(self):
        from http import HTTPMethod

        from blackbull.router import PathNotRegistered, Router
        from blackbull.utils import Scheme

        router = Router()

        @router.route(path='/p/{x}/q', methods=[HTTPMethod.GET])
        async def handler(scope, receive, send):
            pass

        # '/p/a%2Fb/q' decodes to '/p/a/b/q' — 4 segments against the
        # 3-segment route shape → PathNotRegistered (dispatch → 404).
        with pytest.raises(PathNotRegistered):
            router[('/p/a/b/q', HTTPMethod.GET, Scheme.http)]


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

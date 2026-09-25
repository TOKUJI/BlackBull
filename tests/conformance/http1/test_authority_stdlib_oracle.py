"""The request-target authority graded against the standard library.

RFC 3986 §3.2.2 puts an ``IPv6address`` inside the brackets of an authority's
IP-literal, and ``urllib.parse.urlsplit`` implements that production — the
bracket scan, the ``ipaddress`` parse, and the "nothing after the bracket but
a port" rule.  It parses and caches a whole URL, so it is an oracle here and
never the call site (the same reasoning as the scheme oracle in
``tests/unit/test_audit_sprint63.py``).

Our verdict is allowed to be *stricter* than the oracle in exactly five
ways, each stated in ``_we_are_stricter`` and asserted below rather than
filtered away.  Every other disagreement is a defect on one side or the other.

The enumerated sweep is the regression: it is what a bracketed string with no
IPv6 grammar behind it (`[::1`, `[]`, `[zz]`, `[::1]x`, `[1.2.3.4]`) fails.
The Hypothesis property runs the same comparison over generated authorities,
so the grammar is compared as a grammar rather than at the reported samples.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from blackbull.server.http1_actor import (
    _HOST_FORBIDDEN_BYTES, BadRequestError, HTTP1Actor, _parse_host_header,
)

_ACTOR = HTTP1Actor.__new__(HTTP1Actor)
_ACTOR._ssl = False

# RFC 3986 §3.2.2 — an IPvFuture literal (§3.2.2's other IP-literal form).
# urlsplit reads it as a ``v``-prefixed special case; we do not read it at all.
_IPV_FUTURE = re.compile(rb'\[v[0-9A-Fa-f]+\.')

# What a bracketed authority can be built from, including the shapes the
# grammar has to refuse.
_INNER = [b'', b'::1', b'zz', b'1.2.3.4', b'::ffff:1.2.3.4',
          b'fe80::1%25eth0', b'0:0:0:0:0:0:0:1', b'::1%', b'v1.foo',
          b'V1.foo', b'v', b'::1 ', b'::1\t']
_SUFFIX = [b'', b':8100', b':', b':abc', b':99999', b'x', b']', b'::80']


def _accepts(authority: bytes) -> bool:
    """What the H/1 parser does with *authority* in absolute-form."""
    request = (b'GET http://' + authority + b'/x HTTP/1.1\r\n'
               b'Host: localhost\r\n\r\n')
    try:
        _ACTOR._parse(request)
    except BadRequestError:
        return False
    return True


def _accepts_as_host(authority: bytes) -> bool:
    """What the H/1 parser does with *authority* in a ``Host`` field."""
    request = b'GET /x HTTP/1.1\r\nHost: ' + authority + b'\r\n\r\n'
    try:
        _ACTOR._parse(request)
    except BadRequestError:
        return False
    return True


def _stdlib_accepts(authority: bytes) -> bool:
    try:
        urlsplit('http://' + authority.decode('ascii') + '/x')
    except (UnicodeDecodeError, ValueError):
        return False
    return True


def _we_are_stricter(authority: bytes) -> bool:
    """The five deliberate one-way differences from ``urlsplit``.

    An empty authority, an ``IPvFuture`` literal, a forbidden octet, and a
    bracket that is not the one pair of an IP-literal — urlsplit accepts the
    last of those whenever the brackets happen to balance, as in
    ``[::1]:[80]``.  The fifth is a reg-name that is not ``host [":" port]``
    (no host, or a port that is not ``*DIGIT``): urlsplit still reads a
    hostname out of it, where RFC 9112 §7.2 wants a 400.
    """
    if not authority or _IPV_FUTURE.match(authority):
        return True
    if any(byte in _HOST_FORBIDDEN_BYTES for byte in authority):
        return True
    if authority.count(b'[') > 1 or authority.count(b']') > 1:
        return True
    if not authority.startswith(b'['):
        host, sep, port = authority.partition(b':')
        if not host or (sep and port and not port.isdigit()):
            return True
    return False


def _expected(authority: bytes) -> bool:
    return _stdlib_accepts(authority) and not _we_are_stricter(authority)


def _authorities():
    for inner in _INNER:
        for suffix in _SUFFIX:
            yield b'[' + inner + b']' + suffix


class TestTheAuthorityGrammarMatchesTheStdlibUrlParser:
    @pytest.mark.parametrize('suffix', [b'#frag', b'?q=ok', b'/x'])
    def test_uri_delimiters_end_authority_but_are_invalid_in_host(self, suffix):
        candidate = b'[::1]' + suffix
        conn = _ACTOR._parse(
            b'GET http://' + candidate + b' HTTP/1.1\r\n'
            b'Host: conflicting.invalid\r\n\r\n')
        assert conn.headers.get(b'host') == b'[::1]'
        assert conn.server == ('::1', 80)
        assert conn.path == ('/x' if suffix == b'/x' else '/')
        assert conn.query_string == (b'q=ok' if suffix == b'?q=ok' else b'')
        assert not _accepts_as_host(candidate)

    @pytest.mark.parametrize('authority', list(_authorities()), ids=repr)
    def test_every_bracketed_candidate(self, authority):
        assert _accepts(authority) is _expected(authority)

    @pytest.mark.parametrize('authority', [
        b'', b'[v1.foo]', b'[v1.foo]:8100', b'[::1\t]', b'[::1] x',
        b'user@[::1]', b'[::1]#frag', b'[[]', b'[::1]:80]', b'[::1]:[80]',
        # A reg-name that is not ``host [":" port]`` (§3.2.2): no host, or a
        # port tail that is not ``*DIGIT``.
        b'good.com:evil', b'a:b:80', b'2001:db8::1', b'::1', b':', b':80',
    ])
    def test_the_deliberate_differences_are_real(self, authority):
        """Stricter means rejected where the oracle accepts — proved, not
        assumed, so the exclusion cannot outlive the difference it names."""
        assert _we_are_stricter(authority)
        if _stdlib_accepts(authority):
            assert _accepts_as_host(authority) is False

    @pytest.mark.parametrize('authority', [
        b'[::1]', b'[::1]:8100', b'[fe80::1%25eth0]', b'[::ffff:1.2.3.4]',
        b'[0:0:0:0:0:0:0:1]', b'[::1]x',
    ])
    def test_a_candidate_the_oracle_grades_is_not_stricter(self, authority):
        assert not _we_are_stricter(authority)

    @settings(max_examples=400, deadline=None)
    @given(authority=st.text(
        alphabet=''.join(chr(b) for b in range(0x20, 0x7F)), max_size=14,
    ).map(lambda text: b'[' + text.encode('ascii') + b']'))
    def test_generated_authorities(self, authority):
        # A Host field tests the complete candidate; URI delimiters in an
        # absolute target would end its authority before validation.
        assert _accepts_as_host(authority) is _expected(authority)

    @settings(max_examples=300, deadline=None)
    @given(authority=st.text(
        alphabet=''.join(chr(b) for b in range(0x21, 0x7F)
                         if b not in _HOST_FORBIDDEN_BYTES), max_size=14,
    ).map(lambda text: b'[' + text.encode('ascii') + b']'))
    def test_both_h1_authority_paths_agree(self, authority):
        """The absolute-form authority and the ``Host`` field are the same
        grammar, so they cannot disagree on a bracketed form."""
        assert _accepts(authority) is _accepts_as_host(authority)


class TestThePortMatchesTheStdlibUrlParser:
    """RFC 3986 §3.2.2 — ``port = *DIGIT``.  The parser reads it, the stdlib
    exposes it through ``SplitResult.port``, which additionally range-checks
    and refuses a non-numeric value: those two are the deliberate differences,
    recorded here rather than adopted."""

    @pytest.mark.parametrize('value,port', [
        (b'example.com:8080', 8080),
        (b'[::1]:8100', 8100),
        (b'example.com:0', 0),
        (b'example.com:65535', 65535),
        (b'example.com:007', 7),
    ])
    def test_a_numeric_port_agrees(self, value, port):
        assert _parse_host_header(value, 80)[1] == port
        assert urlsplit('http://' + value.decode('ascii') + '/x').port == port

    @pytest.mark.parametrize('value', [b'example.com:', b'[::1]:',
                                       b'example.com', b'[::1]'])
    def test_a_missing_port_falls_back_where_the_stdlib_reports_none(self, value):
        assert _parse_host_header(value, 80)[1] == 80
        assert urlsplit('http://' + value.decode('ascii') + '/x').port is None

    def test_an_out_of_range_port_is_accepted(self):
        # §3.2.2 puts no ceiling on the digits, and the peer a request names is
        # decided before this is read; only the dialler may refuse it.
        assert _parse_host_header(b'example.com:99999', 80)[1] == 99999
        with pytest.raises(ValueError):
            urlsplit('http://example.com:99999/x').port

    def test_a_non_numeric_port_falls_back_to_the_default(self):
        # The host side of this shape is pinned in the class below.
        assert _parse_host_header(b'example.com:abc', 80)[1] == 80
        with pytest.raises(ValueError):
            urlsplit('http://example.com:abc/x').port


class TestTheHostMatchesTheStdlibUrlParser:
    """RFC 3986 §3.2.2 — a reg-name contains no ``:``, so the first colon is
    the port delimiter and the host is what precedes it.  A non-numeric port
    is refused before this parser runs (``TestAnInvalidRegNameIsRejected``);
    the cut below is what the valid empty-port form still needs."""

    @pytest.mark.parametrize('value', [
        b'example.com:abc', b'example.com:', b'a:b:c',
    ])
    def test_port_text_never_survives_in_the_host(self, value):
        host, port = _parse_host_header(value, 80)
        assert ':' not in host
        assert host == urlsplit('http://' + value.decode('ascii') + '/x').hostname
        assert port == 80

    @pytest.mark.parametrize('value,host,port', [
        (b'abc:80', 'abc', 80),
        (b'example.com:007', 'example.com', 7),
        (b'example.com:080', 'example.com', 80),
        (b'example.com:99999', 'example.com', 99999),
        (b'example.com', 'example.com', 80),
        (b'[::1]:abc', '::1', 80),
        (b'[::1]', '::1', 80),
        (b'[::1]:', '::1', 80),
        (b'[::1]:8100', '::1', 8100),
        (b'[2001:db8::1]', '2001:db8::1', 80),
    ])
    def test_the_other_shapes_are_unchanged(self, value, host, port):
        assert _parse_host_header(value, 80) == (host, port)

    @pytest.mark.parametrize('value,server', [
        (b'example.com:', ('example.com', 80)),   # the empty port still cuts
        (b'[::1]:8100', ('::1', 8100)),   # control: already right on base
    ])
    def test_conn_server_carries_the_parsed_host(self, value, server):
        conn = _ACTOR._parse(
            b'GET /x HTTP/1.1\r\nHost: ' + value + b'\r\n\r\n')
        assert conn.server == server


class TestAnInvalidRegNameIsRejected:
    """RFC 9112 §7.2 — an authority that is not ``host [":" port]`` is an
    invalid Host field-value and gets a 400, on both H1 paths.  Normalising
    it instead would hand the app a ``conn.server`` indistinguishable from a
    valid authority."""

    _INVALID = [
        b'good.com:evil', b'example.com:abc', b'a:b:c', b'a:b:80',
        b'example.com:80:90', b'a:b:c:d', b'2001:db8::1', b'::1',
        b':', b':80', b'example.com:80x',
    ]

    @pytest.mark.parametrize('authority', _INVALID, ids=repr)
    def test_the_host_field_rejects_it(self, authority):
        with pytest.raises(BadRequestError):
            _ACTOR._parse(
                b'GET /x HTTP/1.1\r\nHost: ' + authority + b'\r\n\r\n')

    @pytest.mark.parametrize('authority', _INVALID, ids=repr)
    def test_the_absolute_form_authority_rejects_it(self, authority):
        with pytest.raises(BadRequestError):
            _ACTOR._parse(
                b'GET http://' + authority + b'/x HTTP/1.1\r\n'
                b'Host: localhost\r\n\r\n')

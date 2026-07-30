"""Octet classifiers in `_parse` must be byte-for-byte what they replaced.

The request-target and Host authority scans were per-byte Python generator
expressions; they are now C-level bulk operations (`bytes.translate` for the
target's large allowed set, a precompiled regex for Host's small forbidden
set).  Both rewrites are only safe if they classify all 256 octets exactly as
the predicates did, so that is what these tests assert — against a literal
copy of the original predicate, not against a restatement of the new one.
"""
import pytest

from blackbull.server.http1_actor import (
    _HOST_FORBIDDEN_BYTES,
    _HOST_FORBIDDEN_RE,
    _TARGET_ALLOWED_OCTETS,
    BadRequestError,
    HTTP1Actor,
)


def _target_forbidden_reference(b: int) -> bool:
    """The predicate as it stood before the table: RFC 9112 §2.1 / RFC 3986."""
    return b < 0x21 or b == 0x7F or b >= 0x80


def _target_rejects(data: bytes) -> bool:
    return bool(data.translate(None, _TARGET_ALLOWED_OCTETS))


@pytest.fixture
def actor():
    a = HTTP1Actor.__new__(HTTP1Actor)
    a._ssl = False
    return a


def test_target_table_classifies_all_256_octets_identically():
    mismatched = [b for b in range(256)
                  if _target_rejects(bytes([b])) is not _target_forbidden_reference(b)]
    assert mismatched == []


def test_target_table_finds_a_bad_octet_at_any_position():
    # `translate` scans the whole string, but pin it: a forbidden byte must be
    # caught mid-target, not only when it leads.
    for bad in (0x00, 0x1F, 0x20, 0x7F, 0x80, 0xFF):
        assert _target_rejects(b'/a' + bytes([bad]) + b'/b'), hex(bad)


def test_target_table_accepts_a_realistic_target():
    assert not _target_rejects(b'/api/v1/x?q=1&r=%20#frag')


def test_host_regex_classifies_all_256_octets_identically():
    mismatched = [b for b in range(256)
                  if bool(_HOST_FORBIDDEN_RE.search(bytes([b]))) is not (b in _HOST_FORBIDDEN_BYTES)]
    assert mismatched == []


def test_host_regex_is_derived_from_the_frozenset():
    # The two must not be able to drift; the set is the single source of truth.
    for b in _HOST_FORBIDDEN_BYTES:
        assert _HOST_FORBIDDEN_RE.search(b'example.com' + bytes([b]))


# ---- the same decisions, through the real parser ---------------------------

def _req(target: bytes = b'/', host: bytes = b'localhost') -> bytes:
    return b'GET ' + target + b' HTTP/1.1\r\nHost: ' + host + b'\r\n\r\n'


@pytest.mark.parametrize('bad', [b'\x00', b'\x1f', b'\x7f', b'\x80', b'\xff'])
def test_parse_rejects_forbidden_target_octet(actor, bad):
    with pytest.raises(BadRequestError):
        actor._parse(_req(target=b'/a' + bad + b'b'))


@pytest.mark.parametrize('bad', [b'/', b'?', b'#', b' ', b'\t', b'@'])
def test_parse_rejects_forbidden_host_octet(actor, bad):
    with pytest.raises(BadRequestError):
        actor._parse(_req(host=b'exam' + bad + b'ple'))


def test_parse_accepts_a_clean_request(actor):
    conn = actor._parse(_req(target=b'/api/v1/x?q=1', host=b'localhost:8080'))
    assert conn.path == '/api/v1/x'
    assert conn.query_string == b'q=1'
    assert conn.headers.get(b'host') == b'localhost:8080'


def test_parse_rejects_empty_target(actor):
    # `not path` guarded this before the table and still has to: an empty
    # target survives `translate` (nothing to reject) but is not a target.
    with pytest.raises(BadRequestError):
        actor._parse(b'GET  HTTP/1.1\r\nHost: localhost\r\n\r\n')


# ---- field-name (tchar) table ----------------------------------------------

def test_tchar_table_classifies_all_256_octets_identically():
    from blackbull.server.http1_actor import _FIELD_NAME_INVALID_RE, _TCHAR_OCTETS
    mismatched = [
        b for b in range(256)
        if bool(bytes([b]).translate(None, _TCHAR_OCTETS))
        is not bool(_FIELD_NAME_INVALID_RE.search(bytes([b])))
    ]
    assert mismatched == []


@pytest.mark.parametrize('name', [b'x y', b'x\x00y', b'x(y', b'x\ty', b'x\x80y'])
def test_parse_rejects_non_token_header_name(actor, name):
    with pytest.raises(BadRequestError):
        actor._parse(b'GET / HTTP/1.1\r\nHost: h\r\n' + name + b': v\r\n\r\n')


def test_parse_accepts_every_tchar_in_a_header_name(actor):
    from blackbull.server.http1_actor import _TCHAR_OCTETS
    conn = actor._parse(b'GET / HTTP/1.1\r\nHost: h\r\n' + _TCHAR_OCTETS + b': v\r\n\r\n')
    assert conn.headers.get(_TCHAR_OCTETS.lower()) == b'v'


# ---- whole-block CTL pre-scan ----------------------------------------------
#
# The per-value regex is skipped when one C-level pass proves no field value
# can contain a forbidden octet.  The pre-scan is a fast path, never a
# rejection: when it trips, the per-header regex still runs and still raises
# the original error.  So what these tests pin is that nothing changes.

def _hdr(extra: bytes) -> bytes:
    return b'GET / HTTP/1.1\r\nHost: h\r\n' + extra + b'\r\n\r\n'


@pytest.mark.parametrize('bad', [b'\x00', b'\x01', b'\x08', b'\x0b', b'\x0c',
                                 b'\x1f', b'\x7f'])
def test_parse_rejects_ctl_in_header_value(actor, bad):
    with pytest.raises(BadRequestError):
        actor._parse(_hdr(b'X-Thing: a' + bad + b'b'))


@pytest.mark.parametrize('bad', [b'\r', b'\n'])
def test_parse_rejects_bare_cr_or_lf_in_header_value(actor, bad):
    # Neither is a line terminator on its own; both are smuggling vectors.
    with pytest.raises(BadRequestError):
        actor._parse(_hdr(b'X-Thing: a' + bad + b'b'))


def test_parse_allows_htab_inside_a_header_value(actor):
    # HTAB is the one CTL a field value may carry (RFC 9110 §5.5).
    conn = actor._parse(_hdr(b'X-Thing: a\tb'))
    assert conn.headers.get(b'x-thing') == b'a\tb'


def test_parse_allows_obs_text_in_a_header_value(actor):
    conn = actor._parse(_hdr(b'X-Thing: caf\xc3\xa9'))
    assert conn.headers.get(b'x-thing') == b'caf\xc3\xa9'


def test_ctl_in_request_line_still_reports_the_request_line_error(actor):
    # The pre-scan covers the whole block, so a CTL in the request target
    # trips it — but the target check must still be what rejects the request.
    with pytest.raises(BadRequestError, match='request-target'):
        actor._parse(b'GET /a\x01b HTTP/1.1\r\nHost: h\r\n\r\n')

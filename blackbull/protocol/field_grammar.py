"""Shared HTTP field and URI scheme grammar."""
import re

#: RFC 9110 §5.6.2 tchar — a field name's octets.
TCHAR_OCTETS = (b"!#$%&'*+-.^_`|~"
                b'0123456789'
                b'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
                b'abcdefghijklmnopqrstuvwxyz')

#: The same, for per-octet membership.
TCHAR_SET: frozenset[int] = frozenset(TCHAR_OCTETS)

#: RFC 9110 §5.5 field-content — a field value's octets: HTAB, SP, VCHAR,
#: obs-text.
FIELD_VALUE_ALLOWED_OCTETS = bytes(
    c for c in range(256) if c == 0x09 or 0x20 <= c <= 0x7E or c >= 0x80)

#: The same, for per-octet membership.
FIELD_VALUE_ALLOWED_SET: frozenset[int] = frozenset(FIELD_VALUE_ALLOWED_OCTETS)

#: RFC 3986 §3.1 — URI scheme, also used in HTTP absolute-form targets.
URI_SCHEME_RE = re.compile(rb'[A-Za-z][A-Za-z0-9+.-]*')


def method_token_is_valid(value: bytes) -> bool:
    """RFC 9110 §9.1 method = token (§5.6.2 token = 1*tchar).

    Both transports grade their method with this — HTTP/1.1 its request line,
    HTTP/2 ``:method`` — so a method one of them refuses cannot run on the
    other.
    """
    return bool(value) and not value.translate(None, TCHAR_OCTETS)


#: Methods and schemes nearly every request carries, already known to satisfy
#: the rule each stands in for.  The request paths decide these by set
#: membership before falling back to the grammar; anything not listed still
#: goes through it, so the accept set is unchanged.
COMMON_METHODS_OCTETS = frozenset({b'GET', b'HEAD', b'POST', b'PUT', b'DELETE',
                                   b'OPTIONS', b'PATCH', b'CONNECT', b'TRACE'})

#: The same methods as the ``str`` values a pseudo-header carries.  Derived
#: from the octets so HTTP/1.1's request line and HTTP/2's ``:method`` cannot
#: come to accept different methods.
COMMON_METHODS = frozenset(m.decode('ascii') for m in COMMON_METHODS_OCTETS)

#: The schemes, as the ``str`` a pseudo-header carries — checked against
#: ``URI_SCHEME_RE``.
COMMON_SCHEMES = frozenset({'https', 'http'})

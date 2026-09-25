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

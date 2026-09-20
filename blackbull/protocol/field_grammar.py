"""The HTTP field grammar HTTP/1.1 and HTTP/2 share — RFC 9110 §5.5, §5.6.2."""
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

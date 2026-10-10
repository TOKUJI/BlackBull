"""Shared HTTP field, authority and URI scheme grammar."""
from functools import lru_cache
import ipaddress
import re
from typing import Iterable

#: RFC 9110 §5.6.2 tchar — a field name's octets.
TCHAR_OCTETS = (b"!#$%&'*+-.^_`|~"
                b'0123456789'
                b'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
                b'abcdefghijklmnopqrstuvwxyz')

#: The same, for per-octet membership.
TCHAR_SET: frozenset[int] = frozenset(TCHAR_OCTETS)

#: tchar without uppercase: the octets of a name already in the lowercase
#: form HTTP/2 requires (RFC 9113 §8.2) and BlackBull sends on both transports.
LOWERCASE_TCHAR_OCTETS = TCHAR_OCTETS.translate(None, b'ABCDEFGHIJKLMNOPQRSTUVWXYZ')

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


class FieldError(ValueError):
    """A field section that breaks the field grammar; the request gets 400."""


def field_value(raw: bytes, *, check: bool = True) -> bytes:
    """Return *raw* without its edge SP/HTAB.

    The result carries no CTL: raises [`FieldError`][] for one, unless *check*
    is false because the caller has already proved the octets clean.
    """
    value = raw.strip(b' \t')
    if check and value.translate(None, FIELD_VALUE_ALLOWED_OCTETS):
        raise FieldError(f'control octet in field value {value!r}')
    return value


def field_line(line: bytes) -> tuple[bytes, bytes]:
    """Split one HTTP/1.1 field line (no CRLF) into its lowercase tchar name and
    the raw value after the colon, still unstripped (RFC 9112 §5).

    Raises [`FieldError`][] for obs-fold, a missing colon, whitespace before
    the colon, or a name outside tchar.  Pass the value to [`field_value`][].
    """
    if line[:1] in (b' ', b'\t'):
        raise FieldError(f'obsolete line folding rejected: {line!r}')
    colon = line.find(b':')
    if colon < 1:
        raise FieldError(f'malformed field line: {line!r}')
    name = line[:colon]
    if name.translate(None, TCHAR_OCTETS):
        if name[-1] in (0x20, 0x09):
            raise FieldError(f'whitespace before colon (smuggling vector): {line!r}')
        raise FieldError(f'invalid field name {name!r}')
    return name.lower(), line[colon + 1:]


def normalized_fields(pairs: Iterable) -> list[tuple[bytes, bytes]]:
    """Return *pairs* with every name lowercased and every value's edge SP/HTAB
    removed, in order.

    Every returned name is lowercase tchar and every value is free of CTL.
    Raises [`FieldError`][] for a pair that is not two ``bytes``, a name
    outside tchar, or a value with a CTL.
    """
    out = []
    for pair in pairs:
        name, value = pair
        if type(name) is not bytes or type(value) is not bytes:
            raise FieldError(f'field is not two bytes strings: {pair!r}')
        name = name.lower()
        if not name or name.translate(None, LOWERCASE_TCHAR_OCTETS):
            raise FieldError(f'invalid field name {name!r}')
        out.append((name, field_value(value)))
    return out


def media_type(value: bytes) -> bytes:
    """Return the lowercase ``type/subtype`` of a media type or media range,
    without its parameters (RFC 9110 §8.3.1); ``b''`` for an empty value."""
    return value.split(b';', 1)[0].strip(b' \t').lower()


def list_members(value: bytes) -> list[bytes]:
    """Return the lowercase members of a comma-separated list value without
    OWS, empty members dropped (RFC 9110 §5.6.1).  Not for case-sensitive
    members such as entity-tags."""
    return [m for m in (p.strip(b' \t').lower() for p in value.split(b',')) if m]


def if_none_match_hit(value: bytes, etag: bytes) -> bool:
    """Whether an If-None-Match *value* matches *etag* by weak comparison:
    ``*``, or any listed entity-tag with ``W/`` ignored (RFC 9110 §13.1.2)."""
    if value == b'*':
        return True
    target = etag[2:] if etag.startswith(b'W/') else etag
    for tag in value.split(b','):
        tag = tag.strip(b' \t')
        if (tag[2:] if tag.startswith(b'W/') else tag) == target:
            return True
    return False


#: RFC 9110 §6.5.1 — fields a trailer section may not carry: framing, routing,
#: authentication, request modifiers, response control and content handling.
PROHIBITED_TRAILER_FIELDS = frozenset((
    b'transfer-encoding', b'content-length', b'host', b'content-type',
    b'content-encoding', b'content-range', b'trailer', b'te',
    b'authorization', b'proxy-authorization', b'cookie', b'set-cookie',
    b'cache-control', b'expect', b'max-forwards', b'pragma', b'range',
))


# RFC 3986 §3.2 — authority = [userinfo "@"] host [":" port]; these octets are
# not in one.  ``@`` is the deprecated userinfo component, the controls are
# CTL/DEL and the high bytes non-ASCII.
HOST_FORBIDDEN_BYTES = (
    frozenset(b'/?# \t@') | frozenset(range(0x20)) | frozenset({0x7F})
    | frozenset(range(0x80, 0x100)))

# RFC 3986 §3.2.2: brackets enclose IPv6, and an unbracketed colon starts
# a numeric port.
_AUTHORITY_SCAN_BYTES = HOST_FORBIDDEN_BYTES | {0x5B, 0x5D}  # '[' ']'
_AUTHORITY_SCAN_RE = re.compile(
    b'[' + re.escape(bytes(sorted(_AUTHORITY_SCAN_BYTES))) + b']'
    b'|\\A:|:[0-9]*[^0-9]')


def _ip_literal_is_valid(value: bytes) -> bool:
    """RFC 3986 §3.2.2 — whether *value*'s bracketed host is an IPv6 address
    (IPvFuture is unsupported)."""
    if (
        not value.startswith(b'[')
        or value.count(b'[') != 1
        or value.count(b']') != 1
    ):
        return False

    close = value.find(b']', 1)
    tail = value[close + 1:]

    if tail and (
        tail[:1] != b':'
        or not HOST_FORBIDDEN_BYTES.isdisjoint(tail[1:])
    ):
        return False

    try:
        # ``UnicodeDecodeError`` is a ``ValueError``: a high byte inside the
        # bracket reaches this decode.
        ipaddress.IPv6Address(value[1:close].decode('ascii'))
    except ValueError:
        return False
    return True


def authority_is_valid(value: bytes) -> bool:
    """Whether *value* is a URI authority ``host [":" port]`` (RFC 3986 §3.2):
    ASCII, no userinfo, a reg-name, IPv4 or bracketed IPv6 host."""
    match = _AUTHORITY_SCAN_RE.search(value)
    if match is None:
        return True
    return match[0] in (b'[', b']') and _ip_literal_is_valid(value)


# Bounded cache: the bytes are the peer's, and a client repeats its authority.
authority_is_valid = lru_cache(maxsize=256)(authority_is_valid)


def host_field_value(fields: list[tuple[bytes, bytes]] | None) -> bytes | None:
    """Return the value of the one ``host`` field in *fields*, or ``None`` when
    there is none.

    *fields* are ``(name, value)`` pairs whose values carry no edge SP/HTAB.
    Raises [`FieldError`][] for more than one field, an empty value, or a value
    that is not an authority.
    """
    if not fields:
        return None
    if len(fields) > 1:
        raise FieldError(f'multiple Host fields ({len(fields)})')
    value = fields[0][1]
    if not value:
        raise FieldError('empty Host field')
    if not authority_is_valid(value):
        raise FieldError(f'invalid Host authority {value!r}')
    return value

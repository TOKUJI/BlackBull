"""Acceptability shared by dynamic and precompressed responses."""
import re

from ..headers import Headers
from ..protocol.field_grammar import TCHAR_OCTETS


_QUALITY = re.compile(rb'q=(0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)', re.IGNORECASE)


def accept_encoding_value(headers: Headers) -> bytes:
    fields = headers.getlist(b'accept-encoding')
    if not fields:
        return b''
    if len(fields) == 1:
        return fields[0][1]
    return b','.join(value for _, value in fields)


def parse_accept_encoding(value: bytes) -> dict[bytes, bool]:
    accepted: dict[bytes, bool] = {}
    for member in value.split(b','):
        name, separator, parameter = member.partition(b';')
        name = name.strip(b' \t').lower()
        if not name or name.translate(None, TCHAR_OCTETS):
            continue
        allowed = True
        if separator:
            quality = _QUALITY.fullmatch(parameter.strip(b' \t'))
            allowed = quality is not None and bool(quality[1].strip(b'0.'))
        # A malformed or refused explicit entry cannot be revived by a duplicate
        # or by the wildcard fallback used by either caller.
        accepted[name] = accepted.get(name, True) and allowed
    return accepted

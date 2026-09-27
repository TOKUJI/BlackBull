"""Acceptability shared by dynamic and precompressed responses."""
import re
from collections.abc import Collection

from ..protocol.field_grammar import TCHAR_OCTETS


_QUALITY = re.compile(rb'q=(0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)', re.IGNORECASE)


def select_encoding(value: bytes, available: Collection[str]) -> str | None:
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
        # or by the wildcard fallback.
        accepted[name] = accepted.get(name, True) and allowed
    wildcard = accepted.get(b'*', False)
    for name in ('br', 'zstd', 'gzip'):
        if name in available and accepted.get(name.encode(), wildcard):
            return name
    return None

"""Acceptability shared by dynamic and precompressed responses."""
import re
from collections.abc import Collection
from functools import lru_cache

from ..protocol.field_grammar import TCHAR_OCTETS


#: The server's preference, each name with the token a client sends.
_PREFERENCE = (('br', b'br'), ('zstd', b'zstd'), ('gzip', b'gzip'))
_QUALITY = re.compile(rb'q=(0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)', re.IGNORECASE)


def select_encoding(value: bytes, available: Collection[str]) -> str | None:
    for name in acceptable_encodings(value):
        if name in available:
            return name
    return None


@lru_cache(maxsize=128)
def acceptable_encodings(value: bytes) -> tuple[str, ...]:
    """The codings *value* accepts, in the server's preference order.  Clients
    repeat their header, so the parse is cached."""
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
    return tuple(name for name, token in _PREFERENCE
                 if accepted.get(token, wildcard))

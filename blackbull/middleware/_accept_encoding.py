"""Acceptability shared by dynamic and precompressed responses."""
import re
from collections.abc import Collection
from functools import lru_cache

from ..protocol.field_grammar import TCHAR_OCTETS


#: The server's preference, each name with the token a client sends.
_PREFERENCE = (('br', b'br'), ('zstd', b'zstd'), ('gzip', b'gzip'))
_QUALITY = re.compile(rb'q=(0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)', re.IGNORECASE)


def select_encoding(value: bytes, available: Collection[str]) -> str | None:
    accepted, wildcard = _accepted(value)
    for name, token in _PREFERENCE:
        if name in available and accepted.get(token, wildcard):
            return name
    return None


@lru_cache(maxsize=128)
def _accepted(value: bytes) -> tuple[dict[bytes, bool], bool]:
    """Each coding *value* names, whether it is acceptable, and whether ``*``
    is.  Clients repeat their header, so the parse is cached; callers must
    not mutate the returned dict."""
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
    return accepted, accepted.get(b'*', False)

"""Shared utilities and enumerations.

Provides:

- `Scheme`: ``StrEnum`` with ``http`` and ``websocket`` values used throughout routing.
- `pop_safe`: moves a key between dicts with an optional rename, no-op when absent.
- `do_nothing`: awaitable no-op used as the innermost middleware-chain terminator.
- `HTTP2`: the HTTP/2 client connection preface (RFC 9113 §3.4).
"""
from enum import StrEnum, auto

import logging

logger = logging.getLogger(__name__)


async def do_nothing(*args, **kwargs):
    pass


class Scheme(StrEnum):
    http = auto()
    websocket = auto()


def pop_safe(key, source: dict, target: dict, *, new_key=None) -> dict:
    """Move ``source[key]`` into *target* (under *new_key* if given), if present.

    Returns *target* unchanged when *key* is absent from *source*.
    """
    if key in source:
        if new_key:
            target[new_key] = source.pop(key)
        else:
            target[key] = source.pop(key)
    return target


# HTTP/2 client connection preface (RFC 9113 §3.4).
HTTP2 = b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'
# 0x505249202a20485454502f322e300d0a0d0a534d0d0a0d0a

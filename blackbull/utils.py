"""Shared utilities and enumerations.

Provides:

- `Scheme`: ``StrEnum`` with ``http`` and ``websocket`` values used throughout routing.
- `pop_safe`: moves a key between dicts with an optional rename, no-op when absent.
- `do_nothing`: awaitable no-op used as the innermost middleware-chain terminator.
- `HTTP2`: the HTTP/2 client connection preface (RFC 9113 §3.4).
- `is_client_error` / `is_server_error`: 3.11-safe HTTP status classifiers.
"""
from enum import StrEnum, auto
from http import HTTPStatus

import logging

logger = logging.getLogger(__name__)


# ``HTTPStatus.is_client_error`` / ``.is_server_error`` are Python 3.12
# additions; using them directly breaks the declared ``requires-python =
# ">=3.11"`` floor (a real crash at ``BlackBull()`` construction on 3.11 —
# ``AttributeError: 'HTTPStatus' object has no attribute 'is_client_error'``)
# and blocks running under a PyPy pinned to 3.11 compat.  ``HTTPStatus`` is an
# ``IntEnum``, so an integer-range check is equivalent and version-agnostic.
def is_client_error(status: HTTPStatus | int) -> bool:
    """True for a 4xx status (RFC 9110 client error).  3.11-safe replacement
    for ``HTTPStatus.is_client_error`` (which is 3.12+)."""
    return 400 <= int(status) <= 499


def is_server_error(status: HTTPStatus | int) -> bool:
    """True for a 5xx status (RFC 9110 server error).  3.11-safe replacement
    for ``HTTPStatus.is_server_error`` (which is 3.12+)."""
    return 500 <= int(status) <= 599


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

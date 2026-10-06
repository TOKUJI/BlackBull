"""Connection IDs with a per-process prefix and monotonic sequence.

Regenerate the random prefix after fork so children cannot share an ID sequence.
"""
from __future__ import annotations

import itertools
import os

_seq = itertools.count()
_prefix = os.urandom(6).hex()


def _reseed() -> None:
    """Fresh prefix + sequence for a forked child (multi-worker prefork)."""
    global _prefix, _seq
    _prefix = os.urandom(6).hex()
    _seq = itertools.count()


os.register_at_fork(after_in_child=_reseed)


def new_connection_id() -> str:
    """Opaque fixed-width hex id, unique per connection within a process.

    The sequence wraps at 2**32; a wrap collision would additionally require
    the 4-billion-connection-old id to still be referenced.
    """
    return f'{_prefix}{next(_seq) & 0xffffffff:08x}'

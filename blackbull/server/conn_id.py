"""Cheap process-unique connection ids.

Replaces the per-connection ``uuid.uuid4()`` (16 bytes of urandom plus UUID
formatting, ~2 µs on the accept hot path) and the earlier 4-byte
``os.urandom`` scheme.  32-bit random ids have real birthday-bound collision
odds at production churn: ~1.2 % at 10 k concurrent connections, ~50 % at
65 k.

Format: ``<12-hex process prefix><8-hex sequence>`` (20 hex characters).
The prefix is drawn once per process and re-drawn in forked children via
``os.register_at_fork``, so ids are unique within a process by construction
(monotonic sequence, no birthday bound) and collide across processes only if
two 48-bit prefixes collide — ~3×10⁻¹² for a 32-worker fleet.  No entropy
syscall is paid per connection.
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

"""Transport operations shared across protocol-specific scenario executors.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def half_close(writer) -> bool:
    """Send write-side FIN while retaining reads; return whether it succeeded.

    TLS does not support this. Assert the result when the test requires half-close.
    """
    if writer is None:
        return False
    transport = getattr(writer, 'transport', writer)
    try:
        if not transport.can_write_eof():
            return False
        transport.write_eof()
    except Exception:  # pragma: no cover - the peer may already be gone
        logger.debug('half_close raced the peer')
        return False
    return True


__all__ = ['half_close']

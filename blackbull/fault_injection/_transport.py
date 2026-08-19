"""Transport primitives shared by the four scenario executors.

The scenario *vocabularies* are per-protocol and per-role; the socket
operations underneath them are neither.  A half-close is the same system
call whether it is a broken HTTP/1.1 client or a broken HTTP/2 server
issuing it, so it lives here rather than four times over — the Sprint 108
lesson was that four copies of one idea drift, and the drift is invisible
until something outside the project reads the bytes.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def half_close(writer) -> bool:
    """Send FIN on the write side; leave the read side open.

    Returns whether the transport accepted it, rather than raising or
    silently doing nothing.  ``write_eof`` is genuinely unsupported on some
    transports — TLS is the one that matters here, because a half-close has
    no TLS equivalent — and a scenario whose half-close quietly did nothing
    would report a pass while testing the peer's *keep-alive* path instead.
    Recording the miss is what lets a test assert it actually happened.
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

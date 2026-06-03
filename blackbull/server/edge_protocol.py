"""Custom asyncio.Protocol for the keepalive-idle read path.

Sprint 30 Tier 1.5 Step 2 — replaces the default
``asyncio.StreamReaderProtocol`` used by ``asyncio.start_server`` so
that the EOF → close chain folds into one event-loop turn per FD
lifecycle (vs ~3-5 turns through the default StreamReader future
wakeup chain).

How it lands
------------
This is just the Protocol class.  Wiring it into ``server.py`` (Step 3)
will switch ``ASGIServer`` from ``asyncio.start_server`` to
``loop.create_server`` with this protocol factory.  Until then this
module is dormant — landing now lets us tests it in isolation.

State machine
-------------
::

    ┌──────────────────────────────┐
    │   __init__ (factory called)  │
    └──────────────┬───────────────┘
                   │ connection_made(transport)
                   ▼
        ┌───────────────────────────┐
   ┌───►│  ACTIVE                   │
   │    │  buffer = ProtocolBuffer()│
   │    │  writer = StreamWriter()  │
   │    │  task = create_task(cb)   │
   │    └────┬─────────────────┬────┘
   │         │ data_received   │ eof_received  (SYNCHRONOUS)
   │         ▼                 ▼
   │   ┌──────────┐    ┌────────────────────────┐
   │   │buffer.   │    │ 1) transport.close()   │
   │   │feed()    │    │ 2) buffer.feed_eof()   │
   │   └────┬─────┘    │ 3) return False        │
   │        │          └──────────┬─────────────┘
   └────────┘                     │
                                  ▼
                     ┌──────────────────────────┐
                     │  CLOSED                  │
                     │  connection_lost(exc)    │
                     │  buffer.close()          │
                     │  (task winds itself up)  │
                     └──────────────────────────┘

The synchronous ``eof_received`` is the architectural change: it
runs in the same event-loop iteration as the epoll batch that
delivered the EOF.  By the time the actor task next runs, the FD
is already on its way to ``connection_lost``-fired ``_sock.close()``
— no readuntil-resume → break → finally → writer.close() chain
needed to release the FD.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from .protocol_buffer import ProtocolBuffer

logger = logging.getLogger(__name__)


# The callback registered with ``loop.create_server(...)``-style APIs.
# We pass our ``ProtocolBuffer`` as the first arg and a writer compatible
# with ``asyncio.StreamWriter`` as the second, mirroring
# ``asyncio.start_server``'s (reader, writer) convention so the existing
# AsyncioReader / AsyncioWriter shims need minimal adaptation in Step 4.
ConnectedCallback = Callable[
    [ProtocolBuffer, asyncio.StreamWriter],
    Awaitable[None],
]


class _BlackBullProtocol(asyncio.streams.FlowControlMixin):
    """asyncio.Protocol that fronts the ProtocolBuffer read path.

    Subclasses ``FlowControlMixin`` (asyncio internal) so we get
    ``pause_writing`` / ``resume_writing`` / ``_drain_helper`` for
    free — letting us reuse ``asyncio.StreamWriter`` as-is for the
    write side without losing backpressure.

    Reuse of StreamWriter is deliberate: the write-side bottleneck
    isn't where the Sprint 30 cliff lives, and the existing
    ``AsyncioWriter`` shim already wraps StreamWriter cleanly.  The
    read-side is where we win turns, and that's where the buffer
    replaces StreamReader.
    """

    __slots__ = (
        '_connected_cb',
        '_buffer',
        '_writer',
        '_task',
        '_transport',
        '_eof_seen',
    )

    def __init__(
        self,
        connected_cb: ConnectedCallback,
        *,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        super().__init__(loop=loop)
        self._connected_cb = connected_cb
        self._buffer: Optional[ProtocolBuffer] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._task: Optional[asyncio.Task] = None
        self._transport: Optional[asyncio.Transport] = None
        self._eof_seen: bool = False

    # ------------------------------------------------------------------
    # asyncio.Protocol overrides
    # ------------------------------------------------------------------

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        # The ``asyncio.Transport`` invariant: SelectorTransport satisfies
        # the StreamWriter contract (``write`` / ``writelines`` / ``close``
        # / pause-writing-events).  We don't type-narrow at runtime
        # because TLS variants subclass differently across Python versions.
        self._transport = transport  # type: ignore[assignment]
        self._buffer = ProtocolBuffer()
        # ``asyncio.StreamWriter`` needs a Protocol that supplies
        # _drain_helper + pause/resume_writing semantics — that's why
        # we extend FlowControlMixin.  The ``reader=None`` argument is
        # honoured by StreamWriter; reads never go through it.
        self._writer = asyncio.StreamWriter(
            transport, self, None, self._loop)
        # Spawn the connection-actor task.  Once the actor returns
        # (cleanly or via exception) the task is done; the protocol
        # itself remains alive until ``connection_lost`` fires.
        self._task = self._loop.create_task(
            self._connected_cb(self._buffer, self._writer))

    def data_received(self, data: bytes) -> None:
        if self._buffer is None:
            # connection_made not yet fired — should not happen in
            # practice but defended-by-default per CodeQL's empty-except
            # pattern from PR #33.
            logger.debug('edge_protocol: data_received before connection_made'
                         ' (%d bytes dropped)', len(data))
            return
        self._buffer.feed(data)

    def eof_received(self) -> bool:
        """Peer half-closed the connection.

        Synchronously: close the transport, feed EOF into the buffer.
        Returns False so asyncio fully closes (no half-open).

        This is the architectural point of Tier 1.5 — all the
        downstream cleanup runs without waiting on
        readuntil-resume → break → finally chains.
        """
        self._eof_seen = True
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception as exc:
                # Best-effort: the transport may already be in a
                # half-broken state (TLS aborted, etc.).  Logging at
                # DEBUG mirrors the close-path style introduced in PR
                # #33's CodeQL fix.
                logger.debug(
                    'edge_protocol: transport.close raised in eof_received'
                    ' (%s) — ignoring; connection_lost will still fire', exc)
        if self._buffer is not None:
            self._buffer.feed_eof()
        # Return False — asyncio should also close on its side.  True
        # would keep the connection half-open (StreamReaderProtocol's
        # default) which inflates the suspended-task count we're
        # trying to bound.
        return False

    def connection_lost(self, exc: Optional[BaseException]) -> None:
        """Transport fully closed.

        Drains state and lets the actor task wind itself up.  Does
        NOT cancel the actor task here — the buffer's ``feed_eof``
        in ``eof_received`` already woke any suspended read, and the
        actor's natural exit through ``IncompleteReadError`` → break
        → finally is the safer cleanup path (cancellation can
        interleave badly with the actor's own ``finally: await
        writer.close()`` in ConnectionActor).
        """
        if self._buffer is not None:
            # close() differs from feed_eof() in that it discards any
            # remaining buffered bytes — by the time connection_lost
            # fires we've decided the connection is dead, and a
            # partial-request half-buffer would only confuse the
            # actor.
            self._buffer.close()
        # Let FlowControlMixin clean up its pause/resume state.
        super().connection_lost(exc)

    # ------------------------------------------------------------------
    # Inspection helpers (handy for tests, server.py's max_connections
    # check, and future graceful-shutdown work)
    # ------------------------------------------------------------------

    @property
    def buffer(self) -> Optional[ProtocolBuffer]:
        return self._buffer

    @property
    def writer(self) -> Optional[asyncio.StreamWriter]:
        return self._writer

    @property
    def task(self) -> Optional[asyncio.Task]:
        return self._task

    @property
    def transport(self) -> Optional[asyncio.Transport]:
        return self._transport

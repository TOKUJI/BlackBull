"""Connection Actor — Phase 6 Step 5."""
import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..actor import Actor, Message
from ..event_aggregator import EventAggregator
from .cap_log import CapHitCounter
from .deadline import ConnectionDeadline
from .protocol_registry import (ConnectionView, ProtocolBinding,
                                ProtocolRegistry)
from .recipient import (AbstractReader,
                        _HTTP2_STREAM_QUEUE_DEPTH, _WS_EVENT_QUEUE_DEPTH)
from .sender import AbstractWriter

logger = logging.getLogger(__name__)


class ConnectionActor(Actor):
    """One per accepted TCP connection.

    Detects protocol, spawns the appropriate protocol Actor, and isolates
    failures so one bad connection cannot affect others.

    Supervisor strategy: isolate — ExceptionGroup from the TaskGroup is
    caught and emitted as a single on_error event; the connection is always
    closed in finally.
    """

    def __init__(
        self,
        reader: AbstractReader,
        writer: AbstractWriter,
        app: Callable[..., Awaitable[None]],
        aggregator: 'EventAggregator | None',
        *,
        peername: tuple[str, int] | None = None,
        sockname: tuple[str, int] | None = None,
        ssl: bool = False,
        alpn: str | None = None,
        stream_queue_depth: int = _HTTP2_STREAM_QUEUE_DEPTH,
        ws_queue_depth: int = _WS_EVENT_QUEUE_DEPTH,
        registry: ProtocolRegistry | None = None,
        bound_binding: ProtocolBinding | None = None,
        connection_id: str = '',
    ) -> None:
        super().__init__()
        self._reader = reader
        self._writer = writer
        self._app = app
        self._aggregator = aggregator
        self._peername = peername
        self._sockname = sockname
        self._ssl = ssl
        self._alpn = alpn
        self._stream_queue_depth = stream_queue_depth
        self._ws_queue_depth = ws_queue_depth
        # A registry is always available: tests construct ConnectionActor with
        # positional (reader, writer, app, aggregator) and no registry, so fall
        # back to a default holding only the built-in http1/http2 bindings.
        self._registry = registry if registry is not None else ProtocolRegistry()
        # When set (port-bound raw protocol), detection is skipped entirely.
        self._bound_binding = bound_binding
        self._connection_id = connection_id

    async def run(self) -> None:
        # Per-connection cap-hit rate-limit state — bound on the
        # ambient contextvar so every log_cap_hit() call inside this
        # task tree (protocol actor, stream actors, recipients,
        # senders) picks it up without constructor plumbing.  TaskGroup
        # children inherit the context automatically.
        counter = CapHitCounter()
        with counter.bind():
            if self._aggregator is not None:
                protocol = (self._bound_binding.name
                            if self._bound_binding is not None else 'http')
                await self._aggregator.on_connection_accepted(
                    self._peername, protocol=protocol)
            try:
                await self._dispatch()
            except Exception as exc:
                if self._aggregator is not None:
                    await self._aggregator.on_error({}, exc)
            finally:
                # One summary record per suppressed cap before the
                # transport goes away.
                counter.flush(peer=self._peername)
                await self._writer.close()

    async def _dispatch(self) -> None:
        import asyncio  # noqa: PLC0415
        from ..env import get_settings as _get_settings  # noqa: PLC0415
        cfg = _get_settings()

        # Slowloris defence at protocol-detection: a connected peer that
        # never sends a first line / preface would otherwise hold a slot
        # forever waiting on readuntil / readexactly here.  We use the
        # same ``header_timeout`` setting HTTP1Actor uses for its own
        # header-completion deadline — the practical worst case is two
        # timeouts back-to-back (protocol-detect + first request headers),
        # still bounded.  ``header_timeout=0`` disables both halves.
        deadline = cfg.header_timeout if cfg.header_timeout > 0 else None

        # Sprint 23: one rescheduled TimerHandle per connection replaces
        # the per-phase ``async with asyncio.timeout(d):`` allocations.
        # The deadline binds to *this* task — the per-connection dispatch
        # task — and gets passed down into HTTP1Actor / HTTP1Recipient so
        # each phase boundary (preface, headers, body chunk, keep-alive)
        # just re-arms the same handle.
        dl = ConnectionDeadline()

        conn = ConnectionView(
            reader=self._reader, writer=self._writer, app=self._app,
            aggregator=self._aggregator,
            peername=self._peername, sockname=self._sockname, ssl=self._ssl,
            alpn=self._alpn, deadline=dl, connection_id=self._connection_id,
            stream_queue_depth=self._stream_queue_depth,
            ws_queue_depth=self._ws_queue_depth,
        )

        # Port-bound non-ASGI protocol (Sprint 50): the listening socket already
        # identifies the protocol, so skip detection and the deadline machinery
        # entirely — the handler owns the connection lifetime.
        if self._bound_binding is not None:
            await self._bound_binding.serve_raw(conn)
            return

        # Sprint 50: dispatch is registry-driven.  ``http1``/``http2`` are
        # built-in bindings; the deadline-guarded reads + slowloris handling
        # stay here so the hot HTTP path is unchanged — only protocol selection
        # and Actor construction move into the bindings.

        # ALPN-negotiated protocol: the peer is committed.  For HTTP/2 the first
        # 24 bytes MUST be the connection preface (RFC 9113 §3.4).  Read exactly
        # 24 bytes rather than scanning for CRLF so an invalid preface is
        # rejected promptly instead of hanging the reader (h2spec §3.5 #2).
        alpn_binding = self._registry.by_alpn(self._alpn)
        if alpn_binding is not None:
            try:
                if deadline is not None:
                    with dl.guard(deadline):
                        preface = await self._reader.readexactly(24)
                else:
                    preface = await self._reader.readexactly(24)
            except (asyncio.TimeoutError, TimeoutError):
                # Peer connected via ALPN but never sent the preface.
                # Close — no GOAWAY since we don't have a SETTINGS exchange.
                logger.warning('slowloris: ALPN-%s peer sent no preface within %.1fs',
                               self._alpn, deadline)
                from .cap_log import log_cap_hit  # noqa: PLC0415
                log_cap_hit('header_timeout',
                            requested=deadline, limit=deadline,
                            peer=self._peername, protocol='h2')
                return
            await alpn_binding.serve_alpn(conn, preface)
            return

        # No ALPN (cleartext) or ALPN didn't match a binding — sniff the first line.
        try:
            if deadline is not None:
                with dl.guard(deadline):
                    first_line = await self._reader.readuntil(b'\r\n')
            else:
                first_line = await self._reader.readuntil(b'\r\n')
        except (asyncio.TimeoutError, TimeoutError):
            # Slowloris during protocol-detect: best-effort 408 then close.
            # We don't yet know the protocol; the bytes are HTTP/1.1-compatible
            # so a 408 will be parseable by an h1 client and ignored by an
            # h2 client (which would have been on the ALPN path anyway).
            logger.warning(
                'slowloris: peer sent no first line within %.1fs', deadline)
            from .cap_log import log_cap_hit  # noqa: PLC0415
            log_cap_hit('header_timeout',
                        requested=deadline, limit=deadline,
                        peer=self._peername, protocol='http1')
            try:
                await self._writer.write(
                    b'HTTP/1.1 408 Request Timeout\r\n'
                    b'connection: close\r\n'
                    b'content-length: 0\r\n\r\n')
            except Exception:
                pass
            return

        # Shared-port protocol detection (Sprint 51): raw bindings with a
        # ProtocolDetector get a chance to claim the connection before the
        # http1 fallback — enabling e.g. MQTT+HTTP on one port.  Iteration is
        # registration order (``raw_bindings`` is insertion-ordered), so when
        # multiple detectors match overlapping byte signatures the
        # first-registered binding wins.  Not exercised in Sprint 52 (a single
        # MQTT detector); revisit this ordering guarantee if multi-detector
        # ports arrive.
        for raw_binding in self._registry.raw_bindings.values():
            if (raw_binding.detector is not None
                    and raw_binding.detector.detect(first_line, self._alpn)):
                await raw_binding.serve_raw(conn)
                return

        # First cleartext binding to claim the line wins (http2 preface, then
        # the http1 fallback which matches any line and is registered last).
        for binding in self._registry.cleartext_bindings:
            if binding.matches_cleartext(first_line):
                await binding.serve_cleartext(conn, first_line)
                return

    async def _handle(self, msg: Message) -> None:
        raise NotImplementedError

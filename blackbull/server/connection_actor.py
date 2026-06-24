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
from .recipient import (AbstractReader, PrefixReader,
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
        # Protocol name reported on the lifecycle events.  At accept time the
        # h1/h2 split isn't known, so the shared listener reports ``'http'``;
        # a port-bound binding already knows its name.  ``_dispatch`` refines
        # this to the actually-served binding once detection picks one.
        self._served_protocol = (bound_binding.name
                                 if bound_binding is not None else 'http')

    async def run(self) -> None:
        # Per-connection cap-hit rate-limit state — bound on the
        # ambient contextvar so every log_cap_hit() call inside this
        # task tree (protocol actor, stream actors, recipients,
        # senders) picks it up without constructor plumbing.  TaskGroup
        # children inherit the context automatically.
        import time  # noqa: PLC0415
        counter = CapHitCounter()
        start = time.monotonic()
        with counter.bind():
            if self._aggregator is not None:
                await self._aggregator.on_connection_accepted(
                    self._peername, protocol=self._served_protocol)
            try:
                await self._dispatch()
            except Exception as exc:
                # One error path for every protocol: a handler / actor that
                # raises is isolated here (decouple-connection-detection Stage 4
                # — RawProtocolActor's L2 error wrapper folded in).
                if self._aggregator is not None:
                    await self._aggregator.on_error({}, exc)
            finally:
                # One summary record per suppressed cap before the
                # transport goes away.
                counter.flush(peer=self._peername)
                await self._writer.close()
                # ``connection_closed`` now fires for *every* protocol, not just
                # raw/MQTT (the old asymmetry — decouple-connection-detection
                # symptom #5).  ``_served_protocol`` is the binding that handled
                # the connection (or the accept-time guess if none was selected).
                if self._aggregator is not None:
                    elapsed_ms = (time.monotonic() - start) * 1000
                    await self._aggregator.on_connection_closed(
                        self._peername, self._served_protocol, elapsed_ms)

    def _make_conn(self, reader: AbstractReader, dl: ConnectionDeadline) -> ConnectionView:
        return ConnectionView(
            reader=reader, writer=self._writer, app=self._app,
            aggregator=self._aggregator,
            peername=self._peername, sockname=self._sockname, ssl=self._ssl,
            alpn=self._alpn, deadline=dl, connection_id=self._connection_id,
            stream_queue_depth=self._stream_queue_depth,
            ws_queue_depth=self._ws_queue_depth,
        )

    def _detection_order(self) -> 'list[ProtocolBinding]':
        """Bindings consulted during cleartext detection, in priority order:
        registered raw detectors first (shared-port protocols such as MQTT),
        then the ordered cleartext chain (``http2`` preface, ``http1`` fallback).
        """
        return (list(self._registry.raw_bindings.values())
                + self._registry.cleartext_bindings)

    def _select(self, prefix: bytes, at_eof: bool,
                order: 'list[ProtocolBinding]') -> 'ProtocolBinding | None':
        """First binding (in priority order) to claim *prefix*, or ``None`` if a
        higher-priority binding still needs more bytes to decide.

        A binding is only consulted once ``prefix`` holds at least its
        ``detect_prefix_len`` bytes (or the peer has closed): until then we must
        not let a lower-priority catch-all (``http1``) claim a connection the
        higher-priority protocol might still own.
        """
        for binding in order:
            if not at_eof and len(prefix) < binding.detect_prefix_len:
                return None
            if binding.claims(prefix, self._alpn):
                return binding
        return None

    async def _peek_and_select(
        self, order: 'list[ProtocolBinding]',
    ) -> 'tuple[bytes, ProtocolBinding | None]':
        """Peek the smallest discriminating prefix and return ``(prefix, binding)``.

        Reads incrementally up to ``max(detect_prefix_len)`` and stops the
        instant a binding claims — so a short non-HTTP frame (e.g. a 15-byte
        MQTT CONNECT) is recognised on its first byte and never blocks waiting
        for HTTP-sized input.  This is the fix for the shared-port MQTT
        ``readuntil`` hang (decouple-connection-detection symptom #4).
        """
        max_len = max((b.detect_prefix_len for b in order), default=0)
        prefix = bytearray()
        at_eof = False
        while True:
            binding = self._select(bytes(prefix), at_eof, order)
            if binding is not None or at_eof or len(prefix) >= max_len:
                return bytes(prefix), binding
            chunk = await self._reader.read(max_len - len(prefix))
            if not chunk:
                at_eof = True
            else:
                prefix += chunk

    async def _peek(self, n: int) -> bytes:
        """Read up to *n* bytes (fewer on EOF) for an ALPN-committed binding."""
        buf = bytearray()
        while len(buf) < n:
            chunk = await self._reader.read(n - len(buf))
            if not chunk:
                break
            buf += chunk
        return bytes(buf)

    async def _dispatch(self) -> None:
        import asyncio  # noqa: PLC0415
        from ..env import get_settings as _get_settings  # noqa: PLC0415
        cfg = _get_settings()

        # Slowloris defence at protocol-detection: a connected peer that
        # never sends its discriminator prefix would otherwise hold a slot
        # forever waiting on the peek read.  We use the same ``header_timeout``
        # setting HTTP1Actor uses for its own header-completion deadline — the
        # practical worst case is two timeouts back-to-back (protocol-detect +
        # first request headers), still bounded.  ``header_timeout=0`` disables
        # both halves.
        deadline = cfg.header_timeout if cfg.header_timeout > 0 else None

        # Sprint 23: one rescheduled TimerHandle per connection replaces
        # the per-phase ``async with asyncio.timeout(d):`` allocations.
        # The deadline binds to *this* task — the per-connection dispatch
        # task — and gets passed down into HTTP1Actor / HTTP1Recipient so
        # each phase boundary (peek, headers, body chunk, keep-alive) just
        # re-arms the same handle.
        dl = ConnectionDeadline()

        # Port-bound non-ASGI protocol (Sprint 50): the listening socket already
        # identifies the protocol, so skip detection and the deadline machinery
        # entirely — the handler owns the connection lifetime and the raw reader.
        if self._bound_binding is not None:
            await self._bound_binding.serve(self._make_conn(self._reader, dl))
            return

        # Peek-and-replay detection (decouple-connection-detection, Stage 2):
        # ``ConnectionActor`` peeks only a protocol-agnostic discriminator — no
        # hardcoded byte counts, delimiters, or HTTP knowledge — and replays it
        # to the winning binding via a ``PrefixReader``.  Each binding then reads
        # its own framing (the 24-byte preface / the ``\r\n`` request line) from
        # a reader still positioned at the start of the stream.
        alpn_binding = self._registry.by_alpn(self._alpn)
        try:
            if alpn_binding is not None:
                # ALPN pre-commits the protocol; still peek its declared prefix
                # length under the deadline so a silent peer is timed out.
                prefix = await self._guarded(
                    dl, deadline, self._peek(alpn_binding.detect_prefix_len))
                binding = alpn_binding
            else:
                prefix, binding = await self._guarded(
                    dl, deadline, self._peek_and_select(self._detection_order()))
        except (asyncio.TimeoutError, TimeoutError):
            # Slowloris at detection: hand the "peer was too slow" decision to
            # the binding that would have served it — ALPN's committed binding,
            # else the cleartext catch-all (http1, which emits a 408).  The
            # status string lives in the binding, not here.
            timeout_binding = (alpn_binding if alpn_binding is not None
                               else self._fallback_binding())
            await self._on_detect_timeout(timeout_binding, deadline, dl)
            return

        if binding is None:
            # No binding claimed the prefix — only reachable if the registry has
            # no http1 fallback (it always does for HTTP listeners).  Close.
            return
        self._served_protocol = binding.name
        await binding.serve(self._make_conn(PrefixReader(prefix, self._reader), dl))

    @staticmethod
    async def _guarded(dl: ConnectionDeadline, deadline: 'float | None', coro):
        """Run *coro* under the connection deadline, if one is configured."""
        if deadline is not None:
            with dl.guard(deadline):
                return await coro
        return await coro

    def _fallback_binding(self) -> 'ProtocolBinding | None':
        """The cleartext catch-all (``http1``, registered last) — the binding a
        silent cleartext peer is treated as.  ``None`` if no cleartext bindings
        are registered."""
        cleartext = self._registry.cleartext_bindings
        return cleartext[-1] if cleartext else None

    async def _on_detect_timeout(self, binding: 'ProtocolBinding | None',
                                 deadline: 'float | None',
                                 dl: ConnectionDeadline) -> None:
        """Peer connected but never sent its discriminator within the deadline.

        Records the (protocol-agnostic) slowloris cap hit, then delegates the
        wire response to the binding's :meth:`~ProtocolBinding.on_detect_timeout`
        — HTTP writes a 408; other protocols close silently.  No status string
        lives here (decouple-connection-detection, Stage 3).
        """
        from .cap_log import log_cap_hit  # noqa: PLC0415
        # The deadline only fires under ``_guarded``, which arms a timer solely
        # when a deadline is configured — so it is non-None on this path.
        assert deadline is not None
        proto = binding.name if binding is not None else 'unknown'
        logger.warning('slowloris: %s peer sent no discriminator within %.1fs',
                       proto, deadline)
        log_cap_hit('header_timeout', requested=deadline, limit=deadline,
                    peer=self._peername, protocol=proto)
        if binding is not None:
            await binding.on_detect_timeout(self._make_conn(self._reader, dl))

    async def _handle(self, msg: Message) -> None:
        raise NotImplementedError

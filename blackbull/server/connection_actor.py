"""Connection Actor — owns one transport and spawns its protocol actor."""
import asyncio
import logging
from collections.abc import Awaitable, Callable

from ..actor import Actor, Message
from ..event_aggregator import EventAggregator
from .cap_log import _LazyCapHitCounter
from .deadline import ConnectionDeadline
from .protocol_registry import (ConnectionView, ProtocolBinding,
                                ProtocolRegistry)
from .recipient import (AbstractReader, PrefixReader,
                        _HTTP2_STREAM_QUEUE_DEPTH, _WS_READ_INLINE)
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
        ws_queue_depth: int = _WS_READ_INLINE,
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
        import time  # noqa: PLC0415
        # Per-connection cap-hit state, bound on the ambient contextvar so every
        # log_cap_hit() in this task tree picks it up without constructor
        # plumbing (TaskGroup children inherit the context).  Lazy: the real
        # counter and its os.urandom id are built only if a cap fires, which
        # keeps a getrandom(2) syscall, an allocation and a flush off every
        # accepted connection.  It reuses the accept-time id so a cap-hit record
        # correlates with the lifecycle events, and generates its own only for
        # the direct test drives that pass none.
        counter = (_LazyCapHitCounter(connection_id=self._connection_id)
                   if self._connection_id else _LazyCapHitCounter())
        start = time.monotonic()
        with counter.bind():
            if self._aggregator is not None:
                await self._aggregator.on_connection_accepted(
                    self._peername, protocol=self._served_protocol)
            try:
                await self._dispatch()
            except Exception as exc:
                # One error path for every protocol: a handler or actor that
                # raises is isolated here.
                if self._aggregator is not None:
                    await self._aggregator.on_error({}, exc)
            finally:
                # One summary record per suppressed cap before the
                # transport goes away.
                counter.flush(peer=self._peername)
                await self._writer.close()
                # Fires for *every* protocol.  ``_served_protocol`` is the
                # binding that handled the connection, or the accept-time guess
                # when none was selected.
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

    def _select(self, prefix: bytes, at_eof: bool,
                order: 'tuple[ProtocolBinding, ...]') -> 'ProtocolBinding | None':
        """First binding (in priority order) to claim *prefix*, or ``None`` if a
        higher-priority binding still needs more bytes to decide.

        A binding is only consulted once ``prefix`` holds at least its
        ``detect_prefix_len`` bytes (or the peer has closed): until then we must
        not let a lower-priority catch-all (``http1``) claim a connection the
        higher-priority protocol might still own.  A binding that can rule the
        bytes out cheaply (:meth:`ProtocolBinding.prefix_possible` returning
        False) is skipped instead, so the http1 catch-all claims a plain HTTP
        request on its first byte rather than after a full 16-byte peek.
        """
        for binding in order:
            if not at_eof and len(prefix) < binding.detect_prefix_len:
                if binding.prefix_possible(prefix):
                    return None
                continue   # this binding can never claim — try the next
            if binding.claims(prefix, self._alpn):
                return binding
        return None

    async def _peek_and_select(
        self, order: 'tuple[ProtocolBinding, ...]',
    ) -> 'ProtocolBinding | None':
        """Inspect the smallest discriminating prefix and return the binding.

        Grows the peek one step at a time up to ``max(detect_prefix_len)`` and
        stops the instant a binding claims — so a short non-HTTP frame (a
        15-byte MQTT CONNECT, say) is recognised on its first byte and never
        blocks waiting for HTTP-sized input.

        Nothing is consumed: the bytes stay in the reader, so the winning
        binding gets a stream still positioned at its own first byte and there
        is no prefix to replay.
        """
        max_len = max((b.detect_prefix_len for b in order), default=0)
        at_eof = False
        want = 1
        while True:
            prefix = self._reader.peek(max_len)
            binding = self._select(prefix, at_eof, order)
            if binding is not None or at_eof or len(prefix) >= max_len:
                return binding
            want = max(want + 1, len(prefix) + 1)
            if not await self._reader.fill(min(want, max_len)):
                at_eof = True

    async def _peek(self, n: int) -> None:
        """Buffer up to *n* bytes for an ALPN-committed binding, unconsumed.

        ALPN has already decided the protocol; this exists only so a peer that
        connects and then says nothing is timed out by the caller's deadline
        rather than holding a slot.
        """
        await self._reader.fill(n)

    async def _dispatch(self) -> None:
        from ..env import get_settings as _get_settings  # noqa: PLC0415
        cfg = _get_settings()

        # Slowloris defence at detection: a peer that connects and never sends
        # its discriminator would hold a slot forever on the peek read.  Shares
        # HTTP1Actor's ``header_timeout``, so the worst case is two bounded
        # timeouts back to back (detect + first headers); ``0`` disables both.
        deadline = cfg.header_timeout if cfg.header_timeout > 0 else None

        # Per-connection registry state, not a per-connection timer: one
        # process-wide scanner walks the registry (see ``deadline.py``).  Bound
        # to *this* dispatch task and passed down into HTTP1Actor /
        # HTTP1Recipient, so every phase boundary re-arms the same object.
        dl = ConnectionDeadline()

        # Port-bound non-ASGI protocol: the listening socket already
        # identifies the protocol, so skip detection and the deadline machinery
        # entirely — the handler owns the connection lifetime and the raw reader.
        if self._bound_binding is not None:
            await self._bound_binding.serve(self._make_conn(self._reader, dl))
            return

        # The discriminator is protocol-agnostic: no hardcoded byte counts, no
        # delimiters, no HTTP knowledge.  Each binding reads its own framing.
        alpn_binding = self._registry.by_alpn(self._alpn)
        try:
            if alpn_binding is not None:
                # ALPN pre-commits the protocol; still wait for its declared
                # prefix length under the deadline so a silent peer is timed out.
                await self._guarded(
                    dl, deadline, self._peek(alpn_binding.detect_prefix_len))
                binding = alpn_binding
            else:
                binding = await self._guarded(
                    dl, deadline,
                    self._peek_and_select(self._registry.detection_order))
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
        # Empty for a buffer-owning reader, which peeked without consuming, so
        # the binding gets that reader itself.  A reader that could only read
        # ahead hands back what it took and the replay wrapper restores the
        # stream — the indirection exists only where it is still needed.
        ahead = self._reader.take_ahead()
        reader = PrefixReader(ahead, self._reader) if ahead else self._reader
        await binding.serve(self._make_conn(reader, dl))

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
        wire response to the binding's
        :meth:`~ProtocolBinding.on_detect_timeout` — HTTP writes a 408, other
        protocols close silently.
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

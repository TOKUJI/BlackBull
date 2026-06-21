"""Unified protocol registry — Sprint 50.

BlackBull dispatches every accepted connection through a single
:class:`ProtocolRegistry`.  ``http1`` and ``http2`` are built-in *bindings*;
non-HTTP protocols (raw TCP, and later MQTT/Redis) register their own bindings
via :meth:`BlackBull.raw_handler` / :meth:`BlackBull.register_protocol_handler`.

A *binding* owns protocol selection and Actor construction; the deadline-guarded
byte I/O (the 24-byte HTTP/2 preface read, the first-line read, the slowloris
408 / GOAWAY handling) stays in :class:`~blackbull.server.connection_actor.ConnectionActor`
so the hot HTTP path is unchanged.

Two dispatch routes:

* **Detection** (the shared HTTP listener): ``ConnectionActor`` reads the first
  bytes and asks each :class:`ProtocolBinding` in turn — ALPN first, then the
  ordered cleartext chain (``http2`` preface, ``http1`` fallback).
* **Port-bound** (raw protocols): a binding registered with ``port=`` gets its
  own listening socket; connections there skip detection entirely.

Note:
    Do not export the internal classes from ``blackbull/__init__.py``; the
    public surface is :meth:`BlackBull.raw_handler` and
    :meth:`BlackBull.register_protocol_handler`.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..event_aggregator import EventAggregator
from .deadline import ConnectionDeadline
from .recipient import AbstractReader, _HTTP2_STREAM_QUEUE_DEPTH, _WS_EVENT_QUEUE_DEPTH
from .sender import AbstractWriter

logger = logging.getLogger(__name__)

_HTTP2_PREFACE_FIRST_LINE = b'PRI * HTTP/2.0\r\n'
_HTTP2_PREFACE_REMAINDER = b'\r\nSM\r\n\r\n'
_HTTP2_PREFACE = _HTTP2_PREFACE_FIRST_LINE + _HTTP2_PREFACE_REMAINDER


# ---------------------------------------------------------------------------
# Handler contract
# ---------------------------------------------------------------------------

RawProtocolHandler = Callable[
    ['AbstractReader', 'AbstractWriter', 'ProtocolContext'],
    Awaitable[None],
]
"""Async ``(reader, writer, ctx) -> None`` — owns one connection's lifetime."""


@dataclass
class ProtocolContext:
    """Context passed to a non-ASGI protocol handler.

    Carries connection metadata and the shared :class:`EventAggregator`
    without exposing any ASGI concept (no ``scope`` / ``receive`` / ``send``).
    """
    peername: tuple[str, int] | None
    sockname: tuple[str, int] | None
    ssl: bool
    aggregator: EventAggregator | None
    connection_id: str
    protocol: str
    # Protocol Actors may attach arbitrary state here before calling the handler.
    protocol_state: dict[str, Any] | None = None


@dataclass
class ConnectionView:
    """Everything a :class:`ProtocolBinding` needs to build its Actor.

    Assembled once per connection by :class:`ConnectionActor` and handed to the
    selected binding's ``serve_*`` method.  Keeps the binding API narrow and
    decouples bindings from ``ConnectionActor``'s internals.
    """
    reader: AbstractReader
    writer: AbstractWriter
    app: Callable[..., Awaitable[None]]
    aggregator: EventAggregator | None
    peername: tuple[str, int] | None
    sockname: tuple[str, int] | None
    ssl: bool
    alpn: str | None
    deadline: ConnectionDeadline
    connection_id: str
    stream_queue_depth: int = _HTTP2_STREAM_QUEUE_DEPTH
    ws_queue_depth: int = _WS_EVENT_QUEUE_DEPTH


# ---------------------------------------------------------------------------
# Detection (Sprint 51 — wired into ConnectionActor._dispatch())
# ---------------------------------------------------------------------------

class ProtocolDetector(ABC):
    """Inspects the first bytes of a connection to identify the protocol.

    Stateless — one instance is shared across all connections.  Used for
    first-byte sniffing on *shared* ports (e.g. MQTT + HTTP on one port);
    consulted by ``ConnectionActor._dispatch()`` after ALPN detection and
    before the http1 cleartext fallback.
    """

    @abstractmethod
    def detect(self, first_bytes: bytes, alpn: str | None) -> bool:
        """Return True if *first_bytes* matches this protocol."""
        ...

    @property
    @abstractmethod
    def protocol_name(self) -> str:
        """Human-readable protocol name for logging."""
        ...


# ---------------------------------------------------------------------------
# Bindings
# ---------------------------------------------------------------------------

class ProtocolBinding:
    """One connection-level protocol.

    Subclasses implement only the route(s) they support: ``serve_alpn`` for
    ALPN-negotiated protocols, ``matches_cleartext`` + ``serve_cleartext`` for
    cleartext-detected protocols, ``serve_raw`` for port-bound protocols.
    """

    name: str = ''
    alpn_token: str | None = None
    port: int | None = None

    def matches_cleartext(self, first_line: bytes) -> bool:
        """Return True if this binding owns a cleartext connection whose first
        line is *first_line*.  Default: no match."""
        return False

    async def serve_alpn(self, conn: ConnectionView, preface: bytes) -> None:
        raise NotImplementedError

    async def serve_cleartext(self, conn: ConnectionView, first_line: bytes) -> None:
        raise NotImplementedError

    async def serve_raw(self, conn: ConnectionView) -> None:
        raise NotImplementedError


class Http2Binding(ProtocolBinding):
    """HTTP/2 — ALPN ``h2`` or the cleartext connection preface (RFC 9113 §3.4)."""

    name = 'http2'
    alpn_token = 'h2'

    def matches_cleartext(self, first_line: bytes) -> bool:
        return first_line == _HTTP2_PREFACE_FIRST_LINE

    async def serve_alpn(self, conn: ConnectionView, preface: bytes) -> None:
        if preface != _HTTP2_PREFACE:
            # Best-effort GOAWAY(PROTOCOL_ERROR) before closing so a peer that
            # did implement HTTP/2 gets a clean diagnosis.  Frame it by hand —
            # the connection is doomed and we want to close before timing out.
            from ..protocol.frame_types import ErrorCodes  # noqa: PLC0415
            goaway = (b'\x00\x00\x08\x07\x00\x00\x00\x00\x00'
                      + b'\x00\x00\x00\x00'
                      + int(ErrorCodes.PROTOCOL_ERROR).to_bytes(4, 'big'))
            try:
                await conn.writer.write(goaway)
            except Exception:
                pass
            raise ValueError(f'Invalid HTTP/2 preface: {preface!r}')
        await self._run(conn)

    async def serve_cleartext(self, conn: ConnectionView, first_line: bytes) -> None:
        # ``readexactly`` (not ``read``) so a byte-by-byte peer still yields the
        # full 8-byte remainder (regression: test_connection_actor fragmented preface).
        remainder = await conn.reader.readexactly(8)
        if first_line + remainder != _HTTP2_PREFACE:
            raise ValueError(f'Invalid HTTP/2 preface continuation: {remainder!r}')
        await self._run(conn)

    async def _run(self, conn: ConnectionView) -> None:
        from .http2_actor import HTTP2Actor  # noqa: PLC0415
        actor = HTTP2Actor(
            conn.reader, conn.writer, conn.app, conn.aggregator,
            peername=conn.peername, sockname=conn.sockname, ssl=conn.ssl,
            stream_queue_depth=conn.stream_queue_depth,
        )
        await actor.run()


class Http1Binding(ProtocolBinding):
    """HTTP/1.1 — the cleartext fallback (matches any first line)."""

    name = 'http1'

    def matches_cleartext(self, first_line: bytes) -> bool:
        return True

    async def serve_cleartext(self, conn: ConnectionView, first_line: bytes) -> None:
        from .http1_actor import HTTP1Actor  # noqa: PLC0415
        actor = HTTP1Actor(
            conn.reader, conn.writer, conn.app, conn.aggregator,
            request=first_line,
            peername=conn.peername, sockname=conn.sockname, ssl=conn.ssl,
            ws_queue_depth=conn.ws_queue_depth,
            deadline=conn.deadline,
        )
        await actor.run()


class RawBinding(ProtocolBinding):
    """A user-registered non-ASGI protocol, bound to its own listening port."""

    def __init__(
        self,
        name: str,
        handler: RawProtocolHandler,
        *,
        detector: ProtocolDetector | None = None,
        port: int | None = None,
    ) -> None:
        self.name = name
        self.handler = handler
        self.detector = detector
        self.port = port

    async def serve_raw(self, conn: ConnectionView) -> None:
        from .raw_actor import RawProtocolActor  # noqa: PLC0415
        ctx = ProtocolContext(
            peername=conn.peername, sockname=conn.sockname, ssl=conn.ssl,
            aggregator=conn.aggregator, connection_id=conn.connection_id,
            protocol=self.name,
        )
        actor = RawProtocolActor(conn.reader, conn.writer, self.handler, ctx)
        await actor.run()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ProtocolRegistry:
    """Single source of truth for all connection-level protocols.

    Pre-populated with the built-in HTTP bindings; one instance per app.  The
    cleartext-detection order is fixed: ``http2`` (preface) before ``http1``
    (fallback) — ``http1`` must be last because it matches any first line.
    """

    def __init__(self) -> None:
        self._cleartext: list[ProtocolBinding] = [Http2Binding(), Http1Binding()]
        self._alpn: dict[str, ProtocolBinding] = {
            b.alpn_token: b for b in self._cleartext if b.alpn_token
        }
        self._ports: dict[str, RawBinding] = {}

    def register(
        self,
        name: str,
        handler: RawProtocolHandler,
        *,
        detector: ProtocolDetector | None = None,
        port: int | None = None,
    ) -> RawBinding:
        """Register a non-ASGI protocol handler.  Raises on duplicate name.

        A ``detector`` enables shared-port sniffing (see
        :class:`ProtocolDetector`).  When several registered detectors could
        match the same first bytes, dispatch picks the **first registered**
        one — ``raw_bindings`` preserves insertion order.
        """
        if name in self._ports or name in {b.name for b in self._cleartext}:
            raise ValueError(f'Protocol {name!r} already registered')
        binding = RawBinding(name, handler, detector=detector, port=port)
        self._ports[name] = binding
        return binding

    def by_alpn(self, token: str | None) -> ProtocolBinding | None:
        return self._alpn.get(token) if token else None

    @property
    def cleartext_bindings(self) -> list[ProtocolBinding]:
        """Ordered cleartext-detection chain (``http2`` then ``http1``)."""
        return self._cleartext

    @property
    def port_bindings(self) -> dict[int, RawBinding]:
        """Raw bindings that need their own listening socket, keyed by port."""
        return {b.port: b for b in self._ports.values() if b.port is not None}

    @property
    def raw_bindings(self) -> dict[str, RawBinding]:
        return dict(self._ports)

    @property
    def ports(self) -> list[int]:
        return [b.port for b in self._ports.values() if b.port is not None]

    def has_port_bindings(self) -> bool:
        return any(b.port is not None for b in self._ports.values())

    def __bool__(self) -> bool:
        """Truthy once a non-HTTP protocol is registered (HTTP is always present)."""
        return bool(self._ports)

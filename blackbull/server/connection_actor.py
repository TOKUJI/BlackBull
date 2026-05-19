"""Connection Actor — Phase 6 Step 5."""
import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..actor import Actor, Message
from ..event_aggregator import EventAggregator
from .recipient import (AbstractReader, IncompleteReadError,
                        _HTTP2_STREAM_QUEUE_DEPTH, _WS_EVENT_QUEUE_DEPTH)
from .sender import AbstractWriter

logger = logging.getLogger(__name__)

_HTTP2_PREFACE_FIRST_LINE = b'PRI * HTTP/2.0\r\n'
_HTTP2_PREFACE_REMAINDER  = b'\r\nSM\r\n\r\n'


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

    async def run(self) -> None:
        if self._aggregator is not None:
            await self._aggregator.on_connection_accepted(self._peername)
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._dispatch())
        except* Exception as eg:
            if self._aggregator is not None:
                await self._aggregator.on_error({}, eg)
        finally:
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

        # ALPN-negotiated HTTP/2: the peer is committed to HTTP/2, so the
        # first 24 bytes MUST be the connection preface (RFC 9113 §3.4).
        # Read exactly 24 bytes rather than scanning for CRLF so an invalid
        # preface is detected and rejected promptly instead of hanging the
        # reader (h2spec §3.5 #2 exercises this with a non-preface byte
        # sequence and times out otherwise).
        if self._alpn == 'h2':
            try:
                if deadline is not None:
                    async with asyncio.timeout(deadline):
                        preface = await self._reader.readexactly(24)
                else:
                    preface = await self._reader.readexactly(24)
            except (asyncio.TimeoutError, TimeoutError):
                # Peer connected via ALPN h2 but never sent the preface.
                # Close — no GOAWAY since we don't have a SETTINGS exchange.
                logger.warning('slowloris: ALPN-h2 peer sent no preface within %.1fs', deadline)
                return
            expected = _HTTP2_PREFACE_FIRST_LINE + _HTTP2_PREFACE_REMAINDER
            if preface != expected:
                # Best-effort: send GOAWAY before closing so a peer that did
                # implement HTTP/2 gets a clean diagnosis.  We do not bother
                # framing the goaway via the factory — the connection is
                # already doomed and we want to close before timing out.
                from ..protocol.frame_types import ErrorCodes  # noqa: PLC0415
                goaway = (b'\x00\x00\x08\x07\x00\x00\x00\x00\x00'
                          + b'\x00\x00\x00\x00'
                          + int(ErrorCodes.PROTOCOL_ERROR).to_bytes(4, 'big'))
                try:
                    await self._writer.write(goaway)
                except Exception:
                    pass
                raise ValueError(f'Invalid HTTP/2 preface: {preface!r}')
            from .http2_actor import HTTP2Actor  # noqa: PLC0415
            actor = HTTP2Actor(
                self._reader, self._writer, self._app, self._aggregator,
                peername=self._peername, sockname=self._sockname,
                ssl=self._ssl,
                stream_queue_depth=self._stream_queue_depth,
            )
            await actor.run()
            return

        # No ALPN (cleartext) or ALPN didn't pick h2 — sniff the first line.
        try:
            if deadline is not None:
                async with asyncio.timeout(deadline):
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
            try:
                await self._writer.write(
                    b'HTTP/1.1 408 Request Timeout\r\n'
                    b'connection: close\r\n'
                    b'content-length: 0\r\n\r\n')
            except Exception:
                pass
            return

        if first_line == _HTTP2_PREFACE_FIRST_LINE:
            remainder = await self._reader.readexactly(8)
            if first_line + remainder != _HTTP2_PREFACE_FIRST_LINE + _HTTP2_PREFACE_REMAINDER:
                raise ValueError(
                    f'Invalid HTTP/2 preface continuation: {remainder!r}')
            from .http2_actor import HTTP2Actor  # noqa: PLC0415
            actor = HTTP2Actor(
                self._reader, self._writer, self._app, self._aggregator,
                peername=self._peername, sockname=self._sockname,
                ssl=self._ssl,
                stream_queue_depth=self._stream_queue_depth,
            )
        else:
            from .http1_actor import HTTP1Actor  # noqa: PLC0415
            actor = HTTP1Actor(
                self._reader, self._writer, self._app, self._aggregator,
                request=first_line,
                peername=self._peername, sockname=self._sockname,
                ssl=self._ssl,
                ws_queue_depth=self._ws_queue_depth,
            )
        await actor.run()

    async def _handle(self, msg: Message) -> None:
        raise NotImplementedError

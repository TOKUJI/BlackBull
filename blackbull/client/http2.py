"""HTTP/2 client (RFC 7540).

``HTTP2Client`` opens a single TCP/TLS connection, sends the connection
preface and an initial SETTINGS frame, then drives request/response
exchanges over multiple concurrent streams.

The client is intended for **wire-level testing** of BlackBull's
``ASGIServer`` rather than as a feature-rich application client.
"""
import asyncio
import ssl as _ssl
from dataclasses import dataclass, field
from http import HTTPMethod, HTTPStatus
from typing import Iterable

from ..logger import get_logger_set
from ..protocol.frame import (DEFAULT_INITIAL_WINDOW_SIZE,
                              DataFrameFlags,
                              FrameBase,
                              FrameFactory,
                              FrameTypes,
                              HeaderFrameFlags,
                              PseudoHeaders)
from ..protocol.stream import Stream
from ..server.headers import Headers, HeaderList
from ..server.recipient import AbstractReader, AsyncioReader
from ..server.sender import AbstractWriter, AsyncioWriter, HTTP2Sender
from ..utils import HTTP2 as _HTTP2_PREFACE
from .exceptions import ConnectionError, ProtocolError, StreamReset
from .response import ResponderFactory

logger, _ = get_logger_set('client.http2')


# Number of bytes in the fixed HTTP/2 frame header (RFC 7540 §4.1).
_FRAME_HEADER_BYTES = 9


@dataclass
class ClientResponse:
    """A complete HTTP response received by the client.

    ``status`` is the HTTP status code (parsed from the ``:status`` pseudo-header).
    ``headers`` are the regular response headers as a ``Headers`` instance
    (bytes-keyed, lowercase-indexed).  ``body`` is the concatenation of all
    DATA-frame payloads received on the stream.
    """
    status: int
    headers: Headers
    body: bytes


@dataclass
class _PendingResponse:
    """In-flight response state, keyed by stream_id in ``HTTP2Client._responses``."""
    future: asyncio.Future
    status: int = 0
    headers: list[tuple[bytes, bytes]] = field(default_factory=list)
    body_parts: list[bytes] = field(default_factory=list)


class HTTP2Client:
    """Async HTTP/2 client.

    Use as an async context manager::

        async with HTTP2Client('localhost', 8000) as c:
            res = await c.request(HTTPMethod.GET, '/')

    ``ssl=None`` (the default) selects plaintext h2c.  Provide an
    ``ssl.SSLContext`` with ``set_alpn_protocols(['h2'])`` for h2 over TLS.

    Multiple ``request()`` calls share the same connection: each gets its own
    odd, monotonically-increasing client-initiated stream ID
    (RFC 7540 §5.1.1) and the responses are demultiplexed by the receive loop.
    """

    def __init__(self, host: str, port: int, *,
                 ssl: _ssl.SSLContext | None = None) -> None:
        self._host = host
        self._port = port
        self._ssl = ssl
        self._scheme = 'https' if ssl is not None else 'http'

        self._reader: AbstractReader | None = None
        self._writer: AbstractWriter | None = None
        self._raw_writer: asyncio.StreamWriter | None = None

        self._factory = FrameFactory()
        self._control_sender: HTTP2Sender | None = None
        self._senders: dict[int, HTTP2Sender] = {}

        # Client-initiated streams use odd IDs starting at 1 (RFC 7540 §5.1.1).
        self._next_stream_id = 1

        # Track sent streams in a tree so child senders can read from it.
        # The root is stream 0 (connection level).
        self._root_stream = Stream(0, None, 1)

        # In-flight responses keyed by stream_id.
        self._responses: dict[int, _PendingResponse] = {}

        # Receive loop task; created in __aenter__, cancelled in __aexit__.
        self._receive_task: asyncio.Task | None = None

        # Connection-level flow control state (mirrors HTTP2Sender's view).
        self.connection_window_size: int = DEFAULT_INITIAL_WINDOW_SIZE
        self.stream_window_size: dict[int, int] = {}
        self.initial_window_size: int = DEFAULT_INITIAL_WINDOW_SIZE

        # Set when the peer sends GOAWAY; subsequent request() calls raise.
        self._goaway_received: bool = False
        self._goaway_error_code: int = 0

    # ---- async context manager -------------------------------------------

    async def __aenter__(self) -> 'HTTP2Client':
        if self._raw_writer is None:
            r, w = await asyncio.open_connection(self._host, self._port, ssl=self._ssl)
            self._raw_writer = w
            self._reader = AsyncioReader(r)
            self._writer = AsyncioWriter(w)
        await self._start()
        return self

    @classmethod
    def _adopt(cls, host: str, port: int,
               reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
               *, ssl: _ssl.SSLContext | None = None) -> 'HTTP2Client':
        """Wrap an already-open ``(reader, writer)`` pair as an HTTP2Client.

        Used by ``Client`` (the ALPN dispatcher) to hand off a TLS-handshaken
        connection without re-opening the transport.  Call ``await self._start()``
        — or enter via ``async with`` — to send the connection preface.
        """
        c = cls(host, port, ssl=ssl)
        c._raw_writer = writer
        c._reader = AsyncioReader(reader)
        c._writer = AsyncioWriter(writer)
        return c

    async def _start(self) -> None:
        """Send connection preface + initial SETTINGS and start the receive loop.

        Idempotent — calling more than once is a no-op so ``Client`` can adopt
        a connection and then enter the inner client's ``async with`` cleanly.
        """
        if self._receive_task is not None:
            return
        assert self._raw_writer is not None and self._writer is not None

        # HTTP/2 connection preface (RFC 7540 §3.5).
        self._raw_writer.write(_HTTP2_PREFACE)
        await self._raw_writer.drain()

        # Initial SETTINGS frame (empty — server defaults are fine).
        self._control_sender = HTTP2Sender(self._writer, self._factory, 0)
        await self._control_sender(self._factory.settings())

        self._receive_task = asyncio.create_task(self._receive_loop())

    async def __aexit__(self, exc_type, exc, tb) -> None:
        # Cancel the receive loop first so it doesn't read past close.
        if self._receive_task is not None and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except (asyncio.CancelledError, Exception):
                pass

        # Fail any still-pending responses so awaiters don't hang.
        for pending in self._responses.values():
            if not pending.future.done():
                pending.future.set_exception(
                    ConnectionError('client connection closed'))
        self._responses.clear()

        if self._raw_writer is not None:
            try:
                self._raw_writer.close()
                await self._raw_writer.wait_closed()
            except Exception:
                pass

    # ---- public API ------------------------------------------------------

    async def request(self, method: str | HTTPMethod, path: str, *,
                      headers: HeaderList = (),
                      body: bytes = b'') -> ClientResponse:
        """Send one request and await the matching response.

        Adds ``:authority`` automatically from ``host:port``.  Header names
        and values may be ``str`` or ``bytes``; they are normalised to ASCII
        ``str`` for HPACK encoding.
        """
        if self._goaway_received:
            raise ConnectionError(
                f'connection closed by peer (GOAWAY error_code={self._goaway_error_code})')
        if self._writer is None:
            raise ConnectionError('client is not connected')

        stream_id = self._allocate_stream_id()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ClientResponse] = loop.create_future()
        self._responses[stream_id] = _PendingResponse(future=future)
        self.stream_window_size[stream_id] = self.initial_window_size

        # Build the HEADERS frame.  END_STREAM is set immediately when the
        # caller has no body to send; otherwise it goes on the trailing DATA.
        flags = int(HeaderFrameFlags.END_HEADERS)
        if not body:
            flags |= int(HeaderFrameFlags.END_STREAM)

        h_frame = self._factory.create(FrameTypes.HEADERS, flags, stream_id)
        h_frame.pseudo_headers[PseudoHeaders.METHOD] = str(method)
        h_frame.pseudo_headers[PseudoHeaders.PATH] = path
        h_frame.pseudo_headers[PseudoHeaders.SCHEME] = self._scheme
        h_frame.pseudo_headers[PseudoHeaders.AUTHORITY] = f'{self._host}:{self._port}'
        for name, value in headers:
            h_frame.headers.append((_to_str(name).lower(), _to_str(value)))

        sender = self._make_sender(stream_id)
        await sender(h_frame)

        if body:
            d_frame = self._factory.create(
                FrameTypes.DATA, DataFrameFlags.END_STREAM, stream_id, data=body,
            )
            await sender(d_frame)

        return await future

    async def send_raw_frame(self, frame: FrameBase) -> None:
        """Escape hatch: write a raw frame to the wire (negative-path tests)."""
        await self._send_raw_frame(frame)

    async def receive_raw_frame(self) -> FrameBase | None:
        """Escape hatch: read one raw frame; bypasses the receive loop.

        Only safe to call when the receive loop is not running (i.e. before
        ``__aenter__`` finishes or after the loop has been cancelled).
        """
        return await self._receive_frame()

    # ---- internal: senders, streams, frame I/O ---------------------------

    def _allocate_stream_id(self) -> int:
        sid = self._next_stream_id
        self._next_stream_id += 2
        return sid

    def _make_sender(self, stream_id: int) -> HTTP2Sender:
        if stream_id not in self._senders:
            assert self._writer is not None
            self._senders[stream_id] = HTTP2Sender(
                self._writer, self._factory, stream_id)
        return self._senders[stream_id]

    async def _send_raw_frame(self, frame: FrameBase) -> None:
        assert self._control_sender is not None
        await self._control_sender(frame)

    async def _receive_frame(self) -> FrameBase | None:
        assert self._reader is not None
        try:
            header = await self._reader.readexactly(_FRAME_HEADER_BYTES)
        except (asyncio.IncompleteReadError, EOFError):
            return None
        length = int.from_bytes(header[:3], 'big', signed=False)
        payload = await self._reader.readexactly(length) if length else b''
        return self._factory.load(header + payload)

    async def _receive_loop(self) -> None:
        try:
            while True:
                frame = await self._receive_frame()
                if frame is None:
                    break
                # Track every stream the peer touches so children-of-root
                # invariants in Stream stay consistent.
                if frame.stream_id != 0 and self._root_stream.find_child(frame.stream_id) is None:
                    self._root_stream.add_child(frame.stream_id)
                try:
                    await ResponderFactory.create(frame).respond(self)
                except Exception:
                    logger.exception('responder failed for frame %r', frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('receive loop crashed')
        finally:
            # Connection ended; fail any still-pending responses.
            for pending in self._responses.values():
                if not pending.future.done():
                    pending.future.set_exception(
                        ConnectionError('connection closed before response'))
            self._responses.clear()

    # ---- internal: callbacks invoked by Responders -----------------------

    def _on_response_headers(self, frame) -> None:
        pending = self._responses.get(frame.stream_id)
        if pending is None:
            logger.debug('HEADERS for unknown stream %d — dropping', frame.stream_id)
            return
        status_str = frame.pseudo_headers.get(PseudoHeaders.STATUS)
        if status_str is not None:
            try:
                pending.status = int(status_str)
            except (TypeError, ValueError):
                pending.future.set_exception(
                    ProtocolError(f'invalid :status pseudo-header: {status_str!r}'))
                self._responses.pop(frame.stream_id, None)
                return
        for name, value in frame.headers:
            pending.headers.append((_to_bytes(name), _to_bytes(value)))
        if frame.end_stream:
            self._complete(frame.stream_id)

    def _on_response_data(self, frame) -> None:
        pending = self._responses.get(frame.stream_id)
        if pending is None:
            logger.debug('DATA for unknown stream %d — dropping', frame.stream_id)
            return
        pending.body_parts.append(frame.payload)
        if frame.end_stream:
            self._complete(frame.stream_id)

    def _complete(self, stream_id: int) -> None:
        pending = self._responses.pop(stream_id, None)
        if pending is None or pending.future.done():
            return
        response = ClientResponse(
            status=pending.status or HTTPStatus.OK,
            headers=Headers(pending.headers),
            body=b''.join(pending.body_parts),
        )
        pending.future.set_result(response)

    def _on_window_update(self, frame) -> None:
        increment = frame.window_size
        if frame.stream_id == 0:
            self.connection_window_size += increment
            for sender in self._senders.values():
                sender.connection_window_size += increment
                sender._window_open.set()
        else:
            self.stream_window_size[frame.stream_id] = (
                self.stream_window_size.get(frame.stream_id, self.initial_window_size)
                + increment)
            sender = self._senders.get(frame.stream_id)
            if sender is not None:
                sender.window_update(increment)

    def _on_initial_window_size(self, value: int) -> None:
        self.initial_window_size = value
        for sender in self._senders.values():
            sender.apply_settings(value)

    def _on_goaway(self, frame) -> None:
        self._goaway_received = True
        # frame.stream_id on a GOAWAY frame carries last_stream_id (RFC 7540 §6.8)
        last_stream_id = frame.stream_id
        self._goaway_error_code = frame.error_code
        for sid, pending in list(self._responses.items()):
            if sid > last_stream_id and not pending.future.done():
                pending.future.set_exception(ConnectionError(
                    f'connection closed by peer (GOAWAY error_code={frame.error_code})'))
                self._responses.pop(sid, None)

    def _on_rst_stream(self, frame) -> None:
        pending = self._responses.pop(frame.stream_id, None)
        if pending is not None and not pending.future.done():
            pending.future.set_exception(
                StreamReset(frame.stream_id, int(frame.error_code)))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_str(value: str | bytes) -> str:
    return value.decode('ascii') if isinstance(value, (bytes, bytearray)) else value


def _to_bytes(value: str | bytes) -> bytes:
    return value.encode('ascii') if isinstance(value, str) else bytes(value)

import asyncio
from abc import ABC, abstractmethod
import logging

from .sender import WebSocketSender, WSOpcode
from .headers import Headers
from ..protocol.frame import FrameBase, Data

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reader abstraction — swap asyncio for trio/curio by implementing this ABC
# ---------------------------------------------------------------------------

class IncompleteReadError(EOFError):
    """Raised by AbstractReader when the peer closes the connection mid-read.

    Mirrors asyncio.IncompleteReadError but is not tied to asyncio, so
    handlers that depend on AbstractReader remain runtime-agnostic.
    """


class AbstractReader(ABC):
    """Protocol-agnostic async byte-source.

    Mirrors ``AbstractWriter`` on the receive side.  Implementations wrap a
    concrete transport so that ``BaseRecipient`` subclasses stay runtime-agnostic.
    """

    @abstractmethod
    async def read(self, n: int) -> bytes: ...

    @abstractmethod
    async def readuntil(self, sep: bytes) -> bytes: ...

    @abstractmethod
    async def readexactly(self, n: int) -> bytes: ...


class AsyncioReader(AbstractReader):
    """Adapts an asyncio-compatible stream to ``AbstractReader``.

    Accepts any object exposing ``read()``, ``readuntil()``, and
    ``readexactly()`` — the asyncio StreamReader API — so that test doubles
    such as ``MagicMock`` can be injected without ceremony.
    """

    def __init__(self, stream_reader):
        if not (hasattr(stream_reader, 'read') and hasattr(stream_reader, 'readuntil')):
            raise TypeError(
                f"AsyncioReader requires an object with read() and readuntil(), "
                f"got {type(stream_reader)}"
            )
        self._sr = stream_reader

    async def read(self, n: int) -> bytes:
        try:
            return await self._sr.read(n)
        except asyncio.IncompleteReadError as exc:
            raise IncompleteReadError(exc.partial) from exc

    async def readuntil(self, sep: bytes) -> bytes:
        try:
            return await self._sr.readuntil(sep)
        except asyncio.IncompleteReadError as exc:
            raise IncompleteReadError(exc.partial) from exc

    async def readexactly(self, n: int) -> bytes:
        return await self._sr.readexactly(n)


# ---------------------------------------------------------------------------
# Recipient hierarchy
# ---------------------------------------------------------------------------

class BaseRecipient(ABC):
    """Abstract base for ASGI-event receive callables.

    ``__call__`` returns an ASGI event dict appropriate to the protocol:
      - HTTP: ``{'type': 'http.request', 'body': ..., 'more_body': False}``
      - WebSocket: ``{'type': 'websocket.connect'}``,
                   ``{'type': 'websocket.receive', ...}``, or
                   ``{'type': 'websocket.disconnect', ...}``

    The actual byte transport is hidden behind ``AbstractReader`` so the
    recipient logic is decoupled from asyncio internals.
    """

    def __init__(self, reader: AbstractReader | None):
        self._reader = reader

    @abstractmethod
    async def __call__(self) -> dict: ...


class HTTP1Recipient(BaseRecipient):
    """Reads an HTTP/1.1 request body and emits a single ``http.request`` event.

    Body bytes are read lazily on the first ``__call__`` using the Content-Length
    or Transfer-Encoding header from ``scope``.  Subsequent calls return
    ``{'type': 'http.disconnect'}``.
    """

    def __init__(self, reader: AbstractReader, scope: dict):
        super().__init__(reader)
        self._scope = scope
        headers = scope['headers']
        if not isinstance(headers, Headers):
            headers = Headers(headers)
        te = headers.get(b'transfer-encoding', b'').strip().lower()
        cl = headers.get(b'content-length', b'')
        if te and te != b'chunked':
            raise NotImplementedError(
                f'Transfer-Encoding "{te.decode()}" is not supported.'
            )
        self._chunked = (te == b'chunked')
        self._content_length = int(cl) if cl else None
        self._done = False

    async def __call__(self) -> dict:
        if self._done:
            return {'type': 'http.disconnect'}

        try:
            if self._chunked:
                size_line = await self._reader.readuntil(b'\r\n')
                chunk_size = int(size_line.strip(), 16)
                if chunk_size == 0:
                    await self._reader.readuntil(b'\r\n')   # consume trailing CRLF
                    self._done = True
                    return {'type': 'http.request', 'body': b'', 'more_body': False}
                data = await self._reader.read(chunk_size)
                await self._reader.readuntil(b'\r\n')        # consume CRLF after chunk data
                return {'type': 'http.request', 'body': data, 'more_body': True}
            else:
                self._done = True
                body = await self._reader.read(self._content_length) if self._content_length else b''
                logger.debug('HTTP1Recipient body: %r', body)
                return {'type': 'http.request', 'body': body, 'more_body': False}

        except IncompleteReadError:
            self._done = True
            return {'type': 'http.disconnect'}


class HTTP2Recipient(BaseRecipient):
    """Delivers HTTP/2 DATA frames as ASGI ``http.request`` events.

    The server loop feeds frames via ``put_DATAFrame()`` (non-blocking).
    The ASGI app calls ``__call__()`` which suspends until an event is available,
    hiding the concurrency from both sides.
    """

    def __init__(self, frame: FrameBase | None = None):
        super().__init__(None)
        self._queue: asyncio.Queue = asyncio.Queue()
        if frame:
            self.put_DATAFrame(frame)

    def make_event(self, frame: FrameBase) -> dict:
        return {
            'type': 'http.request',
            'body': frame.payload,
            'more_body': False if frame.end_stream else True,
        }

    def put_DATAFrame(self, frame: Data) -> None:
        self._queue.put_nowait(self.make_event(frame))

    def put_event(self, event: dict) -> None:
        """Enqueue a pre-built event dict (e.g. empty-body for HEADERS+END_STREAM)."""
        self._queue.put_nowait(event)

    async def __call__(self) -> dict:
        return await self._queue.get()


class WebSocketRecipient(BaseRecipient):
    """Reads WebSocket frames and emits ASGI ``websocket.*`` events.

    First call returns ``{'type': 'websocket.connect'}``.  Subsequent calls
    read the next frame from the transport:
      - Text frame   → ``{'type': 'websocket.receive', 'text': ..., 'bytes': None}``
      - Binary frame → ``{'type': 'websocket.receive', 'text': None, 'bytes': ...}``
      - Close frame  → ``{'type': 'websocket.disconnect', 'code': 1000}``
      - Ping frame   → sends Pong immediately, then reads the next frame
      - Pong frame   → silently dropped, reads the next frame

    Ping/pong handling requires write access to the transport, so the raw
    writer is stored alongside the reader.
    """

    def __init__(self, reader: AbstractReader, writer):
        super().__init__(reader)
        self._writer = writer
        self._connect_sent = False

    async def __call__(self) -> dict:
        if not self._connect_sent:
            self._connect_sent = True
            return {'type': 'websocket.connect'}

        while True:
            opcode, masked, length = await WebSocketSender._read_opcode(self._reader)
            payload = await WebSocketSender._read_payload(self._reader, masked, length)

            if not masked:
                raise ValueError(
                    'Received unmasked frame from client, which is a protocol violation.'
                )

            match opcode:
                case WSOpcode.TEXT:
                    return {'type': 'websocket.receive',
                            'text': payload.decode('utf-8'), 'bytes': None}

                case WSOpcode.BINARY:
                    return {'type': 'websocket.receive', 'text': None, 'bytes': payload}

                case WSOpcode.CLOSE:
                    return {'type': 'websocket.disconnect', 'code': 1000}

                case WSOpcode.PING:
                    pong = WebSocketSender._encode_frame(payload, opcode=WSOpcode.PONG)
                    self._writer.write(pong)
                    await self._writer.drain()

                case WSOpcode.PONG:
                    pass  # unsolicited pong — silently drop

                case _:
                    logger.warning('WebSocketRecipient: unsupported opcode 0x%02x', opcode)
                    return {'type': 'websocket.receive', 'text': None, 'bytes': payload}


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

class RecipientFactory:
    """Creates the appropriate ``BaseRecipient`` for the given protocol.

    All methods that need a reader accept a raw asyncio-compatible stream reader
    and wrap it in ``AsyncioReader`` internally.
    """

    @staticmethod
    def http1(reader, scope: dict) -> HTTP1Recipient:
        if not isinstance(reader, AbstractReader):
            reader = AsyncioReader(reader)
        return HTTP1Recipient(reader, scope)

    @staticmethod
    def http2(frame: FrameBase | None = None) -> HTTP2Recipient:
        return HTTP2Recipient(frame)

    @staticmethod
    def websocket(reader, writer) -> WebSocketRecipient:
        if not isinstance(reader, AbstractReader):
            reader = AsyncioReader(reader)
        return WebSocketRecipient(reader, writer)

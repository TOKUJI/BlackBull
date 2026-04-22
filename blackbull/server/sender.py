import asyncio
from abc import ABC, abstractmethod
from enum import IntEnum
from http import HTTPStatus
import logging
from email.utils import formatdate

from ..protocol.frame import FrameTypes, HeaderFrameFlags, DataFrameFlags, SettingFrameFlags, FrameBase, PseudoHeaders
from .headers import Headers, HeaderList

class WSOpcode(IntEnum):
    CONTINUATION = 0x0
    TEXT         = 0x1
    BINARY       = 0x2
    CLOSE        = 0x8
    PING         = 0x9
    PONG         = 0xA

class WSFrameBits(IntEnum):
    FIN         = 0x80  # FIN bit in byte 0
    OPCODE_MASK = 0x0F  # opcode bits in byte 0
    MASK_BIT    = 0x80  # mask bit in byte 1
    LENGTH_MASK = 0x7F  # payload length bits in byte 1

logger = logging.getLogger(__name__)

_CRLF = b'\r\n'


# ---------------------------------------------------------------------------
# Writer abstraction — swap asyncio for trio/curio by implementing this ABC
# ---------------------------------------------------------------------------

class AbstractWriter(ABC):
    """Protocol-agnostic async byte-sink.

    ``write()`` is the single responsibility: deliver bytes and ensure they
    are flushed.  Backpressure, buffering, and draining are implementation
    details of each concrete subclass — callers never call ``drain()`` directly.

    Implementors wrap a concrete transport (asyncio.StreamWriter, trio
    MemorySendStream, curio socket, …).  ``BaseSender`` only depends on this
    interface, so switching the async runtime requires only a new subclass here.
    """

    @abstractmethod
    async def write(self, data: bytes) -> None:
        """Write *data* to the transport and ensure it is flushed."""
        ...


class AsyncioWriter(AbstractWriter):
    """Adapts an asyncio-compatible stream to ``AbstractWriter``.

    The constructor accepts any object that exposes ``write(bytes)`` (sync)
    and ``drain()`` (async) — the asyncio StreamWriter API — so that test
    doubles such as ``MagicMock`` can be injected without ceremony.

    ``drain()`` is called inside ``write()`` so the asyncio backpressure
    mechanism is handled transparently and ``BaseSender`` stays runtime-agnostic.
    """

    def __init__(self, stream_writer):
        if not (hasattr(stream_writer, 'write') and hasattr(stream_writer, 'drain')):
            raise TypeError(
                f"AsyncioWriter requires an object with write() and drain(), "
                f"got {type(stream_writer)}"
            )
        self._sw = stream_writer

    async def write(self, data: bytes) -> None:
        self._sw.write(data)
        await self._sw.drain()


# ---------------------------------------------------------------------------
# Sender hierarchy
# ---------------------------------------------------------------------------

class BaseSender(ABC):
    """Abstract base for ASGI-event → wire-format senders.

    ``__call__`` accepts either:
      - ``bytes`` body + optional ``status`` and ``headers``: the sender builds
        and sends the full protocol response (start + body) in one call.
      - A protocol-specific event dict: dispatched to the appropriate handler.

    The actual byte transport is hidden behind ``AbstractWriter`` so the sender
    logic is decoupled from asyncio internals.
    """

    def __init__(self, writer: AbstractWriter):
        self._writer = writer

    @abstractmethod
    async def __call__(self, body, status: HTTPStatus = HTTPStatus.OK, headers: list = []): ...

    async def _write(self, data: bytes):
        """Flush *data* through the writer."""
        await self._writer.write(data)


class HTTP1Sender(BaseSender):
    """Translates content or ASGI HTTP send events into HTTP/1.1 wire-format bytes.

    ``__call__`` accepts two forms:

    **High-level** (bytes body + status):
      ``await sender(body_bytes, HTTPStatus.OK, headers=[...])``
      Writes the status line, headers, blank line, and body in one call.

    **Low-level** (ASGI event dict, for internal/error-handler use):
      ``await sender({'type': 'http.response.start', ...})``
      ``await sender({'type': 'http.response.body', ...})``

    ``http.response.start`` is buffered until ``http.response.body`` arrives so
    that Content-Length can be injected when the app omits it.
    """

    def __init__(self, writer: AbstractWriter):
        super().__init__(writer)
        self._buffered_status: HTTPStatus | None = None
        self._buffered_headers: Headers | None = None

    async def __call__(self, body,
                       status: HTTPStatus = HTTPStatus.OK,
                       headers: HeaderList = []):
        match body:
            case bytes():
                h = headers if isinstance(headers, Headers) else Headers(headers)
                await self._flush(status, h, body)

            case {'type': 'http.response.start'}:
                self._buffered_status = HTTPStatus(body.get('status', HTTPStatus.OK))
                self._buffered_headers = Headers(list(body.get('headers', [])))

            case {'type': 'http.response.body'}:
                content = body.get('body', b'')
                more_body = body.get('more_body', False)
                if self._buffered_status is not None:
                    await self._flush(self._buffered_status, self._buffered_headers, content, more_body)
                    self._buffered_status = None
                    self._buffered_headers = None
                elif content:
                    logger.debug('HTTP1Sender body: %r', content)
                    await self._write(content)

            case {'type': 'http.response.trailers'}:
                await self._write(b'0\r\n')
                for name, value in body.get('headers', []):
                    await self._write(name + b': ' + value + b'\r\n')
                await self._write(b'\r\n')

            case {'type': str() as event_type}:
                logger.warning('HTTP1Sender: unknown event type %r', event_type)

            case _:
                raise TypeError(f'HTTP1Sender expected bytes or dict, got {type(body)!r}')

    async def _flush(self, status: HTTPStatus, headers: Headers, body: bytes, more_body: bool = False) -> None:
        if not more_body and b'content-length' not in headers:
            headers.append(b'content-length', str(len(body)).encode())

        if b'Date' not in headers:
            headers.append(b'Date', formatdate(timeval=None, localtime=False, usegmt=True).encode())

        await self._write_start(status, headers)
        if body:
            logger.debug('HTTP1Sender body: %r', body)
            await self._write(body)

    async def _write_start(self, status: HTTPStatus, headers: HeaderList):
        chunks: list[bytes] = []
        chunks.append(f'HTTP/1.1 {status} {status.phrase}'.encode() + _CRLF)
        for k, v in headers:
            k = k.encode() if isinstance(k, str) else k
            v = v.encode() if isinstance(v, str) else v
            chunks.append(k + b': ' + v + _CRLF)
        chunks.append(_CRLF)
        for chunk in chunks:
            logger.debug('HTTP1Sender: %r', chunk)
            await self._write(chunk)


class HTTP2Sender(BaseSender):
    """Translates content or ASGI HTTP send events into HTTP/2 frames.

    ``__call__`` accepts three forms:

    **High-level** (bytes body + status):
      ``await sender(body_bytes, HTTPStatus.OK, headers=[...])``
      Sends a HEADERS frame followed by a DATA frame.

    **Low-level** (ASGI event dict):
      ``await sender({'type': 'http.response.start', ...})``
      ``await sender({'type': 'http.response.body', ...})``

    **Control-plane** (raw FrameBase instance):
      ``await sender(settings_frame)``
      Serialises and writes the frame directly.
    """

    def __init__(self, writer: AbstractWriter, factory, stream_identifier: int):
        super().__init__(writer)
        self._factory = factory
        self._stream_identifier = stream_identifier
        self.connection_window_size = 65535  # initial connection-level window (RFC 7540 §6.9.2)
        self.stream_window_size = {stream_identifier: 65535}  # initial stream window
        self._window_open = asyncio.Event()
        self._window_open.set()  # window starts open

    async def _write(self, data: bytes):
        while (len(data) > self.connection_window_size or
               len(data) > self.stream_window_size[self._stream_identifier]):
            await self._window_open.wait()
            self._window_open.clear()
        await super()._write(data)
        self.connection_window_size -= len(data)
        self.stream_window_size[self._stream_identifier] -= len(data)

    def window_update(self, increment: int) -> None:
        self.connection_window_size += increment
        self.stream_window_size[self._stream_identifier] += increment
        self._window_open.set()  # wake any blocked _write()

    def apply_settings(self, initial_window_size: int) -> None:
        """Apply SETTINGS INITIAL_WINDOW_SIZE to the stream window."""
        self.stream_window_size[self._stream_identifier] = initial_window_size
        self._window_open.set()

    async def __call__(self, body, status: HTTPStatus = HTTPStatus.OK, headers: list = []):
        # Control-plane: raw frame object (SETTINGS, PING ACK, WINDOW_UPDATE, …)
        if isinstance(body, FrameBase):
            logger.debug('HTTP2Sender raw frame: %r', body)
            await self._write(body.save())
            return

        if isinstance(body, bytes):
            # High-level: build HEADERS + DATA frames from bytes + status
            h_frame = self._factory.create(
                FrameTypes.HEADERS,
                HeaderFrameFlags.END_HEADERS,
                self._stream_identifier,
            )
            h_frame.pseudo_headers[PseudoHeaders.STATUS] = str(status)
            for k, v in headers:
                h_frame.headers.append((k, v))
            await self._write(h_frame.save())

            d_frame = self._factory.create(
                FrameTypes.DATA,
                DataFrameFlags.END_STREAM,
                self._stream_identifier,
                data=body,
            )
            await self._write(d_frame.save())

        elif isinstance(body, dict):
            event_type = body.get('type', '')
            logger.debug('HTTP2Sender event: %r', event_type)

            if event_type == 'http.response.start':
                frame = self._factory.create(
                    FrameTypes.HEADERS,
                    HeaderFrameFlags.END_HEADERS,
                    self._stream_identifier,
                )
                frame.pseudo_headers[PseudoHeaders.STATUS] = str(body.get('status', 200))
                for k, v in body.get('headers', []):
                    frame.headers.append((k, v))
                await self._write(frame.save())

            elif event_type == 'http.response.body':
                flags = 0 if body.get('more_body', False) else DataFrameFlags.END_STREAM
                frame = self._factory.create(
                    FrameTypes.DATA,
                    flags,
                    self._stream_identifier,
                    data=body.get('body', b''),
                )
                await self._write(frame.save())

            else:
                logger.info('HTTP2Sender: unhandled event type %r', event_type)

        else:
            raise TypeError(f'HTTP2Sender expected bytes, dict, or FrameBase, got {type(body)!r}')


class WebSocketSender(BaseSender):
    """Translates ASGI websocket send events or WebSocketResponse dicts into
    WebSocket wire frames (RFC 6455).

    ``__call__`` accepts an ASGI event dict (as returned by ``WebSocketResponse``):
      - ``{'type': 'websocket.send', 'text': ...}``  → text frame (opcode 0x1)
      - ``{'type': 'websocket.send', 'bytes': ...}`` → binary frame (opcode 0x2)
      - ``{'type': 'websocket.close'}``              → close frame (opcode 0x8)
      - ``{'type': 'websocket.accept'}``             → no-op (handshake already sent)

    The ``status`` and ``headers`` parameters are accepted for interface
    consistency but are unused for WebSocket connections.
    """
    def __hash__(self):
        self.has_received_closed = False

    async def __call__(self, body, _status: HTTPStatus | None = None, _headers: HeaderList = []):
        if not isinstance(body, dict):
            raise TypeError(f'WebSocketSender expected a dict, got {type(body)!r}')

        event_type = body.get('type', '')

        match event_type:
                
            case 'websocket.send':
                if 'text' in body and body['text'] is not None:
                    frame = self._encode_frame(body['text'].encode('utf-8'), opcode=WSOpcode.TEXT)
                else:
                    frame = self._encode_frame(body.get('bytes', b''), opcode=WSOpcode.BINARY)
                await self._write(frame)

            case 'websocket.close':
                frame = self._encode_frame(b'\x03\xe8', opcode=WSOpcode.CLOSE)
                await self._write(frame)

            case 'websocket.accept':
                pass  # handshake reply is sent by WebSocketHandler.run()
            case _:
                logger.warning('WebSocketSender: unknown event type %r', event_type)

    @staticmethod
    def _encode_frame(payload: bytes, opcode: WSOpcode | int = WSOpcode.TEXT) -> bytes:
        """Encode *payload* as an unmasked WebSocket data frame (RFC 6455 §5).

        ``opcode`` defaults to ``WSOpcode.TEXT``; pass ``WSOpcode.BINARY`` for
        binary frames, ``WSOpcode.CLOSE`` for close frames, etc.
        The server MUST NOT mask frames it sends to the client (RFC 6455 §5.1).
        """
        length = len(payload)
        header = bytes([WSFrameBits.FIN | opcode])
        if length < 126:
            header += bytes([length])
        elif length < 65536:
            header += bytes([126]) + length.to_bytes(2, 'big')
        else:
            header += bytes([127]) + length.to_bytes(8, 'big')
        return header + payload
    
    @staticmethod
    async def _read_opcode(reader) -> tuple[int, bool, int]:
        """Read one WebSocket frame from *reader* and return its opcode.

        Raises ``asyncio.IncompleteReadError`` on EOF.
        """
        header = await reader.readexactly(2)
        opcode = header[0] & WSFrameBits.OPCODE_MASK
        masked = bool(header[1] & WSFrameBits.MASK_BIT)
        length = header[1] & WSFrameBits.LENGTH_MASK
        return opcode, masked, length
    
    @staticmethod
    async def _read_payload(reader, masked: bool, length: int) -> bytes:
        """Read the payload of a WebSocket frame from *reader*.

        If *masked* is True, also read the 4-byte mask and unmask the payload.
        Raises ``asyncio.IncompleteReadError`` on EOF.
        """
        if length == 126:
            length = int.from_bytes(await reader.readexactly(2), 'big')
        elif length == 127:
            length = int.from_bytes(await reader.readexactly(8), 'big')

        if masked:
            mask = await reader.readexactly(4)
            raw = await reader.readexactly(length)
            return bytes(b ^ mask[i % 4] for i, b in enumerate(raw))
        else:
            return await reader.readexactly(length)

    @staticmethod
    async def _read_frame(reader) -> tuple[int, bytes]:
        """Read one WebSocket frame from *reader*.

        Returns ``(opcode, payload)`` where *payload* is already unmasked.
        Raises ``asyncio.IncompleteReadError`` on EOF.
        """
        opcode, masked, length = await WebSocketSender._read_opcode(reader)
        payload = await WebSocketSender._read_payload(reader, masked, length)

        return opcode, payload


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

class SenderFactory:
    """Creates the appropriate BaseSender for the given protocol.

    All methods accept a raw asyncio-compatible stream writer and wrap it in
    ``AsyncioWriter`` internally.  To support a different async runtime,
    implement a new ``AbstractWriter`` subclass and pass it directly to the
    sender constructors instead.
    """

    @staticmethod
    def http1(stream_writer) -> HTTP1Sender:
        return HTTP1Sender(AsyncioWriter(stream_writer))

    @staticmethod
    def http2(stream_writer, factory, stream_identifier: int) -> HTTP2Sender:
        return HTTP2Sender(AsyncioWriter(stream_writer), factory, stream_identifier)

    @staticmethod
    def websocket(stream_writer) -> WebSocketSender:
        return WebSocketSender(AsyncioWriter(stream_writer))

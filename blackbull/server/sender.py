import asyncio
import os
from abc import ABC, abstractmethod
from enum import IntEnum
from http import HTTPStatus
from email.utils import formatdate
from typing import NamedTuple

from ..protocol.frame import (FrameTypes, HeaderFrameFlags, DataFrameFlags,
                               SettingFrameFlags, FrameBase, PseudoHeaders,
                               DEFAULT_INITIAL_WINDOW_SIZE)
import logging
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
    RSV1        = 0x40  # RSV1 bit in byte 0 (per-message deflate, RFC 7692)
    RSV2        = 0x20  # RSV2 bit in byte 0 (reserved)
    RSV3        = 0x10  # RSV3 bit in byte 0 (reserved)
    OPCODE_MASK = 0x0F  # opcode bits in byte 0
    MASK_BIT    = 0x80  # mask bit in byte 1
    LENGTH_MASK = 0x7F  # payload length bits in byte 1


class WSFrameHeader(NamedTuple):
    """Decoded fields from the two-byte WebSocket frame header (RFC 6455 §5.2).

    Note on masking (RFC 6455 §5.1):
    - Client → server frames MUST be masked (``masked`` is True);
      ``WebSocketRecipient`` raises ``ValueError`` on an unmasked client frame.
    - Server → client frames MUST NOT be masked; ``WebSocketSender`` never sets
      the mask bit.
    """
    opcode: int
    masked: bool
    length: int
    fin:    bool
    rsv1:   bool
    rsv2:   bool
    rsv3:   bool

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
    async def __call__(self, body, status: HTTPStatus = HTTPStatus.OK, headers: HeaderList = []): ...

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
        self._chunked: bool = False
        self._expect_trailers: bool = False

    async def __call__(self, body,
                       status: HTTPStatus = HTTPStatus.OK,
                       headers: HeaderList = []):
        """Dispatch on *body* and write the resulting HTTP/1.1 bytes.

        Accepted forms:

        - ``bytes`` — emit a complete response: status line, headers
          (with ``Content-Length`` injected if absent), blank line, body.
        - ``{'type': 'http.response.start', ...}`` — buffer the status,
          headers, and ``trailers`` flag; nothing is written yet.
        - ``{'type': 'http.response.body', ...}`` — on the first call after a
          buffered start, flush the start (adding ``Content-Length`` for
          single-body responses or ``Transfer-Encoding: chunked`` when
          ``more_body=True``); subsequent calls write chunk-framed body bytes
          and the terminal ``0\\r\\n\\r\\n`` when streaming completes.
        - ``{'type': 'http.response.trailers', ...}`` — write the terminal
          ``0\\r\\n`` followed by the trailer headers (chunked encoding).

        Unknown event types are logged and dropped; non-dict / non-bytes
        bodies raise ``TypeError``.
        """
        match body:
            case bytes():
                h = headers if isinstance(headers, Headers) else Headers(headers)
                await self._flush(status, h, body)

            case {'type': 'http.response.start'}:
                self._buffered_status = HTTPStatus(body.get('status', HTTPStatus.OK))
                self._buffered_headers = Headers(list(body.get('headers', [])))
                self._expect_trailers = bool(body.get('trailers', False))

            case {'type': 'http.response.body'}:
                content = body.get('body', b'')
                more_body = body.get('more_body', False)
                if self._buffered_status is not None:
                    assert self._buffered_headers is not None
                    await self._flush(self._buffered_status, self._buffered_headers, content, more_body)
                    self._buffered_status = None
                    self._buffered_headers = None
                else:
                    if self._chunked:
                        if content:
                            await self._write(f'{len(content):x}\r\n'.encode() + content + b'\r\n')
                        if not more_body and not self._expect_trailers:
                            await self._write(b'0\r\n\r\n')
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
        if more_body:
            if b'transfer-encoding' not in headers:
                headers.append(b'transfer-encoding', b'chunked')
            self._chunked = True
        elif b'content-length' not in headers:
            headers.append(b'content-length', str(len(body)).encode())

        if b'Date' not in headers:
            headers.append(b'Date', formatdate(timeval=None, localtime=False, usegmt=True).encode())

        await self._write_start(status, headers)

        if self._chunked:
            if body:
                await self._write(f'{len(body):x}\r\n'.encode() + body + b'\r\n')
            if not more_body and not self._expect_trailers:
                await self._write(b'0\r\n\r\n')
        elif body:
            logger.debug('HTTP1Sender body: %r', body)
            await self._write(body)

    async def _write_start(self, status: HTTPStatus, headers: HeaderList):
        chunks: list[bytes] = []
        chunks.append(f'HTTP/1.1 {status} {status.phrase}'.encode() + _CRLF)
        for k, v in headers:
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

    def __init__(self, writer: AbstractWriter, factory, stream_id: int,
                 push_callback=None):
        super().__init__(writer)
        self._factory = factory
        self._stream_id = stream_id
        self._push_callback = push_callback
        self.connection_window_size = DEFAULT_INITIAL_WINDOW_SIZE
        self.stream_window_size = {stream_id: DEFAULT_INITIAL_WINDOW_SIZE}
        self._window_open = asyncio.Event()
        self._window_open.set()  # window starts open

    async def _write(self, data: bytes):
        """Send *data* respecting HTTP/2 flow control (RFC 7540 §6.9).

        Blocks on ``self._window_open`` until both the connection-level and
        the stream-level windows have enough space; ``window_update()`` sets
        the event to wake any blocked writer.  Both windows are decremented
        by ``len(data)`` after the underlying transport write succeeds.
        """
        while (len(data) > self.connection_window_size or
               len(data) > self.stream_window_size[self._stream_id]):
            await self._window_open.wait()
            self._window_open.clear()
        await super()._write(data)
        self.connection_window_size -= len(data)
        self.stream_window_size[self._stream_id] -= len(data)

    def window_update(self, increment: int) -> None:
        self.connection_window_size += increment
        self.stream_window_size[self._stream_id] += increment
        self._window_open.set()  # wake any blocked _write()

    def apply_settings(self, initial_window_size: int) -> None:
        """Apply SETTINGS INITIAL_WINDOW_SIZE to the stream window."""
        self.stream_window_size[self._stream_id] = initial_window_size
        self._window_open.set()

    async def __call__(self, body, status: HTTPStatus = HTTPStatus.OK, headers: HeaderList = []):
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
                self._stream_id,
            )
            h_frame.pseudo_headers[PseudoHeaders.STATUS] = str(status)
            for k, v in headers:
                h_frame.headers.append((k, v))
            await self._write(h_frame.save())

            d_frame = self._factory.create(
                FrameTypes.DATA,
                DataFrameFlags.END_STREAM,
                self._stream_id,
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
                    self._stream_id,
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
                    self._stream_id,
                    data=body.get('body', b''),
                )
                await self._write(frame.save())

            elif event_type == 'http.response.push':
                if self._push_callback is not None:
                    await self._push_callback(body, self._stream_id)
                else:
                    logger.warning('http.response.push received but no push handler registered')

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
    def __init__(self, writer: AbstractWriter):
        super().__init__(writer)
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
    def _encode_frame(payload: bytes, opcode: WSOpcode | int = WSOpcode.TEXT,
                      *, mask: bool = False) -> bytes:
        """Encode *payload* as a WebSocket data frame (RFC 6455 §5).

        ``opcode`` defaults to ``WSOpcode.TEXT``; pass ``WSOpcode.BINARY`` for
        binary frames, ``WSOpcode.CLOSE`` for close frames, etc.

        Masking (RFC 6455 §5.1):
        - Server → client frames MUST NOT be masked: callers in the server
          path keep ``mask=False`` (the default).
        - Client → server frames MUST be masked: the protocol-layer client
          passes ``mask=True``, which prepends a random 4-byte masking key
          and XORs the payload with it.
        """
        length = len(payload)
        header = bytes([WSFrameBits.FIN | opcode])
        mask_bit = WSFrameBits.MASK_BIT if mask else 0
        if length < 126:
            header += bytes([mask_bit | length])
        elif length < 65536:
            header += bytes([mask_bit | 126]) + length.to_bytes(2, 'big')
        else:
            header += bytes([mask_bit | 127]) + length.to_bytes(8, 'big')
        if mask:
            mask_key = os.urandom(4)
            masked_payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
            return header + mask_key + masked_payload
        return header + payload
    
    @staticmethod
    async def _read_frame_header(reader) -> WSFrameHeader:
        """Read the two-byte WebSocket frame header (RFC 6455 §5.2).

        Returns a ``WSFrameHeader`` with all decoded flag and length fields.
        RSV1 signals per-message deflate (RFC 7692 §7); RSV2 and RSV3 are
        reserved for future extensions.
        Raises ``asyncio.IncompleteReadError`` on EOF.
        """
        header = await reader.readexactly(2)
        return WSFrameHeader(
            opcode = header[0] & WSFrameBits.OPCODE_MASK,
            masked = bool(header[1] & WSFrameBits.MASK_BIT),
            length = header[1] & WSFrameBits.LENGTH_MASK,
            fin    = bool(header[0] & WSFrameBits.FIN),
            rsv1   = bool(header[0] & WSFrameBits.RSV1),
            rsv2   = bool(header[0] & WSFrameBits.RSV2),
            rsv3   = bool(header[0] & WSFrameBits.RSV3),
        )
    
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
        h = await WebSocketSender._read_frame_header(reader)
        payload = await WebSocketSender._read_payload(reader, h.masked, h.length)
        return h.opcode, payload


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
        if isinstance(stream_writer, AbstractWriter):
            return HTTP1Sender(stream_writer)
        return HTTP1Sender(AsyncioWriter(stream_writer))

    @staticmethod
    def http2(stream_writer, factory, stream_id: int,
              push_callback=None) -> HTTP2Sender:
        return HTTP2Sender(AsyncioWriter(stream_writer), factory,
                           stream_id, push_callback)

    @staticmethod
    def websocket(stream_writer) -> WebSocketSender:
        if isinstance(stream_writer, AbstractWriter):
            return WebSocketSender(stream_writer)
        return WebSocketSender(AsyncioWriter(stream_writer))

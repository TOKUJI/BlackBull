"""HTTP/1.1 WebSocket client. Client frames are masked; incoming
server frames must not be. The returned session owns post-handshake reads.
"""
import asyncio
import os
import ssl as _ssl
from base64 import b64encode
from hashlib import sha1
from http import HTTPStatus
from typing import Iterable

from ..asgi import WebSocketDisconnectEvent, WebSocketReceiveEvent

import logging
from ..headers import Headers
from ..protocol.field_grammar import TCHAR_OCTETS, list_members
from ..server.recipient import (_WS_EVENT_QUEUE_DEPTH, AbstractReader,
                                WebSocketRecipient)
from ..server.sender import AbstractWriter, AsyncioWriter
from ..env import get_settings
from ..server.ws_codec import WSOpcode, encode_frame
from ._connect import (DEFAULT_CONNECT_TIMEOUT, ClientConnection,
                       open_connection as _open_connection)
from .exceptions import HandshakeError, ProtocolError
from .http1 import (HTTP1RequestSender, HTTP1ResponseHeadReader,
                    _response_length)

logger = logging.getLogger(__name__)


# RFC 6455 §1.3 — appended to the client's Sec-WebSocket-Key, hashed with
# SHA-1, and base64-encoded to form Sec-WebSocket-Accept.
_WEBSOCKET_GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

# RFC 6455 §4.1 — the only protocol version BlackBull's server accepts.
_WEBSOCKET_VERSION = b'13'

# Length in bytes of the random nonce used for Sec-WebSocket-Key.
_NONCE_BYTES = 16

# RFC 6455 §7.4.1 — normal closure status code.
_CLOSE_CODE_NORMAL = 1000


def _validate_negotiation(fields: Iterable[tuple[bytes, bytes]],
                          offered: Iterable[bytes] = (), *,
                          has_extensions: bool = False) -> bytes | None:
    chosen = None
    for name, value in fields:
        if name == b'sec-websocket-protocol':
            if (chosen is not None or not value
                    or value.translate(None, TCHAR_OCTETS)
                    or value not in offered):
                raise HandshakeError(
                    f'invalid or unoffered WebSocket subprotocol {value!r}')
            chosen = value
        elif name == b'sec-websocket-extensions':
            has_extensions = True
    if has_extensions:
        raise HandshakeError('server selected an unoffered WebSocket extension')
    return chosen


class WebSocketSession:
    """Frame-level WebSocket session over an established connection.

    Always masks outgoing frames (RFC 6455 §5.1).  Reads use
    ``WebSocketRecipient(require_masked=False)`` because servers MUST NOT
    mask their outgoing frames.
    """

    def __init__(self, reader: AbstractReader, writer: AbstractWriter,
                 raw_writer: ClientConnection,
                 subprotocol: bytes | None,
                 *, max_frame_payload: int | None = None,
                 max_message_size: int | None = None) -> None:
        settings = get_settings()
        if max_frame_payload is None:
            max_frame_payload = settings.client_ws_max_frame_payload
        if max_message_size is None:
            max_message_size = settings.client_ws_max_message_size
        self._reader = reader
        self._writer = writer
        self._raw_writer = raw_writer
        # Read-ahead stays on for clients.  The server default is inline
        # (no reader task) because a server handler drives receive() in a
        # tight loop; a client typically awaits an application response
        # between reads, and read-ahead is what answers a server PING in
        # that gap.
        self._recipient = WebSocketRecipient(reader,
                                             writer,
                                             require_masked=False,
                                             ws_queue_depth=_WS_EVENT_QUEUE_DEPTH,
                                             max_frame_payload=max_frame_payload,
                                             max_message_size=max_message_size,
                                             frame_cap_name=(
                                                 'client_ws_max_frame_payload'),
                                             message_cap_name=(
                                                 'client_ws_max_message_size'))
        # Skip the synthetic 'websocket.connect' first-call event — the
        # handshake (HTTP 101) already established the connection.
        self._recipient._connect_sent = True
        self.subprotocol = subprotocol
        self._closed = False
        # True once a 'websocket.disconnect' has been surfaced via
        # ``receive`` — close() then skips its drain loop (the closing
        # handshake already completed from our side's point of view).
        self._disconnect_seen = False

    # ---- public API ------------------------------------------------------

    async def send_text(self, text: str) -> None:
        """Send *text* as a UTF-8 text message."""
        await self._send_frame(text.encode('utf-8'), WSOpcode.TEXT)

    async def send_bytes(self, data: bytes) -> None:
        """Send *data* as a binary message."""
        await self._send_frame(data, WSOpcode.BINARY)

    async def ping(self, data: bytes = b'') -> None:
        """Send a PING carrying *data*; the peer's PONG arrives on the read path."""
        await self._send_frame(data, WSOpcode.PING)

    async def receive(self) -> WebSocketReceiveEvent | WebSocketDisconnectEvent:
        """Read one ASGI ``websocket.*`` event from the connection.

        Returns one of:
          - ``{'type': 'websocket.receive', 'text': str, 'bytes': None}``
          - ``{'type': 'websocket.receive', 'text': None, 'bytes': bytes}``
          - ``{'type': 'websocket.disconnect', 'code': int}``

        Server-initiated PING frames are auto-PONGed by the underlying
        ``WebSocketRecipient`` (with masking, since this session is the
        client).  Server PONG frames are silently dropped.
        """
        event = await self._recipient()
        if event.get('type') == 'websocket.disconnect':
            self._disconnect_seen = True
        return event

    async def close(self, code: int = _CLOSE_CODE_NORMAL, *,
                    drain_timeout: float = 5.0) -> None:
        """Send a CLOSE frame, await the peer's echoed CLOSE (bounded by
        *drain_timeout*), and stop the background reader task.

        RFC 6455 §7.1.2 — the closing handshake
        is complete once both endpoints have sent and received a Close
        frame.  A silent peer cannot hang ``close()``: after
        *drain_timeout* the reader is shut down regardless.  Data frames
        still in flight during the drain are discarded.  Idempotent.
        """
        if self._closed:
            return
        self._closed = True
        payload = code.to_bytes(2, 'big')
        try:
            await self._send_frame(payload, WSOpcode.CLOSE)
        except Exception:
            pass  # best-effort close frame; the socket may already be gone.
        try:
            if not self._disconnect_seen:
                async with asyncio.timeout(drain_timeout):
                    while True:
                        event = await self._recipient()
                        if event.get('type') == 'websocket.disconnect':
                            break
        except Exception:
            # TimeoutError (silent peer) or a recipient-raised protocol
            # error — either way the drain is over; shut the reader down.
            pass
        finally:
            await self._recipient.shutdown()

    # ---- internal --------------------------------------------------------

    async def _send_frame(self, payload: bytes, opcode: WSOpcode) -> None:
        frame = encode_frame(payload, opcode=opcode, mask=True)
        await self._writer.write(frame)

    # ---- async context manager (optional usage) --------------------------

    async def __aenter__(self) -> 'WebSocketSession':
        """Return the already-connected session; the handshake ran before this."""
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()


class WebSocketClient:
    """Async WebSocket client.

    Use as an async context manager::

        async with WebSocketClient('localhost', 8000) as c:
            ws = await c.connect('/path', subprotocols=[b'chat'])
            await ws.send_text('hello')
            msg = await ws.receive()
            await ws.close()

    The transport is held open between ``connect()`` and ``__aexit__``;
    only one concurrent session per ``WebSocketClient`` is supported.
    """

    def __init__(self, host: str, port: int, *,
                 ssl: _ssl.SSLContext | None = None,
                 connect_timeout: float | None = DEFAULT_CONNECT_TIMEOUT) -> None:
        self._host = host
        self._port = port
        self._ssl = ssl
        self._connect_timeout = connect_timeout
        self._reader: AbstractReader | None = None
        self._writer: AbstractWriter | None = None
        self._raw_writer: ClientConnection | None = None

    async def __aenter__(self) -> 'WebSocketClient':
        conn = await _open_connection(self._host, self._port, self._ssl,
                                      self._connect_timeout)
        self._raw_writer = conn
        self._reader = conn.reader
        self._writer = AsyncioWriter(
            conn, get_settings().client_write_timeout,
            cap_name='client_write_timeout', protocol='ws')
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        connection, self._raw_writer = self._raw_writer, None
        self._reader = self._writer = None
        if connection is not None:
            try:
                connection.close()
                await connection.wait_closed()
            except Exception:
                pass  # best-effort close on context exit; peer may already be gone.

    async def connect(self, path: str, *,
                      subprotocols: Iterable[bytes] = (),
                      response_timeout: float | None = 5.0) -> WebSocketSession:
        """Upgrade to WebSocket, closing the connection on failure.

        ``response_timeout=None`` disables the handshake response deadline.
        """
        if self._raw_writer is None:
            raise HandshakeError('WebSocket connect requires an open connection')
        try:
            assert self._writer is not None and self._reader is not None

            nonce = os.urandom(_NONCE_BYTES)
            key = b64encode(nonce)
            expected_accept = b64encode(sha1(key + _WEBSOCKET_GUID).digest())

            request_headers = Headers([
                (b'host', f'{self._host}:{self._port}'.encode()),
                (b'upgrade', b'websocket'),
                (b'connection', b'upgrade'),
                (b'sec-websocket-key', key),
                (b'sec-websocket-version', _WEBSOCKET_VERSION),
            ])
            offered = list(subprotocols)
            if offered:
                request_headers.append(b'sec-websocket-protocol', b', '.join(offered))

            await HTTP1RequestSender(self._writer).send('GET', path, request_headers)
            recipient = HTTP1ResponseHeadReader()
            if response_timeout is None:
                version, status, headers = await recipient.receive_head(self._reader)
            else:
                async with asyncio.timeout(response_timeout):
                    version, status, headers = await recipient.receive_head(self._reader)

            if (version != b'HTTP/1.1'
                    or status != HTTPStatus.SWITCHING_PROTOCOLS):
                raise HandshakeError(
                    f'server rejected WebSocket upgrade: status={status}')
            if (recipient._boundary_declared or b'transfer-encoding' in headers
                    or _response_length(headers)):
                raise HandshakeError('WebSocket upgrade declared an HTTP body')
            if list_members(headers.get_combined(b'upgrade') or b'') != [b'websocket']:
                raise HandshakeError('invalid WebSocket Upgrade header')
            if b'upgrade' not in recipient._connection_options(headers):
                raise HandshakeError('missing WebSocket Connection upgrade token')
            accepts = headers.getlist(b'sec-websocket-accept')
            if len(accepts) != 1 or accepts[0][1] != expected_accept:
                raise HandshakeError(
                    'invalid Sec-WebSocket-Accept')

            chosen = _validate_negotiation(
                headers.getlist(b'sec-websocket-protocol'), offered,
                has_extensions=b'sec-websocket-extensions' in headers)

            return WebSocketSession(self._reader, self._writer, self._raw_writer,
                                    subprotocol=chosen,
                                    max_frame_payload=(
                                        get_settings().client_ws_max_frame_payload),
                                    max_message_size=(
                                        get_settings().client_ws_max_message_size))
        except BaseException as exc:
            await self.__aexit__(None, None, None)
            if isinstance(exc, ProtocolError):
                raise HandshakeError('invalid WebSocket HTTP response') from exc
            raise

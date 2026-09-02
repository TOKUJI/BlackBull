"""WebSocket client (RFC 6455).

Two pieces:

- ``WebSocketClient`` opens a TCP/TLS connection and drives the HTTP/1.1
  ``Upgrade: websocket`` handshake.
- ``WebSocketSession``, returned by ``WebSocketClient.connect``, owns the
  post-handshake frame loop: ``send_text`` / ``send_bytes`` / ``receive`` /
  ``ping`` / ``close``.

Reuses ``HTTP1RequestSender`` / ``HTTP1ResponseRecipient`` for the handshake
and ``encode_frame(mask=True)`` / ``WebSocketRecipient(require_masked=False)``
for post-handshake frames — the same codec serves both directions,
parameterised by the ``mask`` flag.
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
from ..server.recipient import (_WS_EVENT_QUEUE_DEPTH, AbstractReader,
                                AsyncioReader, WebSocketRecipient)
from ..server.sender import AbstractWriter, AsyncioWriter
from ..server.ws_codec import WSOpcode, encode_frame
from ._connect import DEFAULT_CONNECT_TIMEOUT, open_connection as _open_connection
from .exceptions import HandshakeError
from .http1 import HTTP1RequestSender, HTTP1ResponseRecipient

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


class WebSocketSession:
    """Frame-level WebSocket session over an established connection.

    Always masks outgoing frames (RFC 6455 §5.1).  Reads use
    ``WebSocketRecipient(require_masked=False)`` because servers MUST NOT
    mask their outgoing frames.
    """

    def __init__(self, reader: AbstractReader, writer: AbstractWriter,
                 raw_writer: asyncio.StreamWriter,
                 subprotocol: bytes | None) -> None:
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
                                             ws_queue_depth=_WS_EVENT_QUEUE_DEPTH)
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
        await self._send_frame(text.encode('utf-8'), WSOpcode.TEXT)

    async def send_bytes(self, data: bytes) -> None:
        await self._send_frame(data, WSOpcode.BINARY)

    async def ping(self, data: bytes = b'') -> None:
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
        self._raw_writer: asyncio.StreamWriter | None = None

    async def __aenter__(self) -> 'WebSocketClient':
        r, w = await _open_connection(self._host, self._port, self._ssl,
                                      self._connect_timeout)
        self._raw_writer = w
        self._reader = AsyncioReader(r)
        self._writer = AsyncioWriter(w)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._raw_writer is not None:
            try:
                self._raw_writer.close()
                await self._raw_writer.wait_closed()
            except Exception:
                pass  # best-effort close on context exit; peer may already be gone.

    async def connect(self, path: str, *,
                      subprotocols: Iterable[bytes] = (),
                      response_timeout: float | None = 5.0) -> WebSocketSession:
        """Run the HTTP/1.1 ``Upgrade: websocket`` handshake on this connection.

        Returns a ``WebSocketSession`` once the server has confirmed the
        upgrade with HTTP 101 and a valid ``Sec-WebSocket-Accept`` header.
        Raises ``HandshakeError`` on any handshake-time failure, or
        ``TimeoutError`` if no response arrives within *response_timeout*.

        A peer can accept the connection and then never send the 101, so the
        transport-level deadline does not cover this wait.  ``None`` opts out.
        """
        assert self._writer is not None and self._reader is not None
        assert self._raw_writer is not None

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
        # What keeps the first WebSocket frame from being read as a body is
        # the 1xx half of RFC 9112 §6.3 item 1, which applies to the status
        # alone; passing GET changes no outcome, since the method half names
        # only HEAD and CONNECT.  It is passed because it is known and because
        # a call that omits it is indistinguishable from one that forgot.
        if response_timeout is None:
            response = await HTTP1ResponseRecipient().receive(
                self._reader, method='GET')
        else:
            async with asyncio.timeout(response_timeout):
                response = await HTTP1ResponseRecipient().receive(
                    self._reader, method='GET')

        if response.status != HTTPStatus.SWITCHING_PROTOCOLS:
            raise HandshakeError(
                f'server rejected WebSocket upgrade: status={response.status}')
        accept = response.headers.get(b'sec-websocket-accept')
        if accept != expected_accept:
            raise HandshakeError(
                f'invalid Sec-WebSocket-Accept: got {accept!r}, expected {expected_accept!r}')

        # Headers.get returns b'' when absent, not None — normalise to None
        # so callers can use ``ws.subprotocol is None`` as the no-protocol test.
        chosen_raw = response.headers.get(b'sec-websocket-protocol')
        chosen: bytes | None = chosen_raw if chosen_raw else None
        if chosen is not None and offered and chosen not in offered:
            raise HandshakeError(
                f'server selected subprotocol {chosen!r} which the client did not offer')

        return WebSocketSession(self._reader, self._writer, self._raw_writer,
                                subprotocol=chosen)

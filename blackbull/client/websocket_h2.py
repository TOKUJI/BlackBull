"""WebSocket-over-HTTP/2 client (RFC 8441).

Mirrors the HTTP/1.1 :class:`blackbull.client.WebSocketClient` /
:class:`blackbull.client.WebSocketSession` pair: the *Client* owns the
TLS + HTTP/2 transport and runs the Extended CONNECT handshake; the
*Session* owns the post-handshake WebSocket frame loop on one H2 stream.

Flow control: outgoing WS frames are dispatched as
``http.response.body`` events through the per-stream
:class:`blackbull.server.sender.HTTP2Sender`, which splits payloads
across multiple DATA frames at ``max_frame_size`` and respects send
windows.  Incoming DATA payloads are tracked per stream + at the
connection level; ``WINDOW_UPDATE`` frames are emitted when received
bytes accumulate past :data:`_WINDOW_UPDATE_THRESHOLD`.

Example::

    import ssl
    from blackbull.client import WebSocketH2Client

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_alpn_protocols(['h2'])

    async with WebSocketH2Client('localhost', 8443, ssl=ctx) as c:
        ws = await c.connect('/ws')
        await ws.send_bytes(b'hello')
        opcode, payload = await ws.receive()
        await ws.close()

The peer server must advertise ``SETTINGS_ENABLE_CONNECT_PROTOCOL=1``;
BlackBull's own server does so when ``BB_H2_ENABLE_WEBSOCKET=1``.

RFC 8441 is an experimental surface in BlackBull — both the server
gate and this client may change shape until the feature is declared
default-on.
"""
import asyncio
import logging
import ssl as _ssl
import struct

from ..asgi import ASGIEvent
from ..protocol.frame import FrameFactory
from ..protocol.frame_types import (
    DataFrameFlags, FrameTypes, HeaderFrameFlags, PseudoHeaders,
)
from ..server.http2_ws import HTTP2WSWriter
from ..server.recipient import (AbstractReader, IncompleteReadError,
                                WebSocketRecipient)
from ..server.ws_codec import WSOpcode, encode_frame
from .client import Client
from .exceptions import HandshakeError
from .http2 import HTTP2Client

logger = logging.getLogger(__name__)

# RFC 6455 §7.4.1 — normal closure status code.
_CLOSE_CODE_NORMAL = 1000

# Emit WINDOW_UPDATE when the receiver has buffered this many bytes
# without acknowledging them — half the RFC 9113 §6.9.2 default
# initial window so a peer streaming at line rate never stalls waiting
# for credit.
_WINDOW_UPDATE_THRESHOLD = 32768


class _H2QueueReader(AbstractReader):
    """``AbstractReader`` over a raw-stream DATA-frame queue.

    Sprint 72 (audit 1.20b / F.2) — lets the H2 client session reuse the
    shared ``ws_codec`` + ``WebSocketRecipient`` stack (whose read path
    calls only ``readexactly``) instead of a third, private WS frame
    parser.  DATA payloads are buffered as a byte stream; flow-control
    credit is returned through *credit_cb* the moment a frame is consumed
    off the queue — the same timing the old parser used.  END_STREAM or
    RST_STREAM marks EOF, which the recipient's read loop surfaces as an
    abnormal-closure ``websocket.disconnect`` (1006).
    """

    def __init__(self, queue: asyncio.Queue, credit_cb) -> None:
        self._queue = queue
        self._credit_cb = credit_cb
        self._buf = bytearray()
        self._eof = False

    async def _fill(self, n: int) -> None:
        while len(self._buf) < n and not self._eof:
            frame = await self._queue.get()
            ftype = frame.FrameType()
            if ftype == FrameTypes.DATA:
                if frame.payload:
                    self._buf.extend(frame.payload)
                    await self._credit_cb(len(frame.payload))
                if frame.end_stream:
                    self._eof = True
            elif ftype == FrameTypes.RST_STREAM:
                self._eof = True
            # Other frame types (WINDOW_UPDATE etc.) are connection-level
            # bookkeeping handled by the client's receive loop; ignore.

    async def readexactly(self, n: int) -> bytes:
        await self._fill(n)
        if len(self._buf) < n:
            raise IncompleteReadError()
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    async def read(self, n: int) -> bytes:
        await self._fill(1)
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    async def readuntil(self, sep: bytes) -> bytes:
        raise NotImplementedError('_H2QueueReader does not support readuntil')


class WebSocketH2Session:
    """Frame-level WebSocket session over one HTTP/2 stream.

    Outgoing frames are masked (RFC 6455 §5.1) and wrapped in H2 DATA
    frames.  Incoming DATA payloads feed the shared
    ``WebSocketRecipient`` stack through :class:`_H2QueueReader`
    (Sprint 72, audit 1.20b) — fragmentation reassembly, FIN/RSV/mask
    validation, UTF-8 checks, and auto-PONG all come from the same
    codec the server and the H1 client use.
    """

    def __init__(self, http2_client: HTTP2Client, factory: FrameFactory,
                 stream_id: int, frame_queue: asyncio.Queue) -> None:
        self._client = http2_client
        self._factory = factory
        self._stream_id = stream_id
        self._queue = frame_queue
        self._closed = False
        self._disconnect_seen = False
        # Per-stream HTTP2Sender so outgoing WS payloads larger than
        # ``max_frame_size`` are split into multiple DATA frames and
        # respect the peer's send window — same machinery the server
        # uses for response bodies.
        self._sender = http2_client._make_sender(stream_id)
        # Unacknowledged received bytes since the last WINDOW_UPDATE.
        # Tracked at both stream and connection levels per RFC 9113 §5.2.
        self._unacked_stream: int = 0
        self._unacked_conn: int = 0
        # Shared WS decode stack: DATA-frame queue → byte stream →
        # WebSocketRecipient.  ``require_masked=False`` because server
        # frames are unmasked — which also makes recipient-generated
        # PONG / echo-CLOSE frames masked, as a client's must be.
        # Recipient writes ride the per-stream sender via HTTP2WSWriter,
        # inheriting H2 flow control.
        self._recipient = WebSocketRecipient(
            _H2QueueReader(frame_queue, self._credit_returned),
            HTTP2WSWriter(self._sender),
            require_masked=False)
        # Skip the synthetic 'websocket.connect' first-call event — the
        # Extended CONNECT handshake already established the session.
        self._recipient._connect_sent = True

    async def send_text(self, text: str) -> None:
        await self._send_ws(text.encode('utf-8'), WSOpcode.TEXT)

    async def send_bytes(self, data: bytes) -> None:
        await self._send_ws(data, WSOpcode.BINARY)

    async def receive(self, timeout: float = 5.0) -> tuple[int, bytes]:
        """Return ``(opcode, payload)`` for the next complete WebSocket
        **message** on this stream (fragmented messages are reassembled;
        PING is answered transparently).

        Raises :class:`TimeoutError` if no message arrives within
        *timeout*.  Returned opcodes match
        :class:`blackbull.server.ws_codec.WSOpcode`; a peer CLOSE (or
        stream end) is surfaced as ``(WSOpcode.CLOSE, 2-byte code)``.
        """
        try:
            event = await asyncio.wait_for(self._recipient(), timeout)
        except asyncio.TimeoutError as e:
            raise TimeoutError(
                f'no WebSocket frame within {timeout}s on stream '
                f'{self._stream_id}') from e
        if event.get('type') == ASGIEvent.WS_RECEIVE:
            text = event.get('text')
            if text is not None:
                return WSOpcode.TEXT, text.encode('utf-8')
            return WSOpcode.BINARY, event.get('bytes') or b''
        # websocket.disconnect — peer CLOSE (echoed by the recipient) or
        # abnormal stream end (1006).
        self._disconnect_seen = True
        code = event.get('code', 1006)
        return WSOpcode.CLOSE, struct.pack('>H', code)

    async def close(self, code: int = _CLOSE_CODE_NORMAL, *,
                    drain_timeout: float = 5.0) -> None:
        """Send a WebSocket close frame (END_STREAM on the carrying DATA
        frame), await the peer's echoed CLOSE bounded by *drain_timeout*,
        and stop the recipient's reader task.  Idempotent.

        Sprint 72 (audit 1.20c) — same close discipline as the H1
        client's :meth:`WebSocketSession.close`.
        """
        if self._closed:
            return
        self._closed = True
        payload = struct.pack('>H', code)
        ws_bytes = encode_frame(payload, opcode=WSOpcode.CLOSE, mask=True)
        d_frame = self._factory.create(
            FrameTypes.DATA, DataFrameFlags.END_STREAM,
            self._stream_id, data=ws_bytes,
        )
        try:
            await self._client.send_raw_frame(d_frame)
        except Exception:
            pass  # best-effort close frame; the connection may be gone.
        try:
            if not self._disconnect_seen:
                async with asyncio.timeout(drain_timeout):
                    while True:
                        event = await self._recipient()
                        if event.get('type') == ASGIEvent.WS_DISCONNECT:
                            break
        except Exception:
            # TimeoutError (silent peer) or a recipient-raised protocol
            # error — either way the drain is over.
            pass
        finally:
            await self._recipient.shutdown()
            self._client.unregister_raw_stream(self._stream_id)

    async def _send_ws(self, payload: bytes, opcode: int) -> None:
        if self._closed:
            raise RuntimeError('session is closed')
        ws_bytes = encode_frame(payload, opcode=opcode, mask=True)
        # Route through the per-stream HTTP2Sender so payloads larger
        # than max_frame_size are split into multiple DATA frames and
        # send-side flow control is honoured.  ``more_body=True``
        # leaves END_STREAM unset so the WS session stays open.
        await self._sender({
            'type': ASGIEvent.HTTP_RESPONSE_BODY,
            'body': ws_bytes,
            'more_body': True,
        })

    async def _credit_returned(self, n: int) -> None:
        """Account for *n* bytes consumed off the receive buffer and
        emit ``WINDOW_UPDATE`` at stream + connection level when the
        accumulated credit crosses :data:`_WINDOW_UPDATE_THRESHOLD`.
        """
        self._unacked_stream += n
        self._unacked_conn += n
        if self._unacked_stream >= _WINDOW_UPDATE_THRESHOLD:
            await self._client.send_raw_frame(self._factory.window_update(
                self._stream_id, self._unacked_stream))
            self._unacked_stream = 0
        if self._unacked_conn >= _WINDOW_UPDATE_THRESHOLD:
            await self._client.send_raw_frame(self._factory.window_update(
                0, self._unacked_conn))
            self._unacked_conn = 0

    async def __aenter__(self) -> 'WebSocketH2Session':
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()


class WebSocketH2Client:
    """Async WebSocket-over-HTTP/2 client (RFC 8441).

    Owns the TLS + HTTP/2 connection; performs the Extended CONNECT
    handshake (``:method=CONNECT``, ``:protocol=websocket``) on
    :meth:`connect` and returns a :class:`WebSocketH2Session` for
    post-handshake frame I/O.

    The peer server must advertise ``SETTINGS_ENABLE_CONNECT_PROTOCOL=1``;
    BlackBull's server does so when ``BB_H2_ENABLE_WEBSOCKET=1``.
    """

    def __init__(self, host: str, port: int, *,
                 ssl: _ssl.SSLContext | None = None,
                 stream_id: int = 1) -> None:
        self._host = host
        self._port = port
        self._ssl = ssl
        self._stream_id = stream_id
        self._client: HTTP2Client | None = None
        self._factory = FrameFactory()
        self._connect_status: int | None = None

    async def __aenter__(self) -> 'WebSocketH2Client':
        self._client = await Client(
            self._host, self._port, ssl=self._ssl).__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            try:
                await self._client.__aexit__(None, None, None)
            finally:
                self._client = None

    @property
    def connect_status(self) -> int | None:
        """``:status`` from the last Extended CONNECT response, or ``None``
        if :meth:`connect` has not run yet."""
        return self._connect_status

    async def connect(self, path: str = '/',
                      *, response_timeout: float = 5.0) -> WebSocketH2Session:
        """Run the RFC 8441 Extended CONNECT handshake on this connection.

        Returns a :class:`WebSocketH2Session` bound to the new H2 stream.
        Raises :class:`HandshakeError` on a non-200 ``:status`` response
        or :class:`TimeoutError` if no response arrives within
        *response_timeout*.
        """
        if self._client is None:
            raise RuntimeError(
                'use as async context manager: '
                '`async with WebSocketH2Client(...) as c:`')

        queue = self._client.register_raw_stream(self._stream_id)

        h_frame = self._factory.create(
            FrameTypes.HEADERS,
            HeaderFrameFlags.END_HEADERS,
            self._stream_id,
        )
        h_frame.pseudo_headers[PseudoHeaders.METHOD] = 'CONNECT'
        h_frame.pseudo_headers[PseudoHeaders.PROTOCOL] = 'websocket'
        h_frame.pseudo_headers[PseudoHeaders.SCHEME] = (
            'https' if self._ssl else 'http')
        h_frame.pseudo_headers[PseudoHeaders.PATH] = path
        h_frame.pseudo_headers[PseudoHeaders.AUTHORITY] = (
            f'{self._host}:{self._port}')

        sender = self._client._make_sender(self._stream_id)
        await sender(h_frame)

        try:
            frame = await asyncio.wait_for(queue.get(), response_timeout)
        except asyncio.TimeoutError as e:
            self._client.unregister_raw_stream(self._stream_id)
            raise TimeoutError(
                f'no CONNECT response within {response_timeout}s') from e

        if frame.FrameType() != FrameTypes.HEADERS:
            self._client.unregister_raw_stream(self._stream_id)
            raise HandshakeError(
                f'expected HEADERS response for Extended CONNECT, '
                f'got frame type {frame.FrameType()!r}')

        status = frame.pseudo_headers.get(PseudoHeaders.STATUS, '')
        try:
            self._connect_status = int(status)
        except (TypeError, ValueError):
            self._client.unregister_raw_stream(self._stream_id)
            raise HandshakeError(
                f'invalid :status pseudo-header in CONNECT response: '
                f'{status!r}')
        if self._connect_status != 200:
            self._client.unregister_raw_stream(self._stream_id)
            raise HandshakeError(
                f'server rejected WebSocket-over-H2 CONNECT: '
                f':status={self._connect_status}')

        return WebSocketH2Session(
            self._client, self._factory, self._stream_id, queue)

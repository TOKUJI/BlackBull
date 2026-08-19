"""HTTP/2 client (RFC 7540).

``HTTP2Client`` opens a single TCP/TLS connection, sends the connection
preface and an initial SETTINGS frame, then drives request/response
exchanges over multiple concurrent streams.

The client is intended for **wire-level testing** of BlackBull's
``ASGIServer`` rather than as a feature-rich application client.
"""
import asyncio
import ssl as _ssl
from collections.abc import Iterable
from dataclasses import dataclass, field
from http import HTTPMethod, HTTPStatus

import logging
from ..protocol.frame import FrameFactory
from ..protocol.frame_types import (DEFAULT_INITIAL_WINDOW_SIZE,
                                    FrameBase, FrameTypes,
                                    HeaderFrameFlags, PseudoHeaders)
from ..protocol.stream import Stream
from ..headers import Headers
from ..server.recipient import AbstractReader, AsyncioReader
from ..server.sender import AbstractWriter, AsyncioWriter, HTTP2Sender
from ..utils import HTTP2 as _HTTP2_PREFACE
from ._connect import DEFAULT_CONNECT_TIMEOUT, open_connection as _open_connection
from .exceptions import ConnectionError, ProtocolError, StreamReset
# Module-level and at runtime, as ``http1.py`` imports its own scenario
# types: the annotation on ``execute_scenario`` has to be resolvable from
# this module, and a TYPE_CHECKING import is not (beartype looks in the
# module namespace at call time, where the name would not exist).
from ..fault_injection.scenario_h2_client import (
    Abort as _ScAbort,
    CLIENT_PREFACE as _SC_PREFACE,
    ReadResponse as _ScReadResponse,
    ScenarioH2Client,
    ScenarioH2ClientResult,
    SendBytes as _ScSendBytes,
    SendFrame as _ScSendFrame,
    SendPreface as _ScSendPreface,
    Sleep as _ScSleep,
    encode_frame as _sc_encode_frame,
)
from .response import ResponderFactory

logger = logging.getLogger(__name__)


# Number of bytes in the fixed HTTP/2 frame header (RFC 7540 §4.1).
_FRAME_HEADER_BYTES = 9

# Emit WINDOW_UPDATE once this many received-but-unacked DATA bytes have
# accumulated (stream- and connection-level, tracked separately).  Mirrors
# ``WebSocketH2Session._credit_returned``: batching avoids a WINDOW_UPDATE
# per DATA frame while still reopening the peer's send window long before
# the 65535-byte initial window is exhausted (RFC 9113 §6.9).
#: Seconds to wait for the remainder of a frame whose 9-byte header has
#: already arrived.  Not a bound on waiting for the next frame — see
#: ``HTTP2Client._receive_frame``.
_FRAME_READ_TIMEOUT: float = 30.0

_WINDOW_UPDATE_THRESHOLD = 32768


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
    # Stream-level received-but-unacked DATA bytes (see _credit_received).
    unacked: int = 0


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
                 ssl: _ssl.SSLContext | None = None,
                 connect_timeout: float | None = DEFAULT_CONNECT_TIMEOUT) -> None:
        self._host = host
        self._port = port
        self._ssl = ssl
        self._connect_timeout = connect_timeout
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

        # Streams that bypass ResponderFactory dispatch — used by the
        # RFC 8441 WebSocket-over-H2 client.  When a frame arrives on a
        # stream id in this dict, it is pushed into the queue instead of
        # being routed through the request/response state machine.
        self._raw_streams: dict[int, asyncio.Queue] = {}

        # Receive loop task; created in __aenter__, cancelled in __aexit__.
        self._receive_task: asyncio.Task | None = None

        # Connection-level flow control state (mirrors HTTP2Sender's view).
        self.connection_window_size: int = DEFAULT_INITIAL_WINDOW_SIZE
        self.stream_window_size: dict[int, int] = {}
        self.initial_window_size: int = DEFAULT_INITIAL_WINDOW_SIZE

        # Connection-level received-but-unacked DATA bytes (see
        # _credit_received).  Stream-level credit is tracked per
        # _PendingResponse.
        self._unacked_conn: int = 0

        # Set when the peer sends GOAWAY; subsequent request() calls raise.
        self._goaway_received: bool = False
        # Set when the receive loop ends for any reason.  ``_goaway_received``
        # only covers the polite departure; a peer that simply vanishes leaves
        # no frame behind, and without this ``request()`` awaited a future
        # nobody was left to resolve.  ``HTTP1Client`` raises here, so the two
        # clients used to disagree about the same event.
        self._connection_lost: bool = False
        # Bounds the *rest* of a frame the peer has already begun — never
        # the gap between frames.  See ``_receive_frame``.
        self._frame_read_timeout: float = _FRAME_READ_TIMEOUT
        self._goaway_error_code: int = 0

    # ---- async context manager -------------------------------------------

    async def __aenter__(self) -> 'HTTP2Client':
        if self._raw_writer is None:
            r, w = await _open_connection(self._host, self._port, self._ssl,
                                          self._connect_timeout)
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
                pass  # teardown: the receive task was just cancelled; ignore its unwind.

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
                pass  # best-effort close; the connection may already be gone.

    # ---- public API ------------------------------------------------------

    async def request(self, method: str | HTTPMethod, path: str, *,
                      headers: Iterable[tuple[str | bytes, str | bytes]] = (),
                      body: bytes = b'') -> ClientResponse:
        """Send one request and await the matching response.

        Adds ``:authority`` automatically from ``host:port``.  Header names
        and values may be ``str`` or ``bytes``; they are normalised to ASCII
        ``str`` for HPACK encoding.
        """
        if self._goaway_received:
            raise ConnectionError(
                f'connection closed by peer (GOAWAY error_code={self._goaway_error_code})')
        if self._connection_lost:
            raise ConnectionError('connection closed by peer')
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
        try:
            await sender(h_frame)

            if body:
            # Use the sender's flow-controlled DATA path rather than a single
            # raw DATA frame: it splits the body across SETTINGS_MAX_FRAME_SIZE
            # chunks and blocks on flow-control credit, so bodies larger than
            # one frame (e.g. >16 KiB gRPC messages) are sent correctly.
            # WINDOW_UPDATE frames are routed to this sender in
            # ``_on_window_update``, keeping its send window in sync.
                await sender._write_data(body, end_stream=True)
        except BaseException:
            # The future is only resolvable by the receive loop, and the
            # receive loop only knows about streams the peer answered.  A
            # send that never reached the wire has no answer coming, so its
            # entry would sit in ``_responses`` until GOAWAY or __aexit__ —
            # a dict that grows once per failed send.
            self._responses.pop(stream_id, None)
            raise

        return await future

    async def execute_scenario(
        self, scenario: ScenarioH2Client,
    ) -> ScenarioH2ClientResult:
        """Walk ``scenario.steps`` against the connected socket.

        The HTTP/2 counterpart of
        :meth:`blackbull.client.http1.HTTP1Client.execute_scenario`, and
        deliberately the same shape: it **never raises**, folding every
        outcome — a frame read, a timeout, a transport failure, a
        hard-abort — into the returned result so callers categorise without
        a try/except per scenario.

        Step dispatch:
          * ``SendPreface``  → the RFC 9113 §3.4 preface bytes
          * ``SendFrame``    → one frame, assembled here rather than by the
            production sender (which is what lets a scenario declare a
            length its payload does not match)
          * ``SendBytes``    → arbitrary bytes, optionally one at a time
          * ``Sleep``        → :func:`asyncio.sleep`
          * ``ReadResponse`` → one frame, or a recorded timeout
          * ``Abort``        → ``transport.abort()``; walks no further steps

        This lives on the client rather than in ``fault_injection`` because
        its twin does: a scenario executor needs the connection, and the
        client is what owns one.
        """
        import time as _time  # noqa: PLC0415

        assert self._writer is not None, 'connect via __aenter__ first'
        result = ScenarioH2ClientResult()
        t0 = _time.monotonic()
        try:
            for step in scenario.steps:
                if isinstance(step, _ScSendPreface):
                    await self._writer.write(_SC_PREFACE)
                elif isinstance(step, _ScSendFrame):
                    await self._writer.write(_sc_encode_frame(step))
                elif isinstance(step, _ScSendBytes):
                    await self._write_paced(step.data, step.byte_interval)
                elif isinstance(step, _ScSleep):
                    await asyncio.sleep(step.duration)
                elif isinstance(step, _ScReadResponse):
                    try:
                        result.response = await asyncio.wait_for(
                            self._receive_frame(), timeout=step.timeout)
                    except (asyncio.TimeoutError, TimeoutError) as exc:
                        result.timed_out = True
                        result.exception = repr(exc)
                        return result
                elif isinstance(step, _ScAbort):
                    # Hard-close: RST rather than FIN.  Subsequent socket
                    # I/O would raise, so short-circuit like the twin does.
                    transport = getattr(self._writer, 'transport', None)
                    if transport is not None:
                        transport.abort()
                    result.aborted = True
                    return result
                else:
                    raise TypeError(f'unknown step type: {type(step).__name__}')
                result.steps_completed += 1
        except Exception as exc:  # noqa: BLE001
            result.exception = repr(exc)
        finally:
            result.elapsed_s = _time.monotonic() - t0
        return result

    async def _write_paced(self, data: bytes, byte_interval: float) -> None:
        """Write *data*, optionally one byte at a time.

        A trickle is not a slow write of the whole buffer: each byte has to
        reach the wire before the pause, or the peer sees one burst after
        the total delay and the scenario tests nothing.
        """
        if byte_interval <= 0:
            await self._writer.write(data)
            return
        for i in range(len(data)):
            await self._writer.write(data[i:i + 1])
            await asyncio.sleep(byte_interval)

    async def send_raw_frame(self, frame: FrameBase) -> None:
        """Escape hatch: write a raw frame to the wire (negative-path tests)."""
        await self._send_raw_frame(frame)

    async def receive_raw_frame(self) -> FrameBase | None:
        """Escape hatch: read one raw frame, bypassing the receive loop's dispatch.

        For negative-path / fault-injection tests and raw-frame clients that
        need a peer frame ``_receive_loop`` would otherwise route through the
        normal dispatcher — the read-side twin of :meth:`send_raw_frame`.

        Only safe to call when the receive loop is not running (i.e. before
        ``__aenter__`` finishes or after the loop has been cancelled); a
        concurrent loop would race this call for the reader.
        """
        return await self._receive_frame()

    def register_raw_stream(self, stream_id: int) -> asyncio.Queue:
        """Mark *stream_id* as a raw-frame stream.

        Frames arriving on this stream are pushed into the returned
        ``asyncio.Queue`` instead of being routed through the
        request/response state machine.  Used by
        :class:`blackbull.client.WebSocketH2Client` to receive
        WebSocket frames (carried in DATA frames after RFC 8441
        Extended CONNECT) without racing the receive loop.

        Returning a fresh queue each call is intentional — registering
        the same stream twice would be a programming error.
        """
        if stream_id in self._raw_streams:
            raise ValueError(f'stream {stream_id} already registered as raw')
        q: asyncio.Queue = asyncio.Queue()
        self._raw_streams[stream_id] = q
        self.stream_window_size[stream_id] = self.initial_window_size
        return q

    def unregister_raw_stream(self, stream_id: int) -> None:
        """Stop routing frames for *stream_id* into its raw-frame queue."""
        self._raw_streams.pop(stream_id, None)

    # ---- internal: senders, streams, frame I/O ---------------------------

    def _allocate_stream_id(self) -> int:
        sid = self._next_stream_id
        self._next_stream_id += 2
        return sid

    def _make_sender(self, stream_id: int) -> HTTP2Sender:
        if stream_id not in self._senders:
            assert self._writer is not None
            # Seed the per-stream send window from the server's
            # announced SETTINGS_INITIAL_WINDOW_SIZE: a sender created after
            # the SETTINGS exchange must not start at the RFC default
            # (``_on_initial_window_size`` only delta-adjusts *existing*
            # senders).  Same construction-time seeding as the server's
            # ``make_sender`` (refactor 2.11).
            self._senders[stream_id] = HTTP2Sender(
                self._writer, self._factory, stream_id,
                initial_window=self.initial_window_size)
        return self._senders[stream_id]

    async def _send_raw_frame(self, frame: FrameBase) -> None:
        assert self._control_sender is not None
        await self._control_sender(frame)

    async def _receive_frame(self) -> FrameBase | None:
        """Read one frame, or ``None`` when the peer is finished with us.

        The wait for a frame to *begin* is deliberately unbounded: an
        HTTP/2 client that stops listening after a quiet interval breaks
        server streaming and long-polling, both of which are the peer
        behaving correctly.

        The wait for a frame to *finish* is bounded.  Once nine header
        bytes have arrived the peer has committed to a payload length, so
        a peer that sends the header and stops is not idle — it has
        abandoned a frame mid-delivery.  Without a bound that parks every
        pending future for the life of the process.  This is the
        client-side twin of the server's ``BB_HEADER_TIMEOUT``, and it
        ends the read the same way EOF does: ``None``, which the receive
        loop already treats as the connection being over.
        """
        assert self._reader is not None
        try:
            header = await self._reader.readexactly(_FRAME_HEADER_BYTES)
        except (asyncio.IncompleteReadError, EOFError):
            return None
        length = int.from_bytes(header[:3], 'big', signed=False)
        if not length:
            return self._factory.load(header)
        timeout = self._frame_read_timeout
        try:
            if timeout and timeout > 0:
                async with asyncio.timeout(timeout):
                    payload = await self._reader.readexactly(length)
            else:
                payload = await self._reader.readexactly(length)
        except (asyncio.IncompleteReadError, EOFError):
            return None
        except TimeoutError:
            logger.warning(
                'HTTP/2 peer began a %d-byte frame and stopped for %.1fs — '
                'treating the connection as gone', length, timeout)
            return None
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
                # Raw-frame streams (WebSocket-over-H2, etc.) bypass the
                # request/response dispatcher — the registrant drains
                # the queue itself.  Connection-level frames
                # (WINDOW_UPDATE keeps send-side flow control alive,
                # SETTINGS is connection-wide) must still go through the
                # normal handler so the per-stream sender wakes when
                # the peer credits its window.
                raw_q = self._raw_streams.get(frame.stream_id)
                if raw_q is not None and frame.FrameType() not in (
                        FrameTypes.WINDOW_UPDATE, FrameTypes.SETTINGS,
                ):
                    raw_q.put_nowait(frame)
                    continue
                try:
                    await ResponderFactory.create(frame).respond(self)
                except Exception:
                    logger.exception('responder failed for frame %r', frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('receive loop crashed')
        finally:
            # Record it before failing anyone: a request issued after this
            # point must be refused rather than parked on a future with no
            # remaining resolver.
            self._connection_lost = True
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

    async def _on_response_data(self, frame) -> None:
        pending = self._responses.get(frame.stream_id)
        if pending is None:
            logger.debug('DATA for unknown stream %d — dropping', frame.stream_id)
            # The *payload* is dropped; the credit is not.  RFC 9113 §6.9
            # makes the connection window shared by every stream, so bytes
            # that arrive for a stream we no longer track still consumed it.
            # Returning early without crediting leaks that window by every
            # such frame, and once it reaches zero every stream's body
            # stalls in the peer's writer.  A stream that closed while its
            # DATA was in flight is ordinary, not hostile.
            await self._credit_connection(len(frame.payload))
            return
        payload = frame.payload
        pending.body_parts.append(payload)
        # RFC 9113 §6.9 — the receiver MUST return flow-control credit for
        # consumed DATA via WINDOW_UPDATE.  Without this the server's send
        # window drains and ``HTTP2Sender._write_data`` blocks once a
        # response exceeds the 65535-byte initial window — a deadlock that
        # affects any large HTTP/2 body, not just gRPC.
        if payload:
            await self._credit_received(
                frame.stream_id, pending, len(payload),
                end_stream=bool(frame.end_stream))
        if frame.end_stream:
            self._complete(frame.stream_id)

    async def _credit_received(self, stream_id: int, pending: '_PendingResponse',
                               n: int, *, end_stream: bool) -> None:
        """Return *n* bytes of flow-control credit for received DATA.

        Accumulates per-stream and per-connection and emits WINDOW_UPDATE
        once either crosses :data:`_WINDOW_UPDATE_THRESHOLD`.  Stream-level
        credit is skipped on the final DATA frame (the stream is about to
        close, so the peer no longer needs it); connection-level credit is
        always returned because the connection window is shared across
        every stream.
        """
        pending.unacked += n
        if not end_stream and pending.unacked >= _WINDOW_UPDATE_THRESHOLD:
            await self._send_raw_frame(
                self._factory.window_update(stream_id, pending.unacked))
            pending.unacked = 0
        await self._credit_connection(n)

    async def _credit_connection(self, n: int) -> None:
        """Return *n* bytes of connection-level credit.

        Split out from :meth:`_credit_received` because it is owed for every
        DATA octet that arrived, including octets for a stream this client
        no longer tracks — that path has no per-stream state to accumulate
        against but consumed the shared window all the same.
        """
        if n <= 0:
            return
        self._unacked_conn += n
        if self._unacked_conn >= _WINDOW_UPDATE_THRESHOLD:
            await self._send_raw_frame(
                self._factory.window_update(0, self._unacked_conn))
            self._unacked_conn = 0

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
                sender.wake_window()
        else:
            self.stream_window_size[frame.stream_id] = (
                self.stream_window_size.get(frame.stream_id, self.initial_window_size)
                + increment)
            sender = self._senders.get(frame.stream_id)
            if sender is not None:
                sender.window_update(increment)

    def _on_initial_window_size(self, value: int) -> None:
        # RFC 9113 §6.9.2 — when SETTINGS_INITIAL_WINDOW_SIZE changes,
        # adjust every active stream's send-window by the delta rather than
        # overwriting it (overwriting loses bytes already sent / received).
        delta = value - self.initial_window_size
        self.initial_window_size = value
        if delta != 0:
            for sender in self._senders.values():
                sender.adjust_initial_window(delta)

    def _on_goaway(self, frame) -> None:
        self._goaway_received = True
        # RFC 9113 §6.8 — a GOAWAY's own stream identifier MUST be 0; the
        # Last-Stream-ID is the first four *payload* bytes, which ``GoAway``
        # parses into its own field.  Reading the header field instead made
        # ``sid > last_stream_id`` true for every stream, so a graceful
        # shutdown failed the responses the peer had just promised it *had*
        # processed — the opposite of what GOAWAY communicates.
        last_stream_id = frame.last_stream_id
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

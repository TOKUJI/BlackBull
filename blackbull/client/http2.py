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

from hpack import HPACKError, OversizedHeaderListError

import logging
from ..env import get_settings
from ..protocol.frame import FrameFactory, _UnknownFrame
from ..protocol.frame_types import (DEFAULT_INITIAL_WINDOW_SIZE, ErrorCodes,
                                    FrameBase, FrameFormatError, FrameTypes,
                                    HeaderFrameFlags, PseudoHeaders)
from ..headers import Headers
from ..server.cap_log import log_cap_hit
from ..server.recipient import AbstractReader, AsyncioReader
from ..server.sender import (AbstractWriter, AsyncioWriter, ConnectionWindow,
                             HTTP2Sender)
from ..utils import HTTP2 as _HTTP2_PREFACE
from ._connect import DEFAULT_CONNECT_TIMEOUT, open_connection as _open_connection
from .exceptions import (ConnectionError, ProtocolError, ResponseTooLarge,
                         StreamReset)
# Module-level and at runtime, as ``http1.py`` imports its own scenario
# types: the annotation on ``execute_scenario`` has to be resolvable from
# this module, and a TYPE_CHECKING import is not (beartype looks in the
# module namespace at call time, where the name would not exist).
from ..fault_injection._transport import half_close as _sc_half_close
from ..fault_injection.scenario_h2 import frame_matches as _sc_frame_matches
from ..fault_injection.scenario_h2_client import (
    Abort as _ScAbort,
    CLIENT_PREFACE as _SC_PREFACE,
    ReadResponse as _ScReadResponse,
    ScenarioH2Client,
    ScenarioH2ClientResult,
    SendRawBytes as _ScSendBytes,
    SendFrame as _ScSendFrame,
    ExpectServerFrame as _ScExpectServerFrame,
    HalfClose as _ScHalfClose,
    WaitForServerFrame as _ScWaitForServerFrame,
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

#: RFC 9113 §6.10.
_OPENS_A_FIELD_BLOCK = frozenset({FrameTypes.HEADERS, FrameTypes.PUSH_PROMISE})

_InboundFrame = FrameBase | _UnknownFrame


class _ConnectionFailed(Exception):
    """The receive loop ended the connection on purpose, with its reason.

    An exception that merely escapes a handler is logged as a crash instead.
    """


_WINDOW_UPDATE_THRESHOLD = 32768

#: Largest value SETTINGS_MAX_HEADER_LIST_SIZE can express (RFC 9113 §6.5.2,
#: a 32-bit field).  What "disabled" installs, hpack having no unlimited.
_HEADER_LIST_SIZE_UNBOUNDED = 0xFFFFFFFF


def _header_list_size() -> tuple[int | None, int]:
    """``(advertised, enforced)`` for the decoded field section.

    Returned as a pair from one read so the announcement and the decoder's
    limit cannot drift apart; §6.5.2 makes the announcement advisory, which
    is exactly why the two must be the same number.
    """
    limit = get_settings().client_h2_max_header_list_size
    if not limit:
        return None, _HEADER_LIST_SIZE_UNBOUNDED
    return limit, limit


def _flow_controlled_length(frame) -> int:
    """Octets *frame* charged against the connection window.

    RFC 9113 §6.9.1: DATA only, and the whole payload — ``frame.length``
    rather than ``len(frame.payload)``, which has had the padding stripped.
    """
    return frame.length if frame.FrameType() == FrameTypes.DATA else 0


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
    #: Response-body octets accepted so far, against BB_CLIENT_BODY_MAX_TOTAL.
    #: The flow-control window cannot serve as this: ``_credit_received``
    #: returns credit for every DATA frame, so the window bounds what is in
    #: flight and never what has accumulated.
    body_seen: int = 0
    #: Field-line octets accepted across *every* HEADERS frame on this stream,
    #: against BB_CLIENT_HEAD_MAX_TOTAL.  One field section is hpack's to bound
    #: (``max_header_list_size``); the sum over informational responses, the
    #: final headers and trailers is nobody's until here.
    headers_seen: int = 0
    #: Progress timer for this stream (BB_CLIENT_BODY_TIMEOUT), re-armed by
    #: the final response head and by every DATA frame that delivers payload.
    #: Per stream and not per connection: a connection-wide clock is reset by
    #: any peer traffic, so a busy stream shelters a stalled one indefinitely.
    deadline: asyncio.TimerHandle | None = None
    #: The request body still going up, if any.  A refusal has to cancel it:
    #: ``request()`` awaits the upload *before* it awaits this future, and
    #: RST_STREAM is exactly what makes the peer stop crediting the stream
    #: window — so a refusal mid-upload parked the caller forever on a window
    #: that would never reopen.
    upload: asyncio.Future | None = None
    #: When the response began, for the diagnostic on a stalled stream.
    opened_at: float = 0.0

    def disarm(self) -> None:
        """Stop the progress timer.  Idempotent — the response may end
        because it completed, because it was refused, or because the
        connection did."""
        if self.deadline is not None:
            self.deadline.cancel()
            self.deadline = None


def _record_frame(result, frame) -> None:
    """Log one frame: newest in ``response``, all of them in ``received``.

    ``response`` keeps its old meaning so existing scenarios are
    untouched.  The twin of ``http1._record_response`` — the two roles
    report the same things under the same names.
    """
    result.response = frame
    result.received.append(frame)
    result.server_bytes_received += 9 + int(getattr(frame, 'length', 0) or 0)


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
                 connect_timeout: float | None = DEFAULT_CONNECT_TIMEOUT,
                 scenario_mode: bool = False) -> None:
        self._scenario_mode = scenario_mode
        self._host = host
        self._port = port
        self._ssl = ssl
        self._connect_timeout = connect_timeout
        self._scheme = 'https' if ssl is not None else 'http'

        self._reader: AbstractReader | None = None
        self._writer: AbstractWriter | None = None
        self._raw_writer: asyncio.StreamWriter | None = None

        # One setting, both effects: the number we advertise is the number
        # the decoder refuses at, because they are read together here.  The
        # enforced half is kept because the refusal has to name it.
        (self._advertised_header_list_size,
         self._enforced_header_list_size) = _header_list_size()
        self._factory = FrameFactory(
            max_header_list_size=self._enforced_header_list_size)
        self._push_permitted: bool = get_settings().client_h2_enable_push
        self._control_sender: HTTP2Sender | None = None
        #: Detached refusal tasks, held so neither the GC nor __aexit__
        #: leaves one running against a closed transport.
        self._detached: set[asyncio.Future] = set()
        self._senders: dict[int, HTTP2Sender] = {}

        # Client-initiated streams use odd IDs starting at 1 (RFC 7540 §5.1.1).
        self._next_stream_id = 1

        # In-flight responses keyed by stream_id.
        self._responses: dict[int, _PendingResponse] = {}

        # Streams that bypass ResponderFactory dispatch — used by the
        # RFC 8441 WebSocket-over-H2 client.  When a frame arrives on a
        # stream id in this dict, it is pushed into the queue instead of
        # being routed through the request/response state machine.
        self._raw_streams: dict[int, asyncio.Queue] = {}

        # Receive loop task; created in __aenter__, cancelled in __aexit__.
        self._receive_task: asyncio.Task | None = None

        # The peer's announced SETTINGS_INITIAL_WINDOW_SIZE.  Seeded into
        # each sender at construction; the per-stream windows live there,
        # because the sender is what waits on them.
        self.initial_window_size: int = DEFAULT_INITIAL_WINDOW_SIZE

        # The connection-level send window (RFC 9113 §6.9.1) is one budget
        # every stream debits, so it is owned here and handed to each sender
        # rather than allocated per sender.  Held apart from the senders
        # because it outlives them: a stream that finishes sending stops
        # needing credit, but the credit it spent is still gone.
        self._conn_window = ConnectionWindow(DEFAULT_INITIAL_WINDOW_SIZE)

        # Connection-level received-but-unacked DATA bytes (see
        # _credit_received).  Stream-level credit is tracked per
        # _PendingResponse.
        self._unacked_conn: int = 0

        # While these are set the peer owes CONTINUATION (RFC 9113 §6.10).
        self._open_field_block = None
        self._field_block_opened_at: float | None = None
        self._failure: str | None = None
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
        # RFC 9113 §6.5.3 — SETTINGS are acknowledged in order, so counting
        # what we sent against what the peer acknowledged is what says
        # whether a parameter we sent is yet in force.  A boolean could not:
        # it cannot tell our first SETTINGS from a later one.
        self._settings_sent: int = 0
        self._settings_acked: int = 0
        #: Which of our SETTINGS carried ENABLE_PUSH=0; None when none did.
        self._no_push_generation: int | None = None

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

        Under ``scenario_mode`` this is a no-op, which makes it the twin of
        :meth:`blackbull.client.http1.HTTP1Client._start`: the scenario owns
        the wire from byte zero.  That is not a convenience — a fault
        scenario exists to assemble its own bytes, and it cannot express a
        preface fault (delayed, split, absent, repeated) on a connection
        that has already sent a correct one.
        """
        if self._scenario_mode or self._receive_task is not None:
            return
        assert self._raw_writer is not None and self._writer is not None

        # HTTP/2 connection preface (RFC 7540 §3.5).
        self._raw_writer.write(_HTTP2_PREFACE)
        await self._raw_writer.drain()

        # The only SETTINGS this client sends, hence the only place a
        # generation is opened.
        self._control_sender = HTTP2Sender(self._writer, self._factory, 0)
        self._settings_sent += 1
        enable_push = None
        if not self._push_permitted:
            enable_push = 0
            self._no_push_generation = self._settings_sent
        await self._control_sender(self._factory.settings(
            enable_push=enable_push,
            max_header_list_size=self._advertised_header_list_size,
        ))

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
        for sid in list(self._responses):
            pending = self._drop_pending(sid)
            if pending is not None and not pending.future.done():
                pending.future.set_exception(
                    ConnectionError('client connection closed'))
        # A refusal still in flight would write RST_STREAM to the transport
        # this method has just closed.
        for task in list(self._detached):
            task.cancel()
        self._detached.clear()
        # Each stream releases its own sender at its last send; this is for a
        # connection torn down before that point was ever reached.
        self._senders.clear()
        # Already empty when the receive loop's own teardown ran first.
        self._end_raw_streams()

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
                # Use the sender's flow-controlled DATA path rather than a
                # single raw DATA frame: it splits the body across
                # SETTINGS_MAX_FRAME_SIZE chunks and blocks on flow-control
                # credit, so bodies larger than one frame (e.g. >16 KiB gRPC
                # messages) are sent correctly.  WINDOW_UPDATE frames are
                # routed to this sender in ``_on_window_update``, keeping its
                # send window in sync.
                #
                # Run as a task the refusal can reach.  This await happens
                # before the one on ``future``, and RST_STREAM is precisely
                # what makes a peer stop crediting the stream window — so a
                # response refused mid-upload left the caller parked here on
                # a window that would never reopen, holding an answer it
                # could not deliver.
                upload = asyncio.ensure_future(
                    sender._write_data(body, end_stream=True))
                pending = self._responses.get(stream_id)
                if pending is not None:
                    pending.upload = upload
                try:
                    await upload
                except asyncio.CancelledError:
                    # ``_drop_pending`` cancelled it, and the verdict is
                    # already on the future.  A cancellation from the caller
                    # leaves the future unresolved and must propagate — and
                    # takes the upload with it, since awaiting a task does not
                    # cancel it when the awaiting coroutine is cancelled.
                    if not future.done():
                        upload.cancel()
                        raise
        except BaseException:
            # The future is only resolvable by the receive loop, and the
            # receive loop only knows about streams the peer answered.  A
            # send that never reached the wire has no answer coming, so its
            # entry would sit in ``_responses`` until GOAWAY or __aexit__ —
            # a dict that grows once per failed send.
            self._drop_pending(stream_id)
            raise
        finally:
            # Released on the last *send*, never on the last receive.  A server
            # may answer with END_STREAM while this body is still going up (an
            # early 401 or 413), so a sender dropped when the response
            # completes would leave ``_write_data`` parked on a window event
            # nothing will set again.  Nothing sends on this stream past this
            # point, so the sweeps in ``_on_window_update`` and
            # ``_on_initial_window_size`` have no reason to keep reaching it.
            self._senders.pop(stream_id, None)

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
        self._check_scenario_ownership(scenario)
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
                        _record_frame(result, await asyncio.wait_for(
                            self._receive_frame(), timeout=step.timeout))
                    except (asyncio.TimeoutError, TimeoutError) as exc:
                        result.timed_out = True
                        result.exception = repr(exc)
                        return result
                elif isinstance(step, _ScWaitForServerFrame):
                    # The step that lets a scenario observe a *verdict*.
                    # One read cannot: the first frame a correct server
                    # sends is its handshake SETTINGS, so a GOAWAY is
                    # always further down a stream whose depth varies by
                    # peer.  Skipping is safe here in a way it is not on
                    # HTTP/1.1 — HTTP/2 streams are independent.
                    deadline = _time.monotonic() + step.timeout
                    while True:
                        remaining = deadline - _time.monotonic()
                        if remaining <= 0:
                            result.wait_timed_out = True
                            break
                        try:
                            frame = await asyncio.wait_for(
                                self._receive_frame(), timeout=remaining)
                        except (asyncio.TimeoutError, TimeoutError):
                            result.wait_timed_out = True
                            break
                        if frame is None:
                            result.wait_timed_out = True
                            break
                        _record_frame(result, frame)
                        if _sc_frame_matches(frame, step.match):
                            break
                        result.wait_skipped += 1
                elif isinstance(step, _ScExpectServerFrame):
                    try:
                        frame = await asyncio.wait_for(
                            self._receive_frame(), timeout=step.timeout)
                    except (asyncio.TimeoutError, TimeoutError):
                        result.wait_timed_out = True
                        result.expectations.append((dict(step.match), False))
                    else:
                        if frame is None:
                            result.expectations.append(
                                (dict(step.match), False))
                        else:
                            _record_frame(result, frame)
                            result.expectations.append(
                                (dict(step.match),
                                 _sc_frame_matches(frame, step.match)))
                elif isinstance(step, _ScHalfClose):
                    result.half_closed = _sc_half_close(self._raw_writer)
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

    def _check_scenario_ownership(self, scenario: ScenarioH2Client) -> None:
        """Reject a scenario the connection cannot actually express.

        Raised up front, before any byte reaches the wire, so a scenario
        either runs whole or not at all.

        Both checks catch the same class of defect: HTTP/2, unlike
        HTTP/1.1, has a connect-time preamble, so a client that has already
        handshaked is *not* a blank wire.  Sending a second preface down it
        does not test a peer's preface handling — it corrupts the frame
        stream ahead of every later step, and a lenient peer skipping the
        junk makes the scenario look like it passed.
        """
        if self._scenario_mode:
            return
        if any(isinstance(s, _ScSendPreface) for s in scenario.steps):
            raise RuntimeError(
                'SendPreface on an already-prefaced connection: this client '
                'sent the preface and SETTINGS when it connected, so the '
                "scenario's preface would be the second one on the wire. "
                'Construct the client with scenario_mode=True.')
        if any(isinstance(s, _ScReadResponse) for s in scenario.steps) \
                and self._receive_task is not None:
            raise RuntimeError(
                'ReadResponse races the receive loop: both would read the '
                'same socket, so which one sees a frame is a coin toss. '
                'Construct the client with scenario_mode=True.')

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

    async def receive_raw_frame(self) -> _InboundFrame | None:
        """Escape hatch: read one raw frame, bypassing the receive loop's dispatch.

        For negative-path / fault-injection tests and raw-frame clients that
        need a peer frame ``_receive_loop`` would otherwise route through the
        normal dispatcher — the read-side twin of :meth:`send_raw_frame`.

        Only safe to call when the receive loop is not running (i.e. before
        ``__aenter__`` finishes or after the loop has been cancelled); a
        concurrent loop would race this call for the reader.

        Inherits ``client_h2_max_frame_size``: one rule, not a second path
        around it.  A scenario needing an over-sized frame opts out with
        ``BB_CLIENT_H2_MAX_FRAME_SIZE=0``.
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

        The depth is ``client_raw_queue_depth``.  Flow control does not
        substitute for it: most of what lands here is not flow-controlled,
        and RFC 9113 §6.9.1 charges a DATA frame's payload only, so a
        zero-length one costs the peer no credit at all.
        """
        if self._connection_lost:
            raise ConnectionError('connection closed by peer')
        if stream_id in self._raw_streams:
            raise ValueError(f'stream {stream_id} already registered as raw')
        q: asyncio.Queue = asyncio.Queue(
            maxsize=get_settings().client_raw_queue_depth)
        self._raw_streams[stream_id] = q
        return q

    def unregister_raw_stream(self, stream_id: int) -> None:
        """Stop routing frames for *stream_id* into its raw-frame queue."""
        self._raw_streams.pop(stream_id, None)
        # A WebSocket-over-H2 session writes through its sender until it
        # unregisters — its shutdown sends the CLOSE frame first — so this is
        # that stream's last-send boundary, the same one ``request()`` uses.
        self._senders.pop(stream_id, None)

    def _signal_raw_stream(self, stream_id: int, queue: asyncio.Queue,
                           code: ErrorCodes) -> int:
        """End one raw stream's queue with a synthetic RST_STREAM; return the
        flow-controlled octets displaced to make room for it.

        A consumer parked in ``Queue.get()`` is reachable by a frame and not
        by a flag, so the terminator has to fit.  On a full queue it displaces
        the backlog instead of raising: a stream being ended will never act on
        those frames, and a ``QueueFull`` here leaves every raw stream after
        this one unsignalled.  A queue that is not full drops nothing, so a
        clean teardown still delivers what already arrived.
        """
        displaced = 0
        if queue.full():
            while not queue.empty():
                displaced += _flow_controlled_length(queue.get_nowait())
        queue.put_nowait(self._factory.rst_stream(
            stream_id=stream_id, error_code=code))
        return displaced

    def _end_raw_streams(self) -> None:
        """Signal every raw stream, then forget it — clearing first would
        drop the queues unsignalled.

        CONNECT_ERROR: these are Extended CONNECT streams (RFC 9113 §7).
        Displaced credit is not returned; the connection that would carry the
        WINDOW_UPDATE is the one this method exists to end.
        """
        for stream_id, queue in self._raw_streams.items():
            self._signal_raw_stream(stream_id, queue, ErrorCodes.CONNECT_ERROR)
        self._raw_streams.clear()

    async def _refuse_raw_stream(self, frame) -> None:
        """Refuse the raw stream whose queue *frame* overflowed, and only it.

        ENHANCE_YOUR_CALM (RFC 9113 §7) — the peer is generating load faster
        than the registrant consumes it — which is also why
        :meth:`_refuse_stream` sends it for the header-aggregate breach.
        """
        stream_id = frame.stream_id
        queue = self._raw_streams[stream_id]
        limit = get_settings().client_raw_queue_depth
        log_cap_hit('client_raw_queue_depth', requested=queue.qsize() + 1,
                    limit=limit, protocol='http2')
        displaced = self._signal_raw_stream(
            stream_id, queue, ErrorCodes.ENHANCE_YOUR_CALM)
        self.unregister_raw_stream(stream_id)
        try:
            await self._send_raw_frame(self._factory.rst_stream(
                stream_id, ErrorCodes.ENHANCE_YOUR_CALM))
            # The payload is dropped; the credit is not — the shared-window
            # rule ``_on_response_data`` explains.  A raw stream's DATA is
            # credited on drain, so the backlog still holds window.
            await self._credit_connection(
                displaced + _flow_controlled_length(frame))
        except Exception:
            # A peer that has gone away can be neither told nor credited, and
            # raising would end the loop this cap exists to keep running.
            logger.debug('could not send RST_STREAM for stream %d', stream_id)

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
                conn_window=self._conn_window,
                initial_window=self.initial_window_size)
        return self._senders[stream_id]

    async def _send_raw_frame(self, frame: FrameBase) -> None:
        assert self._control_sender is not None
        await self._control_sender(frame)

    async def _receive_frame(self) -> _InboundFrame | None:
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

        The declared length is checked before either payload read, so no
        peer-declared number ever sizes an allocation.  Over it is
        FRAME_SIZE_ERROR (RFC 9113 §4.2) rather than ``None``, which the loop
        would read as the peer having gone away.
        """
        assert self._reader is not None
        try:
            header = await self._reader.readexactly(_FRAME_HEADER_BYTES)
        except (asyncio.IncompleteReadError, EOFError):
            return None
        length = int.from_bytes(header[:3], 'big', signed=False)
        max_frame = get_settings().client_h2_max_frame_size
        if max_frame and length > max_frame:
            log_cap_hit('client_h2_max_frame_size', requested=length,
                        limit=max_frame, protocol='http2')
            # Connection, never stream, though §4.2 would allow one here: the
            # payload is not read, so it stays in the socket and the next
            # 9-byte read would land inside it.  Draining it first to keep the
            # stream option open would cost whatever the peer declared.
            await self._fail_connection(
                ErrorCodes.FRAME_SIZE_ERROR,
                f'frame length {length} exceeds '
                f'BB_CLIENT_H2_MAX_FRAME_SIZE={max_frame}')
        if not length:
            return await self._load(header)
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
        return await self._load(header + payload)

    async def _load(self, data: bytes) -> _InboundFrame | None:
        """Parse one frame.  A whole block is decoded in the constructor, so
        this is one of the two places a parse can fail."""
        try:
            return self._factory.load(data)
        except _ConnectionFailed:
            raise
        except Exception as exc:
            await self._fail_parse(exc, max(len(data) - _FRAME_HEADER_BYTES, 0))
            return None  # unreachable: _fail_parse raises

    async def _fail_parse(self, exc: Exception, encoded: int) -> None:
        """End the connection with the code the failure earned.

        COMPRESSION_ERROR for everything told the peer its HPACK state was
        unusable (§5.4.1) over a wrong length octet — or over our own bug.
        Connection-wide in every arm: §6.1, §6.4 and §6.7 say so, and §4.3
        leaves an undecoded block's table unrecoverable.  §4.2's stream-error
        option is ``_receive_frame``'s, not this one's.

        *encoded* is how many octets arrived carrying the block — the frame
        payload for a block that fitted one frame, the reassembled block for
        one that did not.  It is the only size this method can attribute to a
        refusal; see the oversize arm for what that costs.
        """
        if isinstance(exc, FrameFormatError):
            await self._fail_connection(exc.error_code, str(exc))
        elif isinstance(exc, OversizedHeaderListError):
            # The one HPACK failure a bound of ours caused, and the only arm
            # that may name it: an invalid index or a bad table-size update
            # is the peer's own error, and a cap-hit record for one would be
            # a record of a refusal that never happened.
            #
            # ``requested`` is the *encoded* block length because hpack
            # reports the limit it refused at and never the decoded total it
            # reached.  Compression makes the two independent — 3528 encoded
            # octets decode to 80,740 in this tree's own test — so the number
            # here is what arrived on the wire and is not the quantity the
            # cap measures.
            log_cap_hit('client_h2_max_header_list_size', requested=encoded,
                        limit=self._enforced_header_list_size,
                        protocol='http2')
            await self._fail_connection(
                ErrorCodes.COMPRESSION_ERROR,
                f'could not decode the field block: {exc}')
        elif isinstance(exc, HPACKError):
            await self._fail_connection(
                ErrorCodes.COMPRESSION_ERROR,
                f'could not decode the field block: {exc}')
        else:
            logger.exception('HTTP/2 frame parse failed')
            await self._fail_connection(
                ErrorCodes.INTERNAL_ERROR,
                f'the client failed to parse a frame: {exc!r}')

    async def _next_frame(self) -> _InboundFrame | None:
        """The next frame — deadlined only while a field block is open.

        ``_receive_frame`` waits for a frame to *begin* without a bound, and
        must: server streaming and long polling are a peer behaving correctly.
        A peer that owes CONTINUATION has instead stopped mid-message.
        """
        opened_at = self._field_block_opened_at
        if opened_at is None:
            return await self._receive_frame()
        timeout = get_settings().client_head_timeout
        if timeout <= 0:
            return await self._receive_frame()
        try:
            async with asyncio.timeout_at(opened_at + timeout):
                return await self._receive_frame()
        except TimeoutError:
            log_cap_hit('client_head_timeout', requested=timeout,
                        limit=timeout, protocol='http2')
            await self._fail_connection(
                ErrorCodes.ENHANCE_YOUR_CALM,
                f'field block incomplete after '
                f'BB_CLIENT_HEAD_TIMEOUT={timeout}s')
            return None  # unreachable: _fail_connection raises

    async def _fail_connection(self, code: ErrorCodes, message: str) -> None:
        """End the connection, telling the peer why before telling the caller.

        Never one stream: HPACK state is connection-wide, so a block the
        decoder did not walk leaves it unable to read any later block.
        """
        last_stream_id = max(self._next_stream_id - 2, 0)  # §6.8
        try:
            await self._send_raw_frame(
                self._factory.goaway(last_stream_id, code))
        except Exception:
            # exc_info because a bug in the frame we build looks from here
            # exactly like a peer that has gone away.
            logger.warning('could not send GOAWAY(%r)', code, exc_info=True)
        raise _ConnectionFailed(message)

    async def _absorb_field_block(self, frame) -> _InboundFrame | None:
        """Reassemble a split field block; return what to dispatch, or
        ``None`` while the block is still open.

        RFC 9113 §4.3 requires the block decompressed *even if the frames are
        to be discarded*: the decoder's dynamic table advances as a side
        effect, and it is connection-wide.
        """
        frame_type = frame.FrameType()
        open_frame = self._open_field_block
        if open_frame is not None:
            # §6.10 — a block is atomic on the wire, so even an unknown type
            # that §5.5 would have us ignore ends the connection here.
            if frame_type is not FrameTypes.CONTINUATION:
                name = frame_type.name if frame_type is not None else 'unknown'
                await self._fail_connection(
                    ErrorCodes.PROTOCOL_ERROR,
                    f'expected CONTINUATION, got {name}')
            if frame.stream_id != open_frame.stream_id:
                await self._fail_connection(
                    ErrorCodes.PROTOCOL_ERROR,
                    f'CONTINUATION on stream {frame.stream_id} while stream '
                    f'{open_frame.stream_id} has a field block open')
            return await self._extend_field_block(open_frame, frame)
        if frame_type is FrameTypes.CONTINUATION:
            await self._fail_connection(
                ErrorCodes.PROTOCOL_ERROR,
                'CONTINUATION without a preceding HEADERS')
        if frame_type in _OPENS_A_FIELD_BLOCK and not frame.end_headers:
            self._open_field_block = frame
            self._field_block_opened_at = asyncio.get_running_loop().time()
            await self._guard_field_block(frame)
            return None
        return frame

    async def _extend_field_block(self, open_frame, frame):
        payload = frame.payload or b''
        # A bytearray extends in amortised O(1); ``bytes += bytes`` is O(n^2).
        if not isinstance(open_frame.raw_block, bytearray):
            open_frame.raw_block = bytearray(open_frame.raw_block)
        open_frame.raw_block += payload
        await self._guard_field_block(open_frame)
        if not frame.end_headers:
            return None
        self._open_field_block = None
        self._field_block_opened_at = None
        try:
            open_frame.parse_payload()
        except _ConnectionFailed:
            raise
        except Exception as exc:
            # §4.3.  hpack may have applied part of the block to the dynamic
            # table before raising, so there is no sound way to carry on.
            await self._fail_parse(exc, len(open_frame.raw_block))
        return open_frame

    async def _guard_field_block(self, open_frame) -> None:
        """The field block is the HTTP/2 spelling of "the head", so it answers
        to the head's total.  Called on every append, so the cap bounds the
        memory and not just the answer.

        Connection-wide, unlike the same budget in ``_on_response_headers``:
        there the block had been decoded, here it has not.
        """
        max_total = get_settings().client_head_max_total
        seen = len(open_frame.raw_block)
        if max_total and seen > max_total:
            log_cap_hit('client_head_max_total', requested=seen,
                        limit=max_total, protocol='http2')
            await self._fail_connection(
                ErrorCodes.COMPRESSION_ERROR,
                f'field block exceeds BB_CLIENT_HEAD_MAX_TOTAL={max_total}')

    async def _receive_loop(self) -> None:
        try:
            while True:
                frame = await self._next_frame()
                if frame is None:
                    break
                frame = await self._absorb_field_block(frame)
                if frame is None:
                    continue
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
                    try:
                        raw_q.put_nowait(frame)
                    except asyncio.QueueFull:
                        # Inside the loop's try: outside it, the catch-all
                        # below would end every stream to bound one.
                        await self._refuse_raw_stream(frame)
                    continue
                try:
                    await ResponderFactory.create(frame).respond(self)
                except _ConnectionFailed:
                    # A responder that ended the connection on purpose is not
                    # a responder that crashed: swallowing it leaves the loop
                    # reading on a connection it has just GOAWAYed.
                    raise
                except Exception:
                    logger.exception('responder failed for frame %r', frame)
        except _ConnectionFailed as exc:
            self._failure = str(exc)
            logger.warning('HTTP/2 connection ended: %s', exc)
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
            for sid in list(self._responses):
                pending = self._drop_pending(sid)
                if pending is not None and not pending.future.done():
                    pending.future.set_exception(ConnectionError(
                        self._failure or 'connection closed before response'))
            # The other half of who is waiting on this connection.
            self._end_raw_streams()

    # ---- internal: callbacks invoked by Responders -----------------------

    def _drop_pending(self, stream_id: int, *,
                      aborted: bool = True) -> '_PendingResponse | None':
        """Remove a stream's state and release what was holding it open.

        Every path that ends a response goes through here.  Popping alone left
        a progress timer on the loop — a strong reference to this client until
        it fired — and, on an aborted stream, an upload still writing DATA on
        a stream we had just closed, which RFC 9113 §5.1 forbids and a peer
        MAY answer with a connection error, losing the point of refusing only
        one stream.

        *aborted* is ``False`` when the response merely finished.  The upload
        and its sender are then left alone: a server may answer with
        END_STREAM while the request body is still going up (an early 401 or
        413), and releasing the sender there parks ``_write_data`` on a window
        event nothing will set again.  That is the defect BLA-269 fixed, and
        this helper re-introduced until ``test_an_early_response_does_not_
        strand_the_upload`` caught it.
        """
        pending = self._responses.pop(stream_id, None)
        if pending is None:
            return None
        pending.disarm()
        if aborted:
            if pending.upload is not None and not pending.upload.done():
                pending.upload.cancel()
            self._senders.pop(stream_id, None)
        return pending

    def _spawn(self, coro) -> None:
        """Run *coro* detached, holding a strong reference until it ends.

        asyncio keeps only a weak reference to a task, and a bare
        ``ensure_future`` also outlived ``__aexit__`` here — still writing to
        a transport the client had just closed.
        """
        task = asyncio.ensure_future(coro)
        self._detached.add(task)
        task.add_done_callback(self._detached.discard)

    def _arm_progress_deadline(self, stream_id: int) -> None:
        """Start or restart the stream's progress timer.

        Armed by the first response frame rather than by the request, for the
        reason the HTTP/1.1 rate floor exempts the wait before the first body
        octet: a peer that has not answered yet is working, not stalling, and
        judging that wait would refuse a slow query for being slow.  What is
        bounded here is the gap between frames once a response has begun.

        ``_FRAME_READ_TIMEOUT`` does not cover this.  It bounds the remainder
        of a frame whose 9-byte header has arrived, so a peer sending one
        complete 1-byte DATA frame every 29 s never trips it.
        """
        pending = self._responses.get(stream_id)
        if pending is None:
            return
        timeout = get_settings().client_body_timeout
        if timeout <= 0:
            return
        loop = asyncio.get_running_loop()
        if not pending.opened_at:
            pending.opened_at = loop.time()
        pending.disarm()
        pending.deadline = loop.call_later(
            timeout, self._on_stream_stalled, stream_id, timeout)

    def _on_stream_stalled(self, stream_id: int, timeout: float) -> None:
        """No frame for this stream within the deadline.

        The verdict is taken again inside :meth:`_refuse_stream`, not here.
        A timer callback cannot await, so this hands off to a task, and in
        that gap the receive loop can finish the response — checking only
        here reset a stream that had already succeeded and logged a cap hit
        against it.
        """
        pending = self._responses.get(stream_id)
        if pending is None:
            return
        elapsed = asyncio.get_running_loop().time() - pending.opened_at
        self._spawn(self._refuse_stream(
            stream_id, 'client_body_timeout', elapsed, timeout,
            TimeoutError(
                f'no HTTP/2 frame for stream {stream_id} within '
                f'BB_CLIENT_BODY_TIMEOUT={timeout}s')))

    async def _refuse_stream(self, stream_id: int, cap: str,
                             seen: int | float, limit: int | float,
                             error: Exception,
                             code: ErrorCodes = ErrorCodes.CANCEL) -> None:
        """Refuse one response without ending the connection.

        The difference from the HTTP/1.1 client is real and belongs here.  A
        refusal there leaves the reader's position inside a message, so the
        connection is abandoned; HTTP/2 frames are self-delimiting, so the
        peer's remaining DATA parses as DATA whether or not we want it.
        ``RST_STREAM`` refuses this response and every other stream, and the
        connection, survives — which is what makes a per-stream cap usable at
        all.

        *code* defaults to ``CANCEL`` — RFC 9113 §7, "the stream is no longer
        needed" — which fits a budget *this* client chose and the peer did not
        violate.  The header-aggregate breach passes ``ENHANCE_YOUR_CALM``
        instead, because the server answers its own header cap that way and
        cites nginx and Envoy for it; one cap should not get two codes
        depending on which end enforces it.

        Nothing happens if the stream has already ended, and that check has to
        be here rather than at the call sites: the timeout path arrives
        through a task, and in the gap the receive loop can complete the
        response.
        """
        pending = self._drop_pending(stream_id)
        if pending is None:
            return
        log_cap_hit(cap, requested=seen, limit=limit, protocol='http2')
        if not pending.future.done():
            pending.future.set_exception(error)
        try:
            await self._send_raw_frame(
                self._factory.rst_stream(stream_id, code))
        except Exception:
            # The refusal already happened; a peer that has gone away cannot
            # also be told about it, and raising here would replace the
            # caller's ResponseTooLarge with a write error.
            logger.debug('could not send RST_STREAM for stream %d', stream_id)

    async def _on_response_headers(self, frame) -> None:
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
                self._drop_pending(frame.stream_id)
                return
        # An interim response is not the response.  Arming on a 1xx starts the
        # progress clock while the peer is still working — 103 Early Hints
        # followed by a second of real work was refused, which is exactly the
        # "a peer that has not answered yet is working" case the deadline is
        # supposed to be exempt from.  Its field lines still count: they are
        # accumulation whatever they announce.
        if pending.status >= 200:
            self._arm_progress_deadline(frame.stream_id)
        max_headers = get_settings().client_head_max_total
        for name, value in frame.headers:
            name, value = _to_bytes(name), _to_bytes(value)
            pending.headers_seen += len(name) + len(value)
            if max_headers and pending.headers_seen > max_headers:
                await self._refuse_stream(
                    frame.stream_id, 'client_head_max_total',
                    pending.headers_seen, max_headers,
                    ResponseTooLarge(
                        f'response header fields exceed '
                        f'BB_CLIENT_HEAD_MAX_TOTAL={max_headers}'),
                    ErrorCodes.ENHANCE_YOUR_CALM)
                return
            pending.headers.append((name, value))
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
            # ``frame.length``, not ``len(payload)``: RFC 9113 §6.9.1 counts
            # the whole DATA payload against the window, and ``payload`` has
            # already had the pad-length octet and the padding stripped.
            # Crediting the visible half leaked the window by the padding —
            # 7.9% on a 20%-padded stream, and a leak only ever closes.
            await self._credit_connection(frame.length)
            return
        payload = frame.payload
        max_body = get_settings().client_body_max_total
        if max_body and pending.body_seen + len(payload) > max_body:
            # Checked before the append: the cap bounds memory, so the frame
            # that breaches it must not be held.  Credit is still owed for it
            # — see the shared-window note above — and the peer will keep
            # sending until RST_STREAM reaches it, which the branch below
            # keeps crediting.
            await self._refuse_stream(
                frame.stream_id, 'client_body_max_total',
                pending.body_seen + len(payload), max_body,
                ResponseTooLarge(
                    f'response body exceeds '
                    f'BB_CLIENT_BODY_MAX_TOTAL={max_body}'))
            await self._credit_connection(frame.length)
            return
        if payload:
            # Only a frame that delivered body octets is progress, and only
            # such a frame is held.  A peer sending empty (or all-padding)
            # DATA re-armed the deadline and appended to body_parts while
            # body_seen stayed at zero: neither bound could fire, and 200,000
            # such frames held 1.6 MB against a 1 KiB cap.
            self._arm_progress_deadline(frame.stream_id)
            pending.body_seen += len(payload)
            pending.body_parts.append(payload)
        # RFC 9113 §6.9 — the receiver MUST return flow-control credit for
        # consumed DATA via WINDOW_UPDATE.  Without this the server's send
        # window drains and ``HTTP2Sender._write_data`` blocks once a
        # response exceeds the 65535-byte initial window — a deadlock that
        # affects any large HTTP/2 body, not just gRPC.
        if frame.length:
            # Gated on the flow-controlled length, not on the visible payload:
            # an all-padding DATA frame delivers nothing and still consumed
            # the window, so crediting only when payload arrived leaked it.
            await self._credit_received(
                frame.stream_id, pending, frame.length,
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
        pending = self._drop_pending(stream_id, aborted=False)
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
            # Credited once, not once per sender: they all debit this same
            # object, so a per-sender credit would multiply the peer's grant.
            # Waking is still per sender, because each parks on its own event.
            self._conn_window.size += increment
            for sender in self._senders.values():
                sender.wake_window()
        else:
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

    def _on_settings_ack(self) -> None:
        """RFC 9113 §6.5.3 — the ACK is the synchronisation point at which
        the parameters we sent take effect for the peer."""
        self._settings_acked += 1

    def _push_forbidden(self) -> bool:
        """True once ENABLE_PUSH=0 has been both sent and acknowledged.

        §6.5.2 conditions the receiver's MUST on both, so the round-trip in
        between is a window where a PUSH_PROMISE is conforming traffic.
        """
        return (self._no_push_generation is not None
                and self._settings_acked >= self._no_push_generation)

    async def _on_push_promise(self, frame) -> None:
        """Refuse a promise we forbade, after its block has been decoded.

        The decode is not optional: §4.3 requires it even for a frame that
        is discarded, because the HPACK table is connection-wide and a block
        skipped silently corrupts every later one.  So the refusal happens
        after the decode, never instead of it.
        """
        if not self._push_forbidden():
            logger.debug('PUSH_PROMISE on stream %d dropped', frame.stream_id)
            return
        await self._fail_connection(
            ErrorCodes.PROTOCOL_ERROR,
            f'PUSH_PROMISE on stream {frame.stream_id} after '
            f'SETTINGS_ENABLE_PUSH=0 was acknowledged')

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
                self._drop_pending(sid)
                pending.future.set_exception(ConnectionError(
                    f'connection closed by peer (GOAWAY error_code={frame.error_code})'))

    def _on_rst_stream(self, frame) -> None:
        pending = self._drop_pending(frame.stream_id)
        if pending is None:
            return
        if not pending.future.done():
            pending.future.set_exception(
                StreamReset(frame.stream_id, int(frame.error_code)))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_str(value: str | bytes) -> str:
    return value.decode('ascii') if isinstance(value, (bytes, bytearray)) else value


def _to_bytes(value: str | bytes) -> bytes:
    return value.encode('ascii') if isinstance(value, str) else bytes(value)

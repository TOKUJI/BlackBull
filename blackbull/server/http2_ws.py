"""HTTP/2 WebSocket byte-stream adapters (RFC 8441).

HTTP2WSReader and HTTP2WSWriter bridge between the HTTP/2 DATA frame layer
and the RFC 6455 WebSocket codec (ws_codec.py).

HTTP2WSReader — AbstractReader backed by DATA frame payloads pushed by
HTTP2Actor._on_data_frame(). Implements the same put_DATAFrame / put_disconnect
interface as HTTP2Recipient so the existing _signal_recipients() helper and
_on_data_frame() dispatch work without modification.

HTTP2WSWriter — AbstractWriter that wraps an HTTP2Sender and translates raw
WebSocket frame bytes into http.response.body events (inheriting HTTP/2 flow
control from the sender). close() sends a final empty DATA+END_STREAM to signal
orderly HTTP/2 stream termination per RFC 8441 §5.
"""
import asyncio
from typing import Awaitable, Callable, Optional

from .recipient import AbstractReader
from .sender import AbstractWriter
from ..protocol.frame_types import Data


# Default per-stream buffer cap before the reader starts withholding
# WINDOW_UPDATE credit.  1 MiB accommodates the largest reasonable
# single WebSocket frame plus headroom; tune via the ``max_buffer``
# constructor parameter when the workload has known-larger frames.
_DEFAULT_MAX_BUFFER = 1024 * 1024


class HTTP2WSReader(AbstractReader):
    """Byte-buffer AbstractReader fed by HTTP/2 DATA frames.

    ws_codec.read_frame_header / read_payload call readexactly(n); this
    class buffers raw DATA frame payloads and serves exact-length reads,
    blocking until enough bytes arrive or EOF is signalled.

    **Backpressure**: when the buffered byte count crosses
    ``max_buffer``, ``put_DATAFrame`` returns ``False`` to signal the
    HTTP/2 actor to skip the per-frame ``WINDOW_UPDATE`` emission.  The
    bytes are *still* buffered (the peer's window debited on the wire
    the moment the frame arrived; dropping them would be silent data
    loss).  As soon as :meth:`readexactly` drains the buffer back under
    ``max_buffer``, the reader replays the withheld credit through
    ``credit_callback``, opening the peer's window again.
    """

    # Marker recognised by HTTP2Actor._on_data_frame — when set, a
    # ``False`` return from ``put_DATAFrame`` means "skip WINDOW_UPDATE,
    # bytes are buffered" rather than "drop with RST_STREAM".
    backpressures_via_credit: bool = True

    def __init__(
        self,
        *,
        max_buffer: int = _DEFAULT_MAX_BUFFER,
        credit_callback: Optional[Callable[[int], Awaitable[None]]] = None,
    ) -> None:
        self._buffer: bytearray = bytearray()
        self._data_ready: asyncio.Event = asyncio.Event()
        self._eof: bool = False
        self._max_buffer: int = max_buffer
        self._credit_cb: Optional[Callable[[int], Awaitable[None]]] = (
            credit_callback)
        # Bytes received-and-buffered but not yet credited via
        # WINDOW_UPDATE.  Replayed by readexactly once the buffer
        # drains below max_buffer.
        self._pending_credit: int = 0

    # ------------------------------------------------------------------
    # Push interface (called by HTTP2Actor)
    # ------------------------------------------------------------------

    def put_DATAFrame(self, frame: Data) -> bool:
        """Buffer payload bytes from an incoming DATA frame.

        Always buffers (no silent drop — the peer's window already
        debited ``frame.length`` when the frame hit the wire).  Returns
        ``True`` when the buffer remained under ``max_buffer`` after the
        append; ``False`` when the cap was exceeded.  In the ``False``
        case the actor must skip the per-frame ``WINDOW_UPDATE`` — the
        bytes are queued for credit replay via ``readexactly``.
        """
        self._buffer.extend(frame.payload)
        if frame.end_stream:
            self._eof = True
        self._data_ready.set()
        if len(self._buffer) > self._max_buffer:
            self._pending_credit += len(frame.payload)
            return False
        return True

    def put_disconnect(self) -> None:
        """Signal EOF (connection closed or GOAWAY received)."""
        self._eof = True
        self._data_ready.set()

    # ------------------------------------------------------------------
    # AbstractReader interface
    # ------------------------------------------------------------------

    async def readexactly(self, n: int) -> bytes:
        while len(self._buffer) < n:
            if self._eof:
                raise asyncio.IncompleteReadError(bytes(self._buffer), n)
            self._data_ready.clear()
            await self._data_ready.wait()
        result = bytes(self._buffer[:n])
        del self._buffer[:n]
        # If we withheld credit while over the cap and have now drained
        # back below it, replay the accumulated credit so the peer's
        # window reopens.  ``_credit_cb`` is set by HTTP2Actor in
        # ``_handle_h2_websocket``; tests that construct a reader
        # directly may leave it ``None`` and observe the pending count
        # via ``_pending_credit``.
        if (self._pending_credit > 0
                and len(self._buffer) <= self._max_buffer
                and self._credit_cb is not None):
            credit = self._pending_credit
            self._pending_credit = 0
            await self._credit_cb(credit)
        return result

    async def read(self, n: int) -> bytes:
        return await self.readexactly(n)

    async def readuntil(self, sep: bytes) -> bytes:
        raise NotImplementedError('HTTP2WSReader does not support readuntil')


class HTTP2WSWriter(AbstractWriter):
    """AbstractWriter that sends WebSocket frame bytes as HTTP/2 DATA frames.

    Delegates to an HTTP2Sender so HTTP/2 flow control (RFC 7540 §6.9) is
    honoured transparently. close() sends an empty DATA+END_STREAM to signal
    orderly HTTP/2 stream termination (RFC 8441 §5).
    """

    def __init__(self, stream_sender) -> None:
        self._sender = stream_sender
        self._ended: bool = False

    async def write(self, data: bytes) -> None:
        await self._sender({'type': 'http.response.body', 'body': data, 'more_body': True})

    async def close(self) -> None:
        if not self._ended:
            self._ended = True
            await self._sender({'type': 'http.response.body', 'body': b'', 'more_body': False})

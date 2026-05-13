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

from .recipient import AbstractReader, IncompleteReadError
from .sender import AbstractWriter
from ..protocol.frame_types import Data


class HTTP2WSReader(AbstractReader):
    """Byte-buffer AbstractReader fed by HTTP/2 DATA frames.

    ws_codec.read_frame_header / read_payload call readexactly(n); this class
    buffers raw DATA frame payloads and serves exact-length reads, blocking
    until enough bytes arrive or EOF is signalled.
    """

    def __init__(self) -> None:
        self._buffer: bytearray = bytearray()
        self._data_ready: asyncio.Event = asyncio.Event()
        self._eof: bool = False

    # ------------------------------------------------------------------
    # Push interface (called by HTTP2Actor)
    # ------------------------------------------------------------------

    def put_DATAFrame(self, frame: Data) -> None:
        """Feed raw payload bytes from an incoming DATA frame."""
        self._buffer.extend(frame.payload)
        if frame.end_stream:
            self._eof = True
        self._data_ready.set()

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

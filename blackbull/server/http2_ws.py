"""RFC 8441 byte-stream adapters. Reuse the HTTP/2 connection
factory and flow control; do not create another HPACK context.
"""
import asyncio
from typing import Awaitable, Callable

from .recipient import AbstractReader, IncompleteReadError
from .sender import AbstractWriter
from ..native import NativeResponse
from ..protocol.frame_types import Data


class HTTP2StreamReader(AbstractReader):
    """One stream's DATA as the WebSocket codec's byte stream, in either role.

    Credit returns as octets are consumed, padding included, so at most
    *budget* octets are ever unconsumed and DATA past that is refused.
    Buffered octets a pending read has committed to count as consumed before
    it waits: otherwise a read longer than the window waits forever.
    """

    def __init__(self, budget: int,
                 credit: Callable[[int], Awaitable[None]]) -> None:
        self._buf = bytearray()
        self._eof = False
        self._arrival = asyncio.Event()
        self._budget = budget
        self._credit = credit
        self._uncredited = 0   # window octets received and not yet credited
        self._owed = 0         # of those, consumed (or padding): creditable now
        self._ahead = 0        # buffered octets already counted in _owed

    def put_DATAFrame(self, frame: Data) -> bool:
        """Buffer *frame*; False when it overruns the window (the caller resets)."""
        if self._uncredited + frame.length > self._budget:
            return False
        self._uncredited += frame.length
        self._owed += frame.length - len(frame.payload)
        self._buf += frame.payload
        if frame.end_stream:
            self._eof = True
        self._arrival.set()
        return True

    def put_disconnect(self) -> None:
        self._eof = True
        self._arrival.set()

    def take_uncredited(self) -> int:
        """Return and clear the credit never returned; the stream is being released."""
        n = self._uncredited
        self._uncredited = self._owed = self._ahead = 0
        return n

    async def readexactly(self, n: int) -> bytes:
        while len(self._buf) < n:
            if self._eof:
                raise IncompleteReadError(bytes(self._buf))
            self._arrival.clear()
            self._owed += len(self._buf) - self._ahead
            self._ahead = len(self._buf)
            await self._flush()
            await self._arrival.wait()   # cleared first: arrivals during the flush count
        out = bytes(self._buf[:n])
        del self._buf[:n]
        fresh = n - self._ahead
        if fresh > 0:
            self._owed += fresh
            self._ahead = 0
        else:
            self._ahead = -fresh
        if self._owed * 2 >= self._budget:
            await self._flush()
        return out

    async def read(self, n: int) -> bytes:
        return await self.readexactly(n)

    async def _flush(self) -> None:
        n, self._owed = self._owed, 0
        if n:
            self._uncredited -= n
            await self._credit(n)


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
        # Framework-owned producer on BlackBull's own send path: native, like
        # StaticFiles and CORS.  The header arm went out with the RFC 8441
        # handshake, so these carry a body arm only; ``more_body`` keeps
        # END_STREAM withheld and the session open.
        await self._sender(NativeResponse(body=data, more_body=True))

    async def close(self) -> None:
        if not self._ended:
            self._ended = True
            await self._sender(NativeResponse(body=b'', more_body=False))

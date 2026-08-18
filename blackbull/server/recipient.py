import asyncio
import contextlib
from abc import ABC, abstractmethod
from collections import deque
from time import monotonic as _monotonic
from typing import Awaitable, Callable, NoReturn, Optional

from .cap_log import log_cap_hit
from .deadline import ConnectionDeadline, WsIdleWatchdog
from .sender import AbstractWriter, AsyncioWriter
from .ws_codec import (
    FramePayloadTooLarge, MessageTooLarge, WSOpcode, encode_frame,
    read_frame_header, read_payload,
)
from .constants import WSCloseCode
from .rate_window import RateWindow
from ..asgi import ASGIEvent
from ..connection import Connection, disconnected, mark_disconnected
from ..request import ClientDisconnected
from ..protocol.frame_types import FrameBase, Data, DEFAULT_INITIAL_WINDOW_SIZE
from ..event import Event, EventDispatcher
import logging

logger = logging.getLogger(__name__)

# Per-stream and per-connection event queue depth limits.
# These cap memory growth under overload; see bench/README.md.
_HTTP2_STREAM_QUEUE_DEPTH = 64
# Depth used when WebSocket read-ahead is switched on.  It is *not* the
# default: read-ahead costs a background task plus a queue hop per message
# (one future + one call_soon), which is the whole of WebSocket's loop-touch
# excess over HTTP/1.1.  See ``WebSocketRecipient`` for the two modes.
_WS_EVENT_QUEUE_DEPTH = 256
# Read inline, in the app's own task — no reader task, no per-message handoff.
_WS_READ_INLINE = 0

# What :meth:`HTTP1Recipient.after_dispatch` answers.  Three states because
# there are three things a connection can do next, and the recipient is the
# object that knows which: the message boundary is its business.  Plain ints,
# compared with ``is`` on small values the interpreter interns — this is read
# once per request on every keep-alive connection.
CONNECTION_REUSABLE = 0
CONNECTION_NEEDS_DRAIN = 1
CONNECTION_MUST_CLOSE = 2

# Consume-crediting mode bounds the HTTP/2 stream queue by BYTES (the
# advertised inbound window), not frame count — a conformant peer sending
# 65535 bytes as 1-byte frames must not be RST'd.  This multiplier bounds the
# frame COUNT against zero/tiny-frame floods (CVE-2019-9518-style abuse) that
# the byte budget cannot see: queue_depth × 16 (1024 by default) is far above
# any conformant burst yet keeps per-stream event-dict overhead bounded.
_EVENT_CAP_MULTIPLIER = 16

# Queue marker for "the WebSocket peer is gone".  The WS channel carries the
# message *values* — ``str`` for text, ``bytes`` for binary — so the end of the
# connection needs a value outside that domain rather than a tagged envelope
# every reader would have to unwrap.  The close code rides ``_terminal_code``,
# which the recipient already tracked.
_WS_CLOSED = object()


def _ws_disconnect(code: int | None):
    """Build the app-facing close signal for the native WS channel.

    Imported lazily: ``websocket`` imports ``connection``, which this module
    already depends on, so a module-level import would close a cycle.
    """
    from ..websocket import WebSocketDisconnect  # noqa: PLC0415
    return WebSocketDisconnect(code or WSCloseCode.ABNORMAL)

# Queue marker for "the peer is gone".  The H2 queue carries native
# ``(chunk, end_of_stream)`` pairs, and a disconnect is not a chunk — it is the
# absence of any further one — so it needs a value outside that domain rather
# than a third tuple field every reader would have to check.
_H2_DISCONNECT = object()

# RFC 6455 §5.5 control opcodes — used by the non-blocking control-frame
# servicing (``WebSocketRecipient.service_available_control_frames``), which
# must never consume a data frame ahead of the app.
_WS_CONTROL_OPS = (WSOpcode.CLOSE, WSOpcode.PING, WSOpcode.PONG)


# ---------------------------------------------------------------------------
# Reader abstraction — swap asyncio for trio/curio by implementing this ABC
# ---------------------------------------------------------------------------

class IncompleteReadError(EOFError):
    """Raised by AbstractReader when the peer closes the connection mid-read.

    Mirrors asyncio.IncompleteReadError but is not tied to asyncio, so
    handlers that depend on AbstractReader remain runtime-agnostic.
    """

    @property
    def partial(self) -> bytes:
        """Whatever had been read when the peer went away.

        A truncated head and an idle close are the same exception with
        different payloads, and the caller answers 400 for one and nothing at
        all for the other — so the payload is part of the contract, not a
        debugging aid.
        """
        return self.args[0] if self.args else b''


class ReadLimitExceeded(Exception):
    """``read_head`` was given a byte budget and the head passed it.

    Belongs to the reader contract rather than to any protocol: the reader is
    told a budget and reports that it was passed.  Which status that becomes
    (431 for a head with too many fields, 400 for bytes that were never a head
    at all) is the protocol's decision — so the reader hands back what it
    :attr:`saw`, and every reader answers that question off the same evidence.
    """

    def __init__(self, message: str, seen: bytes = b'') -> None:
        super().__init__(message)
        #: The over-budget bytes, for the caller to classify.  Never consumed
        #: on the caller's behalf: a reader that owns its buffer leaves them
        #: resident so the connection can still be lingered closed.
        self.seen = seen


#: End of an HTTP message head.  Lives here because :meth:`AbstractReader.read_head`
#: is what looks for it; the H/1.1 actor imports it rather than keeping a copy.
_HEAD_END = b'\r\n\r\n'


_HEXDIG_SET = frozenset(b'0123456789abcdefABCDEF')

# RFC 9110 §5.6.2 — ``token = 1*tchar``.  Used to validate ``chunk-ext-name``
# and an unquoted ``chunk-ext-val`` (RFC 9112 §7.1.1).
_TCHAR_SET = frozenset(
    b"!#$%&'*+-.^_`|~"
    b"0123456789"
    b"abcdefghijklmnopqrstuvwxyz"
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def _bad_request(detail: str):
    """Build the framework's status-carrying 400 exception.

    Imported lazily so ``recipient`` (loaded early via the server) never
    depends on ``router`` at module-import time.  ``HTTPException`` is the
    dispatcher's typed-error seam — raising it from the body reader makes a
    malformed chunked frame surface as ``400 Bad Request`` instead of a
    fabricated 500.
    """
    from http import HTTPStatus  # noqa: PLC0415
    from ..router import HTTPException  # noqa: PLC0415
    return HTTPException(HTTPStatus.BAD_REQUEST, detail)


def _content_too_large(detail: str):
    """Build the 413 for a body that outgrew the cap mid-stream.

    Only ``chunked`` can reach here: a ``Content-Length`` declares the whole
    body in the head, so the actor refuses that one before reading any of it.
    A chunked body announces nothing, so the verdict has to arrive during the
    read — which is why it travels as the dispatcher's typed error rather than
    as a status the actor chose: the handler is already running, and this is
    the same seam a malformed chunk uses to become a 400.

    ``REQUEST_ENTITY_TOO_LARGE`` rather than ``CONTENT_TOO_LARGE``: the same
    member under both names, but the RFC 9110 spelling only exists from
    Python 3.13 and this package supports 3.11.
    """
    from http import HTTPStatus  # noqa: PLC0415
    from ..router import HTTPException  # noqa: PLC0415
    return HTTPException(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, detail)


def _validate_chunk_ext(ext: bytes) -> None:
    """RFC 9112 §7.1.1::

        chunk-ext      = *( BWS ";" BWS chunk-ext-name [ BWS "=" BWS chunk-ext-val ] )
        chunk-ext-name = token
        chunk-ext-val  = token / quoted-string

    *ext* is the chunk line **from the first ``;``** onward.  Reject a bare
    ``;`` (empty ext-name), a non-token ext-name/val, and control characters
    — all silent-acceptance smuggling vectors before this guard.  A
    quoted-string ext-val is accepted leniently (matched quotes, no bare
    CTLs) since chunk extensions are ignored on receipt.
    """
    for element in ext.split(b';')[1:]:
        element = element.strip(b' \t')          # BWS around the element
        name, eq, val = element.partition(b'=')
        name = name.rstrip(b' \t')
        if not name or any(c not in _TCHAR_SET for c in name):
            raise _bad_request(f'invalid chunk-ext-name {name!r}')
        if eq:
            val = val.strip(b' \t')
            if val[:1] == b'"':
                if len(val) < 2 or not val.endswith(b'"') or any(
                        c < 0x20 and c != 0x09 for c in val):
                    raise _bad_request(f'invalid quoted chunk-ext-val {val!r}')
            elif not val or any(c not in _TCHAR_SET for c in val):
                raise _bad_request(f'invalid chunk-ext-val {val!r}')


# MAL-CHUNK-EXT-64K (CVE-2023-39326 class) — hard bound on any
# single chunk-framing line (chunk-size + chunk-ext, or one trailer field
# line).  Mirrors the BB_HEADER_MAX_LINE default: extensions and trailers
# are ignored on receipt, so nothing legitimate needs more.  Without the
# bound, ``asyncio.StreamReader.readuntil`` hits its own buffer limit first
# and raises ``LimitOverrunError`` — a ValueError the dispatcher's 400 seam
# doesn't catch — surfacing as a 500.
_CHUNK_LINE_MAX = 8192

# RFC 9110 §6.5.1 — fields controlling message framing, routing, request
# modifiers, authentication, or content handling are prohibited in a
# chunked trailer section (SMUG-TRAILER-*).  BlackBull never merges
# trailers into the header section, but silently swallowing these invites
# a front-end that *does* merge them to be desynced through us — reject.
_PROHIBITED_TRAILER_FIELDS = frozenset((
    b'transfer-encoding', b'content-length', b'host', b'content-type',
    b'content-encoding', b'content-range', b'trailer', b'te',
    b'authorization', b'proxy-authorization', b'cookie', b'set-cookie',
    b'cache-control', b'expect', b'max-forwards', b'pragma', b'range',
))


def _parse_chunk_size(line: bytes) -> int:
    """RFC 9112 §7.1.1 — ``chunk-size = 1*HEXDIG`` optionally followed by
    ``chunk-ext``.  Validate the size token strictly (no sign, no ``0x``
    prefix, no ``_``, no stray whitespace) **before** ``int()``, and
    validate the chunk-ext grammar, so malformed framing is rejected rather
    than silently accepted or crashed on.

    The line must be CRLF-terminated: a bare-LF terminator (``5\\n``) is a
    framing violation, so we require the trailing CRLF rather than stripping
    either.  Raises :class:`HTTPException` (400) on any violation; the caller
    marks the body unframeable and closes the connection.
    """
    if not line.endswith(b'\r\n') or line.count(b'\n') != 1:
        raise _bad_request(f'chunk-size line not CRLF-terminated: {line!r}')
    line = line[:-2]
    if b';' in line:
        size_part, _, _ext = line.partition(b';')
        # BWS is tolerated between the size and ';' (RFC 9112 §7.1.1 BWS).
        size_part = size_part.rstrip(b' \t')
        _validate_chunk_ext(line[len(size_part):])
    else:
        # No chunk-ext — the whole line is the size; NO trailing OWS allowed
        # (a bare ``5 \r\n`` is a smuggling vector, not a valid chunk-size).
        size_part = line
    if not size_part or any(c not in _HEXDIG_SET for c in size_part):
        raise _bad_request(f'invalid chunk-size {size_part!r}')
    return int(size_part, 16)


class ProtocolError(Exception):
    """Raised when a WebSocket protocol violation is detected (RFC 6455).

    ``close_code`` is the RFC 6455 §7.4 status code that should appear in
    the CLOSE frame sent to the peer.  Defaults to 1002 (PROTOCOL_ERROR);
    UTF-8 violations use 1007.
    """
    def __init__(self, message: str, close_code: int = 1002):
        super().__init__(message)
        self.close_code = close_code


def _is_valid_close_code(code: int) -> bool:
    """RFC 6455 §7.4 — which close codes may appear on the wire.

    Allowed: 1000–1011 (defined), 3000–4999 (registered + private use).
    Disallowed even though numerically in 1000-range: 1004 (reserved),
    1005 (no status), 1006 (abnormal — TCP-only marker), 1015 (TLS-only
    marker).  1012-1014 are defined but accepting them is fine.
    """
    if code in (1004, 1005, 1006, 1015):
        return False
    if 1000 <= code <= 1015:
        return True
    if 3000 <= code <= 4999:
        return True
    return False


def _parse_close_payload(payload: bytes) -> tuple[int, bool]:
    """Decode a CLOSE frame payload.

    Returns ``(code, ok)`` where ``ok`` is False when the payload violates
    RFC 6455 §5.5.1 — length 1 (truncated code), disallowed code value, or
    non-UTF-8 reason text.  Empty payload is permitted and maps to code
    1000 (NORMAL).
    """
    if not payload:
        return 1000, True
    if len(payload) == 1:
        # RFC §5.5.1: when a Close frame contains a status code, the code
        # MUST be 2 octets; a 1-octet payload is malformed.
        return 1002, False
    code = int.from_bytes(payload[:2], 'big', signed=False)
    if not _is_valid_close_code(code):
        return 1002, False
    if len(payload) > 2:
        try:
            payload[2:].decode('utf-8')
        except UnicodeDecodeError:
            return 1002, False
    return code, True


class AbstractReader(ABC):
    """Protocol-agnostic async byte-source.

    Mirrors ``AbstractWriter`` on the receive side.  Implementations wrap a
    concrete transport so that ``BaseRecipient`` subclasses stay runtime-agnostic.
    """

    @abstractmethod
    async def read(self, n: int) -> bytes: pass

    def has_buffered(self) -> bool:
        """True when bytes are already buffered, so a read need not block.

        The WebSocket control-frame watchdog probes this to decide whether it
        can service a frame without blocking the caller's path.  Default
        ``False`` — a reader that cannot report buffering is treated as
        "nothing available", which only disables proactive servicing, never
        correctness (control frames are still serviced at the next read).
        """
        return False

    def buffered_len(self) -> int:
        """Bytes currently buffered, unconsumed.  Default 0."""
        return 0

    def peek(self, n: int) -> bytes:
        """Up to *n* buffered bytes without consuming them.

        Returns whatever the default :meth:`fill` parked, so detection can
        inspect it; a reader that owns its buffer overrides this to read
        straight out of it.
        """
        buf = self.__dict__.get('_ahead_buf')
        return bytes(buf[:n]) if buf else b''

    async def fill(self, n: int) -> bool:
        """Buffer up to *n* bytes for peeking.  ``False`` if EOF came first.

        This is what lets connection detection choose a protocol without
        eating the bytes it inspected.  A reader that owns its buffer
        overrides this and genuinely consumes nothing.

        The default cannot: it has only :meth:`read`, so it *does* consume,
        and parks what it took in :attr:`_ahead`.  :meth:`take_ahead` hands
        that back to the caller, which restores the stream by wrapping this
        reader in a :class:`PrefixReader`.  Same outcome, one indirection more
        — and it keeps every reader, including test doubles, usable for
        detection without each one reimplementing pushback.
        """
        ahead = self._ahead
        if len(ahead) >= n:
            return True
        # One read, not a loop to exactly *n*.  A plain reader returns whatever
        # has arrived, and asking again for the remainder would block a peer
        # that sent a complete short frame — the shared-port MQTT hang this
        # detection path exists to avoid.  The caller re-peeks and calls again,
        # so it converges without ever waiting for bytes that may not come.
        chunk = await self.read(n - len(ahead))
        if not chunk:
            return False
        ahead += chunk
        return True

    @property
    def _ahead(self) -> bytearray:
        # Lazily attached so subclasses need no cooperating __init__ — several
        # are test doubles, and one that forgot to call super() would fail
        # only during detection.
        buf = self.__dict__.get('_ahead_buf')
        if buf is None:
            buf = self.__dict__['_ahead_buf'] = bytearray()
        return buf

    def take_ahead(self) -> bytes:
        """Bytes this reader consumed while filling, cleared.

        Empty for a reader whose :meth:`fill` truly peeks — which is the whole
        point: the caller wraps only when there is something to replay.
        """
        buf = self.__dict__.get('_ahead_buf')
        if not buf:
            return b''
        out = bytes(buf)
        buf.clear()
        return out

    def at_eof(self) -> bool:
        """Return True once the peer has closed and the buffer is drained.

        Default ``False`` (callers that need EOF detection — e.g. a long-lived
        raw-protocol read loop — should use a reader that overrides this).
        """
        return False

    async def readuntil(self, sep: bytes) -> bytes:
        """Read until *sep* is seen (inclusive).  Default: byte-wise via
        :meth:`read`.  Concrete transport readers override this with the
        stream's native, buffered implementation."""
        buf = bytearray()
        while sep not in buf:
            chunk = await self.read(1)
            if not chunk:
                break
            buf += chunk
        return bytes(buf)

    async def readexactly(self, n: int) -> bytes:
        """Read exactly *n* bytes.  Default: accumulate via :meth:`read`.
        Concrete transport readers override this."""
        buf = bytearray()
        while len(buf) < n:
            chunk = await self.read(n - len(buf))
            if not chunk:
                break
            buf += chunk
        return bytes(buf)

    async def read_head(self, limit: int) -> bytes:
        """One message head — start line, field lines, terminator included.

        Part of the reader contract rather than something the caller sniffs
        for, so a protocol asks for a head the same way whatever is underneath
        it.  A reader that owns its buffer overrides this to find the
        terminator in a single scan and to return without a loop turn when the
        head is already resident; the default below is what a reader with only
        ``readuntil`` can do — one call per line.

        Three outcomes, because the caller answers each differently:

        * a complete head → returned;
        * EOF before a single byte of it → ``b''``, an idle close;
        * EOF part-way through → :class:`IncompleteReadError` carrying the
          partial, which is a truncated request and not an idle close.

        *limit* bounds the whole head (0 disables it).  Passing it is what
        stops an unbounded read; :class:`ReadLimitExceeded` says the budget was
        passed and carries the bytes, and the protocol decides which status
        that becomes.
        """
        buf = bytearray()
        while not buf.endswith(_HEAD_END):
            try:
                line = await self.readuntil(b'\r\n')
            except asyncio.LimitOverrunError as exc:
                # ``asyncio.StreamReader`` enforces a buffer limit of its own
                # and gets there first for a single enormous line.  Same
                # condition, same exception out.  Its buffer is not reachable
                # from here, so ``seen`` is what we accumulated — enough to
                # classify, because a line that long has no CRLF in it.
                raise ReadLimitExceeded(
                    f'stream buffer overflow ({exc.consumed} bytes) '
                    f'while reading the head', bytes(buf)) from exc
            except IncompleteReadError as exc:
                # The budget is checked before the truncation is reported: a
                # peer that overran it and *then* went away overran it, and a
                # reader that let EOF mask the breach would answer differently
                # from one that scans its own buffer.
                self._over_budget(bytes(buf) + exc.partial, limit)
                partial = bytes(buf) + exc.partial
                if not partial:
                    return b''
                raise IncompleteReadError(partial) from None
            if not line:
                # A reader that reports EOF by returning empty rather than by
                # raising — the default ``readuntil`` above does exactly that.
                self._over_budget(bytes(buf), limit)
                if not buf:
                    return b''
                raise IncompleteReadError(bytes(buf))
            buf += line
            self._over_budget(bytes(buf), limit)
        return bytes(buf)

    @staticmethod
    def _over_budget(seen: bytes, limit: int) -> None:
        """Raise if *seen* has passed *limit*.  ``limit=0`` disables the bound.

        One predicate for every exit of :meth:`read_head`, because a budget
        enforced on some of them is not a budget.
        """
        if limit > 0 and len(seen) > limit:
            raise ReadLimitExceeded(f'head exceeds {limit} bytes', seen)


class AsyncioReader(AbstractReader):
    """Adapts an asyncio-compatible stream to ``AbstractReader``.

    Accepts any object exposing ``read()``, ``readuntil()``, and
    ``readexactly()`` — the asyncio StreamReader API — so that test doubles
    such as ``MagicMock`` can be injected without ceremony.

    Pass-through by design: every method delegates to the stream's own native,
    buffered implementation with nothing layered on top.  Detection's pushback
    is the base class's :attr:`~AbstractReader._ahead` / :class:`PrefixReader`
    pair — one mechanism for every reader that cannot truly peek, rather than a
    private copy here.  Only the buffer-inspecting probes below know they are
    sitting on a ``StreamReader``.
    """

    def __init__(self, stream_reader):
        if not (hasattr(stream_reader, 'read') and hasattr(stream_reader, 'readuntil')):
            raise TypeError(
                f"AsyncioReader requires an object with read() and readuntil(), "
                f"got {type(stream_reader)}"
            )
        self._sr = stream_reader

    async def read(self, n: int) -> bytes:
        try:
            return await self._sr.read(n)
        except asyncio.IncompleteReadError as exc:
            raise IncompleteReadError(exc.partial) from exc

    def at_eof(self) -> bool:
        at_eof = getattr(self._sr, 'at_eof', None)
        return bool(at_eof()) if at_eof is not None else False

    async def readuntil(self, sep: bytes) -> bytes:
        try:
            return await self._sr.readuntil(sep)
        except asyncio.IncompleteReadError as exc:
            raise IncompleteReadError(exc.partial) from exc

    async def readexactly(self, n: int) -> bytes:
        try:
            return await self._sr.readexactly(n)
        except asyncio.IncompleteReadError as exc:
            raise IncompleteReadError(exc.partial) from exc

    def has_buffered(self) -> bool:
        # asyncio.StreamReader keeps every byte the transport has delivered
        # in ``_buffer`` until a read consumes it, so "buffer non-empty" is
        # the honest "a read won't block" probe.  A buffer-owning reader
        # exposes ``buffered_len()`` instead.
        if self.__dict__.get('_ahead_buf'):
            return True
        buf = getattr(self._sr, '_buffer', None)
        if buf is not None:
            return bool(buf)
        return getattr(self._sr, 'buffered', 0) > 0

    def buffered_len(self) -> int:
        ahead = len(self.__dict__.get('_ahead_buf') or b'')
        buf = getattr(self._sr, '_buffer', None)
        if buf is not None:
            return ahead + len(buf)
        return ahead + getattr(self._sr, 'buffered', 0)

    def peek(self, n: int) -> bytes:
        # Anything detection parked sits in front of what the stream still
        # holds — same order the bytes arrived in.
        ahead = self.__dict__.get('_ahead_buf')
        if ahead:
            if len(ahead) >= n:
                return bytes(ahead[:n])
            return (bytes(ahead) + self._peek_stream(n))[:n]
        return self._peek_stream(n)

    def _peek_stream(self, n: int) -> bytes:
        buf = getattr(self._sr, '_buffer', None)
        if buf is not None:
            return bytes(buf[:n])
        peek = getattr(self._sr, 'peek', None)
        if peek is not None:
            return bytes(peek()[:n])
        return b''


class PrefixReader(AbstractReader):
    """An :class:`AbstractReader` that replays an already-read *prefix*.

    Connection detection peeks the first bytes of a stream to decide which
    protocol owns it; wrapping the underlying reader in a ``PrefixReader`` hands
    the *still-complete* stream to the protocol that claims it — the peeked bytes
    are served back first, then reads fall through to the underlying reader.

    Used by the decouple-connection-detection refactor so the dispatcher no
    longer consumes protocol-specific bytes on the connection's behalf.  The
    fast native ``readuntil`` / ``readexactly`` of the underlying reader are
    used once the prefix is drained, including the seam case where the separator
    straddles the prefix/underlying boundary.
    """

    def __init__(self, prefix: bytes, reader: AbstractReader) -> None:
        self._buf = bytearray(prefix)
        self._reader = reader

    async def read(self, n: int) -> bytes:
        if self._buf:
            chunk = bytes(self._buf[:n])
            del self._buf[:n]
            return chunk
        return await self._reader.read(n)

    async def readexactly(self, n: int) -> bytes:
        if len(self._buf) >= n:
            chunk = bytes(self._buf[:n])
            del self._buf[:n]
            return chunk
        head = bytes(self._buf)
        self._buf.clear()
        return head + await self._reader.readexactly(n - len(head))

    async def readuntil(self, sep: bytes) -> bytes:
        # Short-circuit: once the prefix is drained (the common keep-alive
        # case), delegate directly to the underlying reader's native,
        # buffered readuntil without any per-call overhead.
        if not self._buf:
            return await self._reader.readuntil(sep)
        idx = self._buf.find(sep)
        if idx != -1:
            end = idx + len(sep)
            chunk = bytes(self._buf[:end])
            del self._buf[:end]
            return chunk
        # Not wholly in the prefix: pull the rest with the underlying reader's
        # native readuntil, then re-resolve against the seam so a separator that
        # straddles the boundary is honoured and any over-read is pushed back.
        head = bytes(self._buf)
        self._buf.clear()
        combined = head + await self._reader.readuntil(sep)
        end = combined.find(sep) + len(sep)
        self._buf[:0] = combined[end:]          # push back over-read (usually none)
        return combined[:end]

    def at_eof(self) -> bool:
        return not self._buf and self._reader.at_eof()

    def has_buffered(self) -> bool:
        return bool(self._buf) or self._reader.has_buffered()

    def buffered_len(self) -> int:
        return len(self._buf) + self._reader.buffered_len()

    def peek(self, n: int) -> bytes:
        return (bytes(self._buf) + self._reader.peek(n))[:n]


# ---------------------------------------------------------------------------
# Fragment reassembly (RFC 6455 §5.4)
# ---------------------------------------------------------------------------

class FragmentAssembler:
    """Accumulates RFC 6455 fragmented frames and signals message completion.

    Feed each data/continuation frame via ``feed()``.  Returns
    ``(message_opcode, full_payload)`` when the final FIN=1 continuation
    arrives; returns ``None`` while still accumulating.

    Raises ``ProtocolError`` on violations:
    - CONTINUATION frame with no fragmentation in progress (§5.4)
    - New TEXT/BINARY opener while a fragmented message is open (§5.4)

    *max_total* bounds the reassembled message; ``0`` disables it.  The
    check runs **before** the append, so the frame that crosses the bound
    is refused rather than accumulated and then regretted — a bound
    enforced after the fact would have already paid for the attack.
    Raises :class:`MessageTooLarge`, which the caller turns into
    CLOSE 1009.  Note this bounds the *compressed* bytes when
    permessage-deflate is in play; the inflated size is bounded
    separately, because only one of the two is knowable here.
    """

    def __init__(self, max_total: int = 0) -> None:
        self._max_total = max_total
        self._opcode: int | None = None
        self._buf: bytearray | None = None
        # Tracks the RSV1 bit of the message-opener frame (RFC 7692: only the
        # first frame of a compressed message carries RSV1=1; continuation
        # frames keep it clear).  Reported back from ``feed()`` so the caller
        # knows whether the assembled bytes need decompression.
        self._compressed: bool = False

    @property
    def in_progress(self) -> bool:
        return self._opcode is not None

    def feed(self, opcode: int, payload: bytes, fin: bool, rsv1: bool = False
             ) -> tuple[int, bytes, bool] | None:
        """Feed one frame; return ``(message_opcode, full_payload, compressed)`` on completion, else ``None``."""
        if opcode == WSOpcode.CONTINUATION:
            if not self.in_progress:
                raise ProtocolError(
                    'CONTINUATION frame received with no fragmentation in progress'
                )
            if rsv1:
                # RFC 7692 §6: RSV1 MUST be clear on continuation frames.
                raise ProtocolError(
                    'CONTINUATION frame with RSV1 set is a protocol violation'
                )
            assert self._buf is not None
            assert self._opcode is not None
            if self._max_total and len(self._buf) + len(payload) > self._max_total:
                raise MessageTooLarge(len(self._buf) + len(payload),
                                      self._max_total)
            self._buf += payload
            if fin:
                result = (self._opcode, bytes(self._buf), self._compressed)
                self._opcode = None
                self._buf = None
                self._compressed = False
                return result
            return None
        else:
            # TEXT or BINARY opener
            if self.in_progress:
                raise ProtocolError(
                    'New data frame received while a fragmented message is in progress'
                )
            if fin:
                return (opcode, payload, rsv1)  # unfragmented — pass through immediately
            # The opening fragment is bounded too.  It is not covered by the
            # continuation check below — that runs on the *next* frame — so
            # without this an over-cap opener would be held until a second
            # frame arrived, and the only thing bounding it would be the frame
            # cap, which is the very substitution this limit exists to stop.
            if self._max_total and len(payload) > self._max_total:
                raise MessageTooLarge(len(payload), self._max_total)
            self._opcode = opcode
            self._buf = bytearray(payload)
            self._compressed = rsv1
            return None


# ---------------------------------------------------------------------------
# Recipient hierarchy
# ---------------------------------------------------------------------------

class BaseRecipient(ABC):
    """Abstract base for ASGI-event receive callables.

    ``__call__`` returns an ASGI event dict appropriate to the protocol:
      - HTTP: ``{'type': 'http.request', 'body': ..., 'more_body': False}``
      - WebSocket: ``{'type': 'websocket.connect'}``,
                   ``{'type': 'websocket.receive', ...}``, or
                   ``{'type': 'websocket.disconnect', ...}``

    The actual byte transport is hidden behind ``AbstractReader`` so the
    recipient logic is decoupled from asyncio internals.
    """

    def __init__(self, reader: AbstractReader | None):
        self._reader = reader

    @abstractmethod
    async def __call__(self) -> dict: pass


class HTTP1Recipient(BaseRecipient):
    """Reads an HTTP/1.1 request body and emits a single ``http.request`` event.

    Body bytes are read lazily on the first ``__call__`` using the
    Content-Length or Transfer-Encoding header of the :class:`Connection` it is
    bound to.  Subsequent calls return ``{'type': 'http.disconnect'}``.
    """

    _reader: AbstractReader  # narrows BaseRecipient._reader from AbstractReader | None

    def __init__(self, reader: AbstractReader, conn: Connection,
                 *, body_timeout: float = 0.0,
                 deadline: ConnectionDeadline | None = None,
                 chunk_size: int | None = None,
                 chunk_max: int | None = None,
                 max_body: int | None = None,
                 min_rate: float | None = None,
                 min_rate_grace: float | None = None):
        super().__init__(reader)
        # Deliver a Content-Length body in slices instead of one giant
        # ``readexactly(content_length)`` allocation.  Reads are up-to-n and
        # transport-paced: each returns whatever the peer has delivered, up to
        # ``chunk_max``, so the slice follows the transport (small for a slow
        # peer, large for a fast one) and no read is ever a latency
        # commitment.  ``chunk_size`` is the fixed slice for the chunked-
        # transfer path.  Both fall back to settings when not injected
        # (direct-instantiation tests pass them explicitly).
        if (chunk_size is None or chunk_max is None or max_body is None
                or min_rate is None or min_rate_grace is None):
            from ..env import get_settings as _get_settings  # noqa: PLC0415
            _s = _get_settings()
            if chunk_size is None:
                chunk_size = _s.body_chunk_size
            if chunk_max is None:
                chunk_max = _s.body_chunk_max
            if max_body is None:
                max_body = _s.max_body_size
            if min_rate is None:
                min_rate = _s.min_body_rate
            if min_rate_grace is None:
                min_rate_grace = _s.min_body_rate_grace
        self._chunk_size = chunk_size
        # Total-body cap, 0 = uncapped.  Enforced on the octets themselves
        # rather than on the declaration, so it holds for a chunked body (which
        # declares nothing) and for a peer that under-declares.  The actor
        # refuses an over-cap ``Content-Length`` at head time; this is what
        # makes the guarantee true without it — a directly-driven recipient, or
        # an external ASGI host, gets the same ceiling.
        self._max_body = max_body
        # Anti-trickle floor, 0 = disabled.  A transport-paced read cannot
        # carry a deadline the way a fixed-size one did — it returns on any
        # arrival, so ``body_timeout`` degrades from "fill a slice in 30 s" to
        # "send *something* every 30 s", which a one-byte drip always meets.
        # A minimum *rate* is what a drip cannot fake (Kestrel's
        # ``MinRequestBodyDataRate``); the grace period is the slow-start
        # allowance so nothing is judged on its first packets.
        self._min_rate = min_rate
        self._min_rate_grace = min_rate_grace
        # A cap of 0 would turn every up-to-n read into b'' (which reads as
        # EOF), so a misconfigured zero falls back to a single usable byte.
        self._chunk_max = max(chunk_max, 1)
        # Body-read deadline.  0 = disabled.  Applied per
        # ``_read_with_timeout`` call — which is per *slice* on both framings,
        # since a chunk larger than ``chunk_size`` is delivered in several.
        # Mirrors nginx ``client_body_timeout`` semantics: each read has the
        # same bound, so a peer must keep making progress, not merely finish.
        #
        # The deadline is rescheduled on the shared
        # :class:`ConnectionDeadline` rather than allocating a fresh
        # ``asyncio.wait_for`` Timeout per chunk.  Per-chunk semantics
        # preserved.
        self._body_timeout = body_timeout
        self._deadline = deadline
        # Everything above belongs to the connection; everything ``bind`` sets
        # belongs to the request.
        self.bind(conn)

    def bind(self, conn: Connection) -> 'HTTP1Recipient':
        """Point this recipient at *conn*, the next request on the connection.

        The reader, chunk size, and deadline are properties of the connection
        and survive; the framing state is re-derived from the new head.  One
        recipient per connection instead of one per request is the same trade
        the sender already makes — safe for HTTP/1.1 because a connection
        dispatches one request at a time, and **not** safe for HTTP/2, whose
        streams are concurrent.

        The split is the whole contract: any per-request field left out of this
        method would leak from request N into request N+1, so new state belongs
        here, not in ``__init__``.
        """
        # We deliberately do **not** retain the Connection: the actor binds this
        # recipient as ``conn._receive`` for lazy ``conn.body()``, so a
        # back-reference here would close a per-request cycle (conn → recipient →
        # conn) reclaimable only by the cyclic GC — the v0.60.0 tail-latency
        # regression. Only the request path is kept (a plain ``str``), purely for
        # the log-cap-hit diagnostics below.
        headers = conn.headers
        self._req_path: str | None = conn.path
        te = headers.get(b'transfer-encoding', b'').strip().lower()
        cl = headers.get(b'content-length', b'')
        if te and te != b'chunked':
            raise NotImplementedError(
                f'Transfer-Encoding "{te.decode()}" is not supported.'
            )
        self._chunked = (te == b'chunked')
        # Remaining Content-Length bytes; counts down as the body streams.
        self._content_length = int(cl) if cl else None
        # Octets still owed by the chunk currently being delivered.  Per
        # request, not per connection: a rebound recipient that inherited a
        # half-read chunk would splice request N's body into request N+1.
        self._chunk_remaining = 0
        self._done = False
        # Body octets delivered for *this* request, against ``_max_body``.
        self._body_seen = 0
        # Seconds this request spent *waiting on the transport* for body
        # octets — the denominator of the delivery rate.  Only the waiting
        # counts: the rate is evidence about the peer, so time the handler
        # spent between reads (writing a chunk to disk, awaiting a database)
        # must never be charged to it.
        self._body_wait = 0.0
        # Rate-window state: the slow-drip judge averages over one
        # grace-period window of waiting time, not the request's whole life.
        # A peer that ran ahead and then stalled must be judged on the stalled
        # window, not on the burst that paid for it (the burst-then-drip shape
        # a cumulative average cannot see).  ``_rate_window_wait`` is the
        # accumulated wait when the current window opened; ``_rate_window_seen``
        # the octets delivered inside it.
        self._rate_window_wait = 0.0
        self._rate_window_seen = 0
        # Set once this body was given up on — over the size cap, or below the
        # minimum rate.  Like a framing violation it ends the connection, for a
        # different reason: the stream is still perfectly framed, but the
        # octets we stopped reading are still arriving.
        self._body_refused = False
        # Set once a chunked-framing violation is detected: the byte stream is
        # now desynced, so the connection MUST close rather than keep-alive
        # (draining would parse smuggled bytes as the next request).
        self.framing_broken = False
        return self

    @property
    def must_close(self) -> bool:
        """This connection cannot carry another request.

        Two causes, one consequence.  A chunked-framing violation leaves the
        byte stream desynced; a body refused for size leaves octets we
        deliberately did not read.  Either way the bytes that follow are the
        peer's to choose, and parsing them as the next request line is the
        request-smuggling shape — so the answer is to close, not to resynchronise.
        """
        return self.framing_broken or self._body_refused

    def needs_drain(self) -> bool:
        """True if a declared request body may still be buffered unread.

        A handler that ignores ``receive`` (e.g. a 404/405 response to a POST)
        leaves the body bytes in the reader; the next keep-alive request would
        then parse them as its request line.  A body-less request (GET, no
        Content-Length, not chunked) never needs draining.

        Kept as the named question it is, for tests and for a directly-driven
        recipient; the actor asks :meth:`after_dispatch` instead, which answers
        this and ``must_close`` in one call.
        """
        if self.framing_broken or self._body_refused:
            # Nothing to preserve: the connection is going away, and draining a
            # refused body would read the very octets the refusal declined
            # (and, for a cap breach, re-raise the 413 on the way).
            return False
        return not self._done and (self._chunked or bool(self._content_length))

    def after_dispatch(self) -> int:
        """What the connection should do now the handler has answered.

        One question, because it is one judgement.  The actor used to ask
        ``must_close`` and then ``needs_drain()`` and combine the two, which
        put the verdict in the caller and left the two predicates free to
        drift apart — the recipient is the object that knows whether the
        message boundary survived, so it should say what follows from that.

        Also one call per request instead of two on the keep-alive path.
        """
        if self.framing_broken or self._body_refused:
            # The bytes after a desync or a refusal are the peer's to choose,
            # and parsing them as the next request line is the smuggling
            # shape.  Answered before the drain, which is exactly what must
            # not happen to them.
            return CONNECTION_MUST_CLOSE
        if not self._done and (self._chunked or bool(self._content_length)):
            return CONNECTION_NEEDS_DRAIN
        return CONNECTION_REUSABLE

    async def drain(self, max_bytes: int) -> bool:
        """Discard any unread request body so the next pipelined request parses
        cleanly.  Returns True if fully drained (or the peer disconnected),
        False if the unread body exceeded *max_bytes* — the caller should then
        close the connection rather than keep it alive.
        """
        # Lazily, once per drain rather than per chunk: ``router`` cannot be
        # imported at module scope here (see :func:`_bad_request`).
        from ..router import HTTPException  # noqa: PLC0415
        drained = 0
        while not self._done:
            try:
                chunk = await self.next_chunk()
            except ClientDisconnected:
                # EOF / body_timeout mid-drain: nothing left to desync, and
                # ``_done`` is now set so the loop would exit anyway.
                return True
            except HTTPException:
                # A body limit tripped while draining.  Reachable when the
                # handler never read an *undeclared* over-cap body (a declared
                # one is refused at the head, before dispatch): the drain reads
                # it instead and ``_account`` raises the 413 here, where there
                # is no request left to answer with it.  Report "could not
                # drain" — the caller closes, which is the same thing a refused
                # body asks for — rather than let the exception reach the
                # connection's generic handler and be logged as a server error.
                self._body_refused = True
                return False
            if chunk is None:
                break
            drained += len(chunk)
            if drained > max_bytes:
                return False
        return True

    def _parse_chunk_size_or_400(self, size_line: bytes) -> int:
        """Parse the chunk-size line, marking the stream unframeable on any
        violation so the actor closes the connection instead of keep-aliving
        a desynced byte stream."""
        try:
            return _parse_chunk_size(size_line)
        except BaseException:
            self.framing_broken = True
            raise

    async def _read_chunk_slice(self) -> bytes:
        """Read at most ``_chunk_size`` octets of the chunk in progress.

        ``chunk-size`` is written by the peer, so reading a whole chunk in one
        ``readexactly`` would let the peer choose how much the server buffers.
        It also defeats backpressure outright rather than merely straining it:
        a read larger than the high-water mark has to reopen the transport the
        mark just paused, because otherwise it would be waiting for bytes its
        own pause is refusing to accept.  Slicing keeps every read under the
        mark, so the pause does its job.  (The Content-Length path is
        transport-paced instead — an up-to-n read never parks on bytes the
        pause is refusing to read, so it needs no such bound.)

        The exact-bytes contract is unchanged: ``readexactly`` (not the up-to-n
        ``read``) still backs every slice, so a chunk split across TCP segments
        cannot return short and silently corrupt the body.
        """
        n = min(self._chunk_remaining, self._chunk_size)
        data = await self._read_with_timeout(self._reader.readexactly(n))
        self._chunk_remaining -= n
        if self._chunk_remaining == 0:
            # RFC 9112 §7.1 — chunk-data is followed by exactly CRLF.  Read
            # those two octets and verify: reading *until* CRLF would swallow
            # trailing spill (SMUG-CHUNK-SPILL) up to the next CRLF, and would
            # tolerate a bare CR/LF terminator.
            term = await self._read_with_timeout(self._reader.readexactly(2))
            if term != b'\r\n':
                self.framing_broken = True
                raise _bad_request(f'chunk-data not CRLF-terminated: {term!r}')
        return data

    async def _read_chunk_line(self) -> bytes:
        """Read one line of chunked framing (chunk-size line or trailer
        line) with a hard length bound.

        Reads to bare LF, not CRLF: a bare-LF-terminated line then returns
        immediately and fails the caller's CRLF check with a 400 instead of
        blocking in ``readuntil`` until the peer gives up
        (SMUG-CHUNK-LF-TERM / SMUG-CHUNK-LF-TRAILER were timeouts, not
        rejections, before this).  Length violations — whether detected by
        our own bound or pre-empted by ``asyncio.StreamReader``'s buffer
        limit (``LimitOverrunError``) — surface as 400 with the stream
        marked unframeable (MAL-CHUNK-EXT-64K).
        """
        try:
            line = await self._read_with_timeout(self._reader.readuntil(b'\n'))
        except asyncio.LimitOverrunError as exc:
            self.framing_broken = True
            log_cap_hit('h1_chunk_line_length',
                        requested=exc.consumed, limit=_CHUNK_LINE_MAX,
                        scope_path=self._req_path,
                        protocol='http1')
            raise _bad_request(
                'chunk framing line exceeds length limit') from None
        if len(line) > _CHUNK_LINE_MAX:
            self.framing_broken = True
            log_cap_hit('h1_chunk_line_length',
                        requested=len(line), limit=_CHUNK_LINE_MAX,
                        scope_path=self._req_path,
                        protocol='http1')
            raise _bad_request('chunk framing line exceeds length limit')
        return line

    def _account(self, chunk: bytes) -> bytes:
        """Weigh *chunk* against the two body limits, giving up if it fails one.

        Every delivered octet passes through here, on both framings, so the
        limits are properties of the recipient rather than of whichever caller
        is driving it.  Both verdicts are permanent for the connection
        (:attr:`must_close`): the peer is still sending a body we have stopped
        reading, so there is no message boundary left to resynchronise on.

        The two failures are different in kind and answer differently.  Too
        large is a *judgement about the request*, which the peer is entitled to
        hear: 413.  Too slow is a judgement about the *peer*, and answering it
        politely would be answering the attack — a trickle is cheap to send and
        expensive to hold, so the connection is abandoned exactly as
        ``body_timeout`` abandons a silent one.
        """
        self._body_seen += len(chunk)
        self._rate_window_seen += len(chunk)
        if self._max_body and self._body_seen > self._max_body:
            self._body_refused = True
            self._done = True
            log_cap_hit('max_body_size',
                        requested=self._body_seen, limit=self._max_body,
                        scope_path=self._req_path, protocol='http1')
            raise _content_too_large(
                f'request body exceeds {self._max_body} bytes')
        # Judged on a grace-period window of waiting time, not the request's
        # whole life: once a window's worth of waiting has passed, the octets
        # delivered inside it must earn their keep at ``min_body_rate``, and
        # the window rolls so the next judgement starts from a clean slate.
        # A peer that kept us waiting with little to show is the slow-drip
        # shape whatever its framing; a burst that ran ahead buys one window,
        # not the whole request.
        if self._min_rate:
            window_wait = self._body_wait - self._rate_window_wait
            if window_wait > self._min_rate_grace:
                if self._rate_window_seen < self._min_rate * window_wait:
                    self._body_refused = True
                    self._done = True
                    log_cap_hit(
                        'min_body_rate',
                        requested=self._rate_window_seen / window_wait,
                        limit=self._min_rate,
                        scope_path=self._req_path, protocol='http1')
                    raise ClientDisconnected()
                # The window earned its keep: roll it forward so the next
                # judgement looks at the next grace period only.
                self._rate_window_wait = self._body_wait
                self._rate_window_seen = 0
        return chunk

    async def _read_with_timeout(self, coro):
        """Run *coro* under the configured body_timeout, if any.

        Also the one place body reads wait, which is why the rate detector's
        clock lives here: the elapsed time it accumulates is transport-wait
        time only, never the handler's own.
        """
        # ``None``, not 0.0, for "not timing": a clock reading is a value, not a
        # flag, and 0.0 is one a monotonic clock is allowed to return.
        t0 = _monotonic() if self._min_rate else None
        try:
            if self._body_timeout > 0 and self._deadline is not None:
                with self._deadline.guard(self._body_timeout):
                    return await coro
            if self._body_timeout > 0:
                # Fallback for direct-instantiation tests that don't pass a
                # ConnectionDeadline.  Preserves per-call semantics; the
                # production hot path takes the deadline-guard branch above.
                return await asyncio.wait_for(coro, timeout=self._body_timeout)
            return await coro
        finally:
            if t0 is not None:
                self._body_wait += _monotonic() - t0

    async def next_chunk(self) -> bytes | None:
        """The next body chunk, or ``None`` once the body is complete.

        The native receive channel: a chunk is the bytes themselves, and the
        end of the body is carried by the *call protocol* rather than by a
        field beside the payload.  ``more_body`` was never information about
        the chunk — it is the channel's state — and every internal consumer
        did the same one thing with it (``if not more_body: break``), so the
        boundary belongs where a Python caller already looks for it.

        ``None``, not ``b''``: an empty body is a real body, the same reason
        :class:`~blackbull.native.NativeResponse` decides presence with
        ``is not None``.  On both framings the sentinel is unambiguous — a
        zero-length chunk *is* the terminator in chunked encoding (RFC 9112
        §7.1), and a Content-Length slice is never empty.

        Asking again past the end keeps answering ``None``.  A peer that
        vanishes mid-body raises :class:`ClientDisconnected` — a truncated
        upload must never read as a complete one — and so does a body-read
        timeout, which is recorded as a cap hit first.
        """
        if self._done:
            return None
        try:
            if self._chunked:
                if self._chunk_remaining:
                    return self._account(await self._read_chunk_slice())
                size_line = await self._read_chunk_line()
                chunk_size = self._parse_chunk_size_or_400(size_line)
                if chunk_size == 0:
                    # RFC 9112 §7.1.2 — last-chunk is followed by an
                    # optional trailer-part and then a final CRLF.  Read
                    # lines until we hit the terminator.  Each line must be
                    # CRLF-terminated (a bare-LF terminator is the same
                    # framing violation as on the chunk-size line), and
                    # RFC 9110 §6.5.1-prohibited fields are rejected.
                    while True:
                        line = await self._read_chunk_line()
                        if line == b'\r\n':
                            break
                        if not line.endswith(b'\r\n'):
                            self.framing_broken = True
                            raise _bad_request(
                                f'trailer line not CRLF-terminated: '
                                f'{line[-8:]!r}')
                        name = line.split(b':', 1)[0].strip(b' \t').lower()
                        if name in _PROHIBITED_TRAILER_FIELDS:
                            self.framing_broken = True
                            raise _bad_request(
                                f'prohibited trailer field {name!r} '
                                f'(RFC 9110 §6.5.1)')
                    self._done = True
                    return None
                self._chunk_remaining = chunk_size
                return self._account(await self._read_chunk_slice())
            else:
                # Stream the Content-Length body in up-to-n slices so a large
                # upload is delivered as several ``http.request`` events
                # (``more_body: True`` until exhausted) rather than one giant
                # allocation.  Each read returns whatever the peer has
                # delivered, up to ``_chunk_max``, and never parks on bytes
                # the transport has not produced — no read is a latency
                # commitment ``body_timeout`` might not deliver.  ``b''`` from
                # the reader means the peer is gone (a reader parks rather
                # than returning short when more may come), so a body that is
                # not yet spent is a truncated upload, never a complete one.
                if self._content_length:
                    n = min(self._content_length, self._chunk_max)
                    body = await self._read_with_timeout(self._reader.read(n))
                    # Gate on the *length*, not truthiness.  Forward progress
                    # is now the reader's return value rather than a constant
                    # ``n``, so "zero bytes came back" is the only thing that
                    # ends the loop — and a zero-length result that is somehow
                    # truthy would decrement by nothing and spin forever.  For
                    # ``bytes`` the two tests are identical; the difference is
                    # that this one cannot be defeated by a reader whose reads
                    # do not return ``bytes``.
                    if not len(body):
                        raise IncompleteReadError(b'')
                    self._content_length -= len(body)
                    if self._content_length == 0:
                        self._done = True
                    return self._account(body)
                self._done = True
                return None

        except (asyncio.TimeoutError, TimeoutError):
            # body_timeout exceeded — distinguish from EOF mid-body so
            # operators see the cap hit recorded (the request still
            # surfaces as HTTP_DISCONNECT to the ASGI app).
            log_cap_hit('body_timeout',
                        requested=self._body_timeout,
                        limit=self._body_timeout,
                        scope_path=self._req_path,
                        protocol='http1')
            self._done = True
            raise ClientDisconnected() from None
        except IncompleteReadError:
            # EOF mid-body — not a cap hit (peer disappeared).  The handler
            # must not read a truncated upload as a whole one; server closes
            # on return; no synthetic 408.
            self._done = True
            raise ClientDisconnected() from None

    async def __call__(self) -> dict:
        """The ASGI receive channel: the same body, encoded as event dicts.

        The compat surface, and the only place the ``http.request`` dict is
        built.  It costs one dict per chunk and is paid for by the caller that
        wanted the ASGI encoding — a full-form handler calling ``receive()``,
        or an external host.  ``Connection.body()`` / ``stream()`` take
        :meth:`next_chunk` and pay nothing.

        The event sequence is unchanged: ``more_body`` is recovered from
        ``_done``, which :meth:`next_chunk` has just set, so a Content-Length
        body still ends on its last data event while a chunked body still
        ends on a separate empty one.
        """
        if self._done:
            return {'type': ASGIEvent.HTTP_DISCONNECT}
        try:
            chunk = await self.next_chunk()
        except ClientDisconnected:
            return {'type': ASGIEvent.HTTP_DISCONNECT}
        if chunk is None:
            return {'type': ASGIEvent.HTTP_REQUEST, 'body': b'',
                    'more_body': False}
        return {'type': ASGIEvent.HTTP_REQUEST, 'body': chunk,
                'more_body': not self._done}


class HTTP2Recipient(BaseRecipient):
    """Delivers HTTP/2 DATA frames as ASGI ``http.request`` events.

    The server loop feeds frames via ``put_DATAFrame()`` (non-blocking).
    The ASGI app calls ``__call__()`` which suspends until an event is available,
    hiding the concurrency from both sides.

    For GET-style requests (END_STREAM on HEADERS, no DATA frames), the caller
    invokes :meth:`mark_end_of_stream_on_headers` instead of pre-queuing an empty
    ``http.request`` event.  The Queue is then never allocated — the empty event
    is synthesized lazily in :meth:`__call__` only if the handler reads it.

    **Consume-based inbound flow control**: when constructed with
    a ``credit_callback``, WINDOW_UPDATE credit for a DATA frame is replayed
    through the callback when the app *pops* the event — not when the frame is
    enqueued.  A stalled handler then stops crediting, the peer's window
    closes, and the peer back-pressures instead of overflowing a frame-count
    queue into RST_STREAM(ENHANCE_YOUR_CALM).  In this mode the queue is
    bounded by ``credit_budget`` bytes (the advertised inbound window — a
    conformant peer cannot exceed it) plus a generous frame-count abuse cap;
    ``put_DATAFrame`` returning ``False`` therefore means the peer overran the
    closed window or dribbled degenerate frames, and the RST is a true abuse
    backstop.  Without a callback the historical bounded-queue,
    credit-at-enqueue behaviour is preserved (push streams, direct test use).
    """

    def __init__(self, frame: FrameBase | None = None,
                 queue_depth: int = _HTTP2_STREAM_QUEUE_DEPTH,
                 credit_callback: Optional[
                     Callable[[int], Awaitable[None]]] = None,
                 credit_budget: int = DEFAULT_INITIAL_WINDOW_SIZE,
                 max_body: int | None = None,
                 min_rate: float | None = None,
                 min_rate_grace: float | None = None):
        super().__init__(None)
        self._queue: asyncio.Queue | None = None
        self._queue_depth = queue_depth
        self._credit_cb = credit_callback
        self._credit_budget = credit_budget
        # Bytes enqueued but not yet consumed — and therefore not yet credited
        # back to the peer.  For a conformant peer this can never exceed
        # ``credit_budget``: the un-credited bytes ARE the closed part of the
        # window the peer must respect.
        self._uncredited: int = 0
        # When True, HEADERS carried END_STREAM — request has no body.
        # __call__() returns one empty http.request event without allocating a queue.
        self._end_of_stream_on_headers: bool = False
        # Set once the synthetic empty event has been delivered.
        self._initial_consumed: bool = False
        # Native-channel end marker.  Read by ``next_chunk`` only: ``__call__``
        # deliberately does not consult it, so a full-form handler calling
        # ``receive()`` past END_STREAM still blocks for the disconnect event
        # exactly as it did before, rather than being handed a synthetic one.
        self._done: bool = False
        if max_body is None or min_rate is None or min_rate_grace is None:
            # Fallback for a directly-instantiated recipient (tests, and any
            # caller that is not the actor).  One recipient is built *per
            # stream*, so the production path must never take this branch: a
            # function-level relative import is resolved through
            # ``importlib._bootstrap`` on every execution, and at one stream
            # per request that is per-request work.  ``HTTP2Actor`` reads these
            # once per connection and passes them in.
            from ..env import get_settings as _get_settings  # noqa: PLC0415
            _s = _get_settings()
            if max_body is None:
                max_body = _s.max_body_size
            if min_rate is None:
                min_rate = _s.min_body_rate
            if min_rate_grace is None:
                min_rate_grace = _s.min_body_rate_grace
        # The two body limits, shared with HTTP/1.1.  The actor answers a
        # *declared* over-cap body with 413 at HEADERS; these cover what a
        # declaration cannot — a body sent without ``content-length``, and a
        # stream that dribbles.  Both are refusals of the frame, which the
        # actor answers with RST_STREAM.
        self._max_body = max_body
        self._min_rate = min_rate
        self._min_rate_grace = min_rate_grace
        self._body_seen = 0
        #: Wall-clock time the current rate window opened, or ``None`` before
        #: the first DATA frame.  Wall clock is the honest denominator here,
        #: unlike HTTP/1.1: DATA arrives whether or not the handler is reading,
        #: so elapsed time is the peer's alone.  ``None`` rather than 0.0 — a
        #: clock reading is a value, and 0.0 is one a monotonic clock may
        #: legitimately return.
        self._rate_window_start: float | None = None
        #: Octets delivered inside the current grace-period window; the rate
        #: judge averages over this window, not the stream's whole life, so a
        #: burst that ran ahead cannot shelter a subsequent stall.
        self._rate_window_seen = 0
        #: The peer was ever blocked by our own closed inbound window — its
        #: delivery rate is then partly our doing, so it is not judged.
        self._was_window_stalled = False
        if isinstance(frame, Data):
            self.put_DATAFrame(frame)

    @property
    def credits_on_consume(self) -> bool:
        """True when WINDOW_UPDATE credit is replayed at consume-time.

        The actor must then NOT credit at enqueue — the recipient's
        ``credit_callback`` owns the replay.
        """
        return self._credit_cb is not None

    def _ensure_queue(self) -> asyncio.Queue:
        if self._queue is None:
            # Consume-crediting mode enforces its own bounds (byte budget +
            # frame-count abuse cap) in put_DATAFrame, so the queue itself is
            # unbounded — put_disconnect can then always deliver.  Legacy mode
            # keeps the historical frame-count maxsize.
            maxsize = 0 if self._credit_cb is not None else self._queue_depth
            self._queue = asyncio.Queue(maxsize=maxsize)
        return self._queue

    def mark_end_of_stream_on_headers(self) -> None:
        """Mark this stream as ended on HEADERS (no body to deliver).

        Replaces ``put_event({type: http.request, body: b'', more_body: False})``
        with a flag — saves one ``asyncio.Queue`` allocation per body-less request.
        """
        self._end_of_stream_on_headers = True

    @staticmethod
    def make_item(frame: Data) -> tuple[bytes, bool]:
        """The queue's payload: ``(chunk, end_of_stream)``.

        The pair the two channels need, and nothing else — ``__call__``
        re-encodes it as an ASGI event, :meth:`next_chunk` hands the bytes
        straight over.  Building the dict here charged every H2 body reader
        for the encoding, including the ones that never read it.

        ``end_stream`` is coerced: the frame carries the raw flag bit
        (``DataFrameFlags.END_STREAM & flags``, an ``int``), and the queue
        item is a value both channels read directly, so it holds the answer
        rather than the wire encoding of it.
        """
        return frame.payload, bool(frame.end_stream)

    def _body_limits_refuse(self, nbytes: int) -> bool:
        """True when this arrival breaks a body limit and must be refused.

        The HTTP/2 half of the two body defences.  Both are judged on arrival
        rather than on consumption because DATA lands whether or not the
        handler is reading — the queue grows either way, so the memory this
        bounds is spent before anyone asks for it.

        The rate judgement is skipped once the peer has been back-pressured by
        our own inbound window: below-rate delivery is then a consequence of
        our flow control, and blaming the peer for obeying it would turn a slow
        *handler* into a reset stream.  A trickle never fills the window, which
        is exactly why the exemption does not shelter one.
        """
        self._body_seen += nbytes
        if self._max_body and self._body_seen > self._max_body:
            logger.warning('HTTP2Recipient body over BB_MAX_BODY_SIZE — '
                           'refusing DATA frame')
            log_cap_hit('max_body_size',
                        requested=self._body_seen, limit=self._max_body,
                        protocol='http2')
            return True
        if not self._min_rate:
            return False
        now = _monotonic()
        if self._rate_window_start is None:
            self._rate_window_start = now
            self._rate_window_seen = nbytes
        else:
            self._rate_window_seen += nbytes
            if (self._credit_cb is not None
                    and self._uncredited + nbytes >= self._credit_budget):
                # This arrival exhausts the peer's inbound window: from here on
                # it sends only what we credit back, so its pace is ours to
                # answer for.  Observed as the window *closes*, not while it is
                # closed — the peer's next frame can only arrive after a replay
                # has reopened it, by which point the balance no longer shows
                # the stall.
                self._was_window_stalled = True
            elapsed = now - self._rate_window_start
            if elapsed > self._min_rate_grace:
                if (not self._was_window_stalled
                        and self._rate_window_seen < self._min_rate * elapsed):
                    logger.warning(
                        'HTTP2Recipient body below BB_MIN_BODY_RATE — '
                        'refusing DATA frame')
                    log_cap_hit('min_body_rate',
                                requested=self._rate_window_seen / elapsed,
                                limit=self._min_rate, protocol='http2')
                    return True
                # The window earned its keep: roll it forward so the next
                # judgement looks at the next grace period only.
                self._rate_window_start = now
                self._rate_window_seen = 0
        return False

    def put_DATAFrame(self, frame: Data) -> bool:
        """Enqueue a DATA frame event.  Returns False when the frame must be
        refused (the caller answers RST_STREAM): queue full in legacy mode;
        inbound-window overrun, a tiny-frame flood, or a body limit
        (``BB_MAX_BODY_SIZE`` / ``BB_MIN_BODY_RATE``) in consume-crediting mode.
        """
        if self._body_limits_refuse(len(frame.payload)):
            return False
        if self._credit_cb is not None:
            # Flow-control debit is the full frame length including padding
            # (RFC 9113 §6.9.1) — credit must mirror it exactly.
            fc_len = frame.length
            if self._uncredited + fc_len > self._credit_budget:
                # The peer kept sending past the advertised inbound window it
                # was never credited for — abuse, since a conformant peer is
                # back-pressured by the closing window well before this.
                logger.warning(
                    'HTTP2Recipient inbound window overrun — refusing DATA frame')
                log_cap_hit('h2_inbound_window_budget',
                            requested=self._uncredited + fc_len,
                            limit=self._credit_budget,
                            protocol='http2')
                return False
            queue = self._ensure_queue()
            event_cap = self._queue_depth * _EVENT_CAP_MULTIPLIER
            if queue.qsize() >= event_cap:
                # Zero/tiny-frame flood — invisible to the byte budget; see
                # _EVENT_CAP_MULTIPLIER.
                logger.warning(
                    'HTTP2Recipient event-count cap hit — dropping DATA frame')
                log_cap_hit('stream_queue_depth',
                            requested=queue.qsize() + 1,
                            limit=event_cap,
                            protocol='http2')
                return False
            queue.put_nowait((self.make_item(frame), fc_len))
            self._uncredited += fc_len
            return True
        try:
            self._ensure_queue().put_nowait((self.make_item(frame), 0))
            return True
        except asyncio.QueueFull:
            logger.warning('HTTP2Recipient queue full on stream — dropping DATA frame')
            log_cap_hit('stream_queue_depth',
                        requested=self._queue_depth + 1,
                        limit=self._queue_depth,
                        protocol='http2')
            return False

    def put_end_of_stream(self) -> bool:
        """Enqueue a clean, empty end-of-body.

        The trailers case (RFC 9113 §8.1): a second HEADERS on an open request
        stream ends the body without carrying any.  Enqueues the native pair —
        building an ``http.request`` dict here only to translate it back one
        line later was the last request-dict producer on the native path.
        """
        return self._put_item((b'', True))

    def _put_item(self, item) -> bool:
        """Enqueue a native queue item.  False when the queue is full."""
        try:
            self._ensure_queue().put_nowait((item, 0))
            return True
        except asyncio.QueueFull:
            logger.warning('HTTP2Recipient queue full on stream — dropping %r', item)
            log_cap_hit('stream_queue_depth',
                        requested=self._queue_depth + 1,
                        limit=self._queue_depth,
                        protocol='http2')
            return False

    def put_disconnect(self) -> None:
        """Unblock a waiting __call__() with an http.disconnect event.

        Skipped when end-of-stream-on-headers has been delivered and no queue
        was ever created — no consumer can be waiting.
        """
        if (self._queue is None
                and self._end_of_stream_on_headers
                and self._initial_consumed):
            return
        try:
            self._ensure_queue().put_nowait((_H2_DISCONNECT, 0))
        except asyncio.QueueFull:
            # If the queue is completely full the app task is hopelessly behind;
            # TaskGroup cancellation will clean up the stream regardless.
            # (Unreachable in consume-crediting mode — that queue is unbounded.)
            logger.warning('HTTP2Recipient: could not deliver http.disconnect — queue full')

    def take_uncredited(self) -> int:
        """Return and clear the un-consumed credit balance.

        Bytes enqueued but never popped by the app (a handler that finished —
        or was RST — without draining its body).  The actor replays this to
        the CONNECTION window when the stream is released, otherwise the
        shared window leaks shut for every later stream; the stream-level
        window is moot once the stream closes (RFC 9113 §5.1).
        """
        n = self._uncredited
        self._uncredited = 0
        return n

    async def __call__(self) -> dict:
        # Fast path: END_STREAM on HEADERS and no body — synthesize the empty
        # http.request event without allocating a queue.  Checked even when a
        # queue exists: put_disconnect() may have raced ahead of the app's
        # first read (connection closed right after the request), and the
        # stream still ended cleanly at HEADERS — the complete (empty) body
        # must be delivered before any disconnect event, otherwise a
        # body-reading handler on a body-less request (QUERY, POST with
        # END_STREAM on HEADERS) sees a spurious client disconnect.
        if self._end_of_stream_on_headers and not self._initial_consumed:
            self._initial_consumed = True
            self._done = True
            return {'type': ASGIEvent.HTTP_REQUEST, 'body': b'', 'more_body': False}
        item = await self._take()
        if item is _H2_DISCONNECT:
            self._done = True
            return {'type': ASGIEvent.HTTP_DISCONNECT}
        payload, end_stream = item
        # Set, never read, on this channel: ``__call__`` past END_STREAM keeps
        # waiting for the disconnect event exactly as it did before, but the
        # end marker has to be shared or a later ``next_chunk`` would block on
        # a queue nothing will feed again.
        if end_stream:
            self._done = True
        return {'type': ASGIEvent.HTTP_REQUEST, 'body': payload,
                'more_body': not end_stream}

    async def _take(self):
        """Pop the next queue item, replaying consume-time flow-control credit.

        Shared by both channels so the credit contract cannot drift between
        them: the peer is credited when the app *pops*, whichever channel it
        pops through.
        """
        item, credit = await self._ensure_queue().get()
        if credit and self._credit_cb is not None:
            # Decrement before the (interruptible) send so a racing
            # take_uncredited() can never double-credit; worst case a
            # cancellation mid-send under-credits by one frame.
            self._uncredited -= credit
            if (self._was_window_stalled
                    and self._uncredited < self._credit_budget):
                # The window is open again, so the peer's pace is its own once
                # more.  The exemption has to end here: it marks an *interval*
                # we back-pressured, and a flag that only ever turns on retires
                # the rate detector for the rest of the stream — one
                # window-filling burst would buy a peer an unlimited drip.
                # The rate window restarts rather than resumes, so the
                # exempted interval is not averaged into the next judgement.
                self._was_window_stalled = False
                self._rate_window_start = None
                self._rate_window_seen = 0
            try:
                await self._credit_cb(credit)
            except Exception:
                # Connection closing/gone — credit no longer matters; the
                # disconnect event is the authoritative teardown signal.
                logger.debug('consume-time WINDOW_UPDATE replay failed',
                             exc_info=True)
        return item

    async def next_chunk(self) -> bytes | None:
        """The next body chunk, or ``None`` once the stream has ended.

        The H2 half of the native receive channel — same contract as
        :meth:`HTTP1Recipient.next_chunk`, so ``Connection.body()`` /
        ``stream()`` read one protocol and get both.
        """
        if self._end_of_stream_on_headers and not self._initial_consumed:
            self._initial_consumed = True
            self._done = True
            return None
        if self._done:
            return None
        item = await self._take()
        if item is _H2_DISCONNECT:
            self._done = True
            raise ClientDisconnected()
        payload, end_stream = item
        if end_stream:
            self._done = True
        return payload


class WebSocketRecipient(BaseRecipient):
    """Reads WebSocket frames and emits ASGI ``websocket.*`` events.

    First call returns ``{'type': 'websocket.connect'}``.  Subsequent calls
    read the next frame from the transport:
      - Text frame   → ``{'type': 'websocket.receive', 'text': ..., 'bytes': None}``
      - Binary frame → ``{'type': 'websocket.receive', 'text': None, 'bytes': ...}``
      - Close frame  → ``{'type': 'websocket.disconnect', 'code': 1000}``
      - Ping frame   → sends Pong immediately, then reads the next frame
      - Pong frame   → silently dropped, reads the next frame

    Ping/pong handling requires write access to the transport, so the raw
    writer is stored alongside the reader.

    **Two read modes, selected by ``ws_queue_depth``.**

    ``0`` (default) — *inline*.  Frames are read in the app's own task, only
    when it calls ``receive()``.  There is no background task and no queue, so
    a message costs no handoff.  This is the difference between WebSocket's
    4.09 loop touches/req and HTTP/1.1's 2.06: read-ahead is exactly one extra
    future plus one extra ``call_soon`` per message.

    ``> 0`` — *eager*.  A background task reads ahead into a bounded queue of
    that depth.  Costs the handoff, and buys read-ahead: control frames are
    serviced while the handler is busy, so a PING is answered even between
    ``receive()`` calls, and up to *depth* messages buffer under a slow app.

    Both modes deliver an identical *ASGI* event sequence to the app; only the
    timing of control-frame servicing and the existence of buffering differ.
    Inline mode still answers PING and echoes CLOSE per RFC 6455 §5.5 — it does
    so when the app drives the next read.  RFC 6455 §5.5.2 permits a delayed
    PONG, which is what makes inline mode conformant.

    The one thing that *can* tell the modes apart is the ``websocket_message``
    Level B event, which fires when the server reads a message rather than when
    the app consumes it — a handler that never calls ``receive()`` must still
    produce it.  A registered listener therefore forces eager mode; see
    :meth:`_read_ahead_observed`.
    """

    # Hard cap on the declared payload length of a single inbound
    # WebSocket frame.  RFC 6455 §5.2 allows up to 2**63 - 1, which an
    # adversary post-handshake can use to OOM the server before any
    # body bytes arrive (``read_payload`` would attempt to buffer the
    # full declared length).  ``MESSAGE_TOO_BIG`` (1009) is the
    # RFC 6455 §7.4.1 close code.
    #
    # Default: 64 MiB — comfortably above the largest frame the
    # Autobahn|Testsuite sends (16 MiB, case 9.1.6) while still bounding
    # per-connection memory.  A 1 MiB cap regresses the Autobahn 9.x
    # cases, which is why the default is this high.
    # Override per-deployment via ``BB_WS_MAX_FRAME_PAYLOAD`` for
    # stricter (or looser) exposure than the default.
    _MAX_FRAME_PAYLOAD: int = 64 * 1024 * 1024

    # Fallback for the message bound, mirroring ``_MAX_FRAME_PAYLOAD``.
    # 16 MiB is the largest message the Autobahn|Testsuite sends (9.1.6
    # text / 9.2.6 binary), so the suite passes on shipped defaults.
    # ``BB_WS_MAX_MESSAGE_SIZE`` carries the full rationale.
    _MAX_MESSAGE_SIZE: int = 16 * 1024 * 1024

    def __init__(self, reader: AbstractReader, writer: AbstractWriter, *,
                 require_masked: bool = True,
                 dispatcher: EventDispatcher | None = None,
                 conn: Connection | None = None,
                 ws_queue_depth: int = _WS_READ_INLINE,
                 decompressor=None,
                 max_frame_payload: int | None = None,
                 max_message_size: int | None = None,
                 on_message: Callable[[dict], Awaitable[None]] | None = None,
                 read_ahead_needed: Callable[[], bool] | None = None,
                 ws_idle_timeout: float | None = None,
                 ws_pong_timeout: float | None = None):
        super().__init__(reader)
        self._writer = writer
        self._connect_sent = False
        # Resolution order for the cap:
        #  1. explicit ``max_frame_payload=`` constructor arg (tests + power users)
        #  2. ``BB_WS_MAX_FRAME_PAYLOAD`` env var via Settings
        #  3. class default (``_MAX_FRAME_PAYLOAD``)
        # Late import keeps ``recipient`` importable without bringing in the
        # full settings stack — useful for tests that drive the recipient
        # directly without a Settings populated.
        if max_frame_payload is not None:
            self._max_frame_payload: int = max_frame_payload
        else:
            try:
                from ..env import get_settings  # noqa: PLC0415
                self._max_frame_payload = get_settings().ws_max_frame_payload
            except Exception:
                self._max_frame_payload = self._MAX_FRAME_PAYLOAD
        # Same three-step resolution for the message bound.
        if max_message_size is not None:
            self._max_message_size: int = max_message_size
        else:
            try:
                from ..env import get_settings  # noqa: PLC0415
                self._max_message_size = get_settings().ws_max_message_size
            except Exception:
                self._max_message_size = self._MAX_MESSAGE_SIZE
        self._assembler = FragmentAssembler(max_total=self._max_message_size)
        # Per-connection control-frame meter (see _read_step).  Built here
        # rather than shared, because the budget is what one peer may spend.
        try:
            from ..env import get_settings  # noqa: PLC0415
            _s = get_settings()
            self._control_meter = RateWindow(_s.frame_rate_limit,
                                             _s.frame_rate_window)
        except Exception:
            self._control_meter = RateWindow(20, 1.0)
        # Server-side: client frames MUST be masked (RFC 6455 §5.1).  Client-side:
        # server frames MUST NOT be masked, so the recipient must not raise when
        # they aren't.  When ``require_masked`` is False, outgoing PONG frames
        # generated by this recipient also need masking, since masking is
        # symmetric: whoever requires masking *in* must not mask *out*.
        self._require_masked = require_masked
        self._dispatcher = dispatcher
        self._conn = conn
        self._ws_queue_depth = ws_queue_depth
        self._event_queue: asyncio.Queue | None = None
        self._reader_task: asyncio.Task | None = None
        # Inline mode's buffer.  Holds at most one item: the inline driver
        # stops as soon as a frame produces something to deliver, so this is a
        # handoff slot rather than a queue — the bounded read-ahead the depth
        # knob describes only exists in eager mode.
        self._pending: deque = deque()
        # Set once the read side is finished (CLOSE, unknown opcode, EOF, or a
        # protocol error).  Stops the inline driver from touching a dead
        # transport after the terminal event has been handed to the app.
        self._read_finished = False
        # Canonical post-terminal behaviour, identical in both modes: once the
        # terminal event has been handed to the app, receive() keeps answering
        # a disconnect (with the last terminal close code) instead of blocking
        # forever.  ``_terminal_code`` is that code; ``_terminal_delivered``
        # marks the handoff.
        self._terminal_code: int | None = None
        self._terminal_delivered = False
        # When permessage-deflate is negotiated, an
        # :class:`InboundDecompressor` is supplied here.  None means
        # compression is disabled for this connection and any inbound RSV1=1
        # frame is treated as a protocol violation (handled by the read loop).
        self._decompressor = decompressor
        # Read-time emit adapter for the ``websocket_message`` Level B event
        # (server path — the actor wires this to its aggregator).  The
        # dispatcher/conn pair is the equivalent for direct-recipient drives;
        # the event fires when the message is READ, in every mode.
        self._on_message = on_message
        self._read_ahead_needed = read_ahead_needed
        # Wire-ownership coordination (design A'): exactly one of {inline
        # receive, reader task, watchdog servicing} drives the wire at a
        # time.  ``_reading`` is set while the app's own receive() drives it;
        # ``_servicing`` while control-frame servicing reads fully-buffered
        # frames.  The reader task and the watchdog both yield on these.
        self._reading = False
        self._servicing = False
        # True once a control frame (CLOSE/PING/PONG) has been observed on
        # this connection — either read or peeked.  Gates the per-message
        # send/receive watchdog work: before any control frame (and with no
        # ``websocket_message`` listener) that work is pure overhead, so an
        # echo workload pays one bool check per message instead.
        self._saw_control_frame = False
        # Refreshed once per receive cycle from ``read_ahead_needed`` — the
        # hot path reads this plain attr instead of calling the predicate
        # per frame (it only changes when listeners are registered, which the
        # aggregator gen-caches).  ``(ra is None) or ra()``: the direct path
        # has no predicate, so "listeners present" is vacuously True there.
        self._listeners = False
        # Design A' (deferred reader): a listener is registered (read-ahead
        # "needed") but the reader task has not been started yet.  Set at
        # connect; cleared when the idle watchdog starts the reader.
        self._deferred_pending = False
        # Idle watchdog — created lazily on the first touch() so constructing
        # a recipient never requires a running loop.
        self._watchdog: WsIdleWatchdog | None = None
        self._closed = False
        # -- liveness (RFC 6455 §5.5.2) -----------------------------------
        # The *time* column for a WebSocket connection.  An idle WebSocket is
        # normal — a subscription channel pushes nothing until something
        # happens — so idleness alone cannot end it; the peer is asked.  Same
        # purpose, same axis and same defaults as HTTP/2's probe.
        #
        # **Off unless a caller asks for it**, rather than read from Settings
        # here.  This class is the read side of *both* roles: the server
        # binding and the two bundled clients construct it.  Defaulting it on
        # would silently make a client probe the server it connected to —
        # traffic nobody asked for, on the surface the attack-surface audit
        # records as unaudited (§9).  ``RecipientFactory.websocket`` is the
        # server's entry point and is where the Settings values are read, so
        # the policy has exactly one owner.
        self._ws_idle_timeout: float = ws_idle_timeout or 0.0
        self._ws_pong_timeout: float = ws_pong_timeout or 30.0
        # Inbound arrival is recorded as a **counter**, not a timestamp: the
        # receive path is the hot path, and Sprint 104 was spent taking
        # per-message clock reads out of it.  The tick callback — once per
        # idle connection per scanner tick — turns the counter into a time.
        self._inbound_seq: int = 0
        self._seq_at_last_check: int = 0
        self._last_inbound_at: float = 0.0
        self._probe_sent_at: float | None = None

    @property
    def terminal_code(self) -> int | None:
        """The RFC 6455 §7.4 close code, once the read side has finished.

        The single record of how this connection ended: the actor used to keep
        its own copy by intercepting every event to look for a disconnect, and
        two records of one fact is one place for them to disagree.
        """
        return self._terminal_code

    async def _emit(self, item) -> None:
        """Hand one ASGI event (or an exception to re-raise app-side) to the app.

        The only place the two read modes diverge.  Eager mode pushes through
        the bounded queue, which is what applies backpressure to a fast peer;
        inline mode drops it in the handoff slot, where the caller one frame up
        the stack is already waiting for it.

        The terminal code is recorded here — from a disconnect event, a
        :class:`ProtocolError`, or any other exception — so a receive() past
        the terminal event can keep answering a disconnect with the same code.
        """
        if isinstance(item, ProtocolError):
            self._terminal_code = item.close_code
        elif isinstance(item, Exception):
            self._terminal_code = WSCloseCode.ABNORMAL
        if self._event_queue is not None:
            await self._event_queue.put(item)
        else:
            self._pending.append(item)

    async def _read_step(self) -> bool:
        """Read and process exactly one frame.  True ⇒ the read side is done.

        Anything to be delivered goes through :meth:`_emit`; a frame that
        produces nothing (an incomplete fragment, a PING, an unsolicited PONG)
        emits nothing and returns False, so whichever driver is running simply
        reads again.  Keeping every RFC decision here means the two modes
        cannot drift apart.
        """
        _CONTROL_OPS = (WSOpcode.CLOSE, WSOpcode.PING, WSOpcode.PONG)
        h = await read_frame_header(self._reader)
        # Liveness, at the cost of one integer add: the peer sent something,
        # so it is alive.  Deliberately not a ``loop.time()`` — see the
        # counter's comment in ``__init__``.
        self._inbound_seq += 1
        self._probe_sent_at = None

        # RFC 6455 §5.5 — control frames MUST have payload ≤125 and
        # MUST NOT be fragmented.  Reject without reading the body.
        if h.opcode in _CONTROL_OPS:
            if not h.fin:
                raise ProtocolError('fragmented control frame')
            if h.length > 125:
                raise ProtocolError(
                    f'control frame payload {h.length} > 125')
            # A control frame is at most 125 bytes and obliges an answer —
            # a PING costs a PONG write.  No byte budget can see a flood of
            # them, so the count is metered instead (the same mechanism
            # HTTP/2 uses for PING and SETTINGS).  Checked before the
            # payload is read: the answer to too many is to stop, not to
            # keep reading them faster.
            if self._control_meter.hit():
                log_cap_hit('frame_rate',
                            requested=self._control_meter.count,
                            limit=self._control_meter.limit,
                            scope_path=self._conn.path if self._conn else None,
                            protocol='ws')
                raise ProtocolError(
                    f'control frame rate limit exceeded '
                    f'({self._control_meter.count} in '
                    f'{self._control_meter.window}s)',
                    close_code=WSCloseCode.POLICY_VIOLATION)

        # RFC 6455 §5.2 — reserved RSV bits MUST be 0 unless an
        # extension defining them was negotiated in the handshake.
        # RSV1 is owned by permessage-deflate (RFC 7692); RSV2 / RSV3
        # are not defined by any extension we negotiate, so they are
        # always a protocol error.  RSV1 on a control frame is
        # likewise always a violation per RFC 7692 §6.
        if h.rsv2 or h.rsv3:
            raise ProtocolError(
                f'RSV2/RSV3 set without negotiated extension '
                f'(rsv2={h.rsv2} rsv3={h.rsv3})')
        if h.rsv1 and (self._decompressor is None or h.opcode in _CONTROL_OPS):
            raise ProtocolError(
                f'RSV1 set on frame (opcode={h.opcode}) without '
                f'negotiated permessage-deflate')

        # Hard cap on declared payload length.  ``h.length`` is
        # the wire indicator (0–125, 126, or 127); the resolved
        # extended length is read inside read_payload, which
        # raises FramePayloadTooLarge before any body bytes are
        # read off the wire.  Defends against post-handshake
        # OOM where the peer advertises a 2**63 - 1 payload.
        try:
            payload = await read_payload(
                self._reader, h.masked, h.length,
                max_length=self._max_frame_payload)
        except FramePayloadTooLarge as exc:
            log_cap_hit('ws_max_frame_payload',
                        requested=exc.declared,
                        limit=self._max_frame_payload,
                        scope_path=self._conn.path if self._conn else None,
                        protocol='ws')
            raise ProtocolError(
                str(exc),
                close_code=WSCloseCode.MESSAGE_TOO_BIG,
            ) from exc

        if self._require_masked and not h.masked:
            raise ProtocolError('unmasked client frame')

        match h.opcode:
            case WSOpcode.TEXT | WSOpcode.BINARY | WSOpcode.CONTINUATION:
                # A data frame is never terminal.  A complete message is
                # emitted via _emit — inline mode's driver then exits on the
                # non-empty _pending — and an incomplete fragment emits
                # nothing; either way the driver reads on.
                await self._handle_data_frame(h.opcode, payload, h.fin, h.rsv1)
                return False
            case WSOpcode.CLOSE | WSOpcode.PING | WSOpcode.PONG:
                return await self._handle_control_frame(h.opcode, payload)
            case _:
                await self._handle_unknown_opcode()
                return True
        # Exhaustiveness fallback: the wildcard arm above covers every
        # opcode, so this line is unreachable — it exists so every path has
        # an explicit ``bool`` return (the CodeQL mixed-returns rule).
        return False

    async def _drive_once(self) -> bool:
        """One :meth:`_read_step` under the shared error handling.

        Every failure path is terminal and emits exactly one thing for the app
        — a disconnect event or the exception itself — so both drivers can
        treat a True return as "stop reading" without duplicating any of the
        RFC 6455 close-frame handling.
        """
        try:
            return await self._read_step()
        except (asyncio.IncompleteReadError, IncompleteReadError):
            await self._close_channel(WSCloseCode.ABNORMAL)
            return True
        except ProtocolError as exc:
            close = encode_frame(
                exc.close_code.to_bytes(2, 'big'),
                opcode=WSOpcode.CLOSE,
                mask=not self._require_masked,
            )
            try:
                await self._writer.write(close)
            except Exception:
                pass  # best-effort CLOSE frame; the socket may already be gone.
            await self._emit_disconnected(exc.close_code)
            # Surface the violation on the next app-side receive() (matches
            # the legacy contract that any exception in the read loop is
            # raised back to the app); the close frame has already gone out.
            await self._emit(exc)
            return True
        except Exception as exc:
            close = encode_frame(
                (1011).to_bytes(2, 'big'),  # INTERNAL_ERROR
                opcode=WSOpcode.CLOSE,
                mask=not self._require_masked,
            )
            try:
                await self._writer.write(close)
            except Exception:
                pass  # best-effort CLOSE frame; the socket may already be gone.
            await self._emit(exc)
            return True

    async def _read_loop(self) -> None:
        """Eager driver: read ahead of the app until the read side is done."""
        while not self._read_finished:
            self._read_finished = await self._drive_once()

    def _refuse_oversized_message(self, exc: MessageTooLarge) -> NoReturn:
        """Log the cap hit and raise the 1009 that closes the connection.

        Three call sites reach it — fragment total, inflate output, and a
        single oversized frame — because there are three ways for a
        message to outgrow the bound and only one thing to do about it.
        """
        log_cap_hit('ws_max_message_size',
                    requested=exc.produced,
                    limit=exc.maximum,
                    scope_path=self._conn.path if self._conn else None,
                    protocol='ws')
        raise ProtocolError(str(exc), close_code=WSCloseCode.MESSAGE_TOO_BIG) from exc

    async def _handle_data_frame(self, opcode, payload: bytes, fin: bool,
                                 rsv1: bool = False) -> None:
        """Handle TEXT/BINARY/CONTINUATION frame.

        Emits a complete message via :meth:`_emit` when the assembler has one
        (after a ``websocket_message`` event, when a dispatcher is wired).
        Returns nothing — whether a message was emitted is not a signal the
        drivers need: inline mode stops via ``_pending`` non-empty, eager mode
        keeps reading until the read side terminates.  Keeping the return off
        the method prevents a future reader from mistaking "a message was
        emitted" for "the read side is done".
        """
        try:
            result = self._assembler.feed(opcode, payload, fin, rsv1)
        except MessageTooLarge as exc:
            self._refuse_oversized_message(exc)
        if result is None:
            return
        msg_opcode, full_payload, compressed = result
        if compressed:
            assert self._decompressor is not None  # frame loop enforced this
            try:
                full_payload = self._decompressor.decompress(
                    full_payload, max_length=self._max_message_size or None)
            except MessageTooLarge as exc:
                # Ordered before the generic handler on purpose: an inflate
                # bomb is a size refusal (1009), not corrupt data (1002).
                self._refuse_oversized_message(exc)
            except Exception as exc:
                # RFC 7692 §7.1 — a payload that fails to decompress is a
                # connection error.  Treat as PROTOCOL_ERROR (1002).
                raise ProtocolError(
                    f'permessage-deflate decompression failed: {exc}',
                    close_code=1002,
                ) from exc
        elif self._max_message_size and len(full_payload) > self._max_message_size:
            # An unfragmented, uncompressed frame reaches neither of the two
            # bounds above: the assembler passes it straight through and there
            # is no inflate step.  Without this, the message bound would be
            # weaker than the frame cap for the simplest message there is.
            self._refuse_oversized_message(
                MessageTooLarge(len(full_payload), self._max_message_size))
        if msg_opcode == WSOpcode.TEXT:
            try:
                text = full_payload.decode('utf-8')
            except UnicodeDecodeError as e:
                # RFC 6455 §8.1 — invalid UTF-8 in a TEXT message MUST be
                # treated as a CLOSE with status code 1007.
                raise ProtocolError(f'invalid UTF-8 in TEXT message: {e}',
                                    close_code=1007)
            message: str | bytes = text
        else:
            message = full_payload
        # The read-time emit adapter (server path) or the dispatcher (direct
        # path) fires ``websocket_message`` HERE, when the message is read —
        # before delivery to the app, in every mode.  The guard must be
        # re-evaluated per message, not read from the receive-cycle cache:
        # a listener registered while the app's ``receive()`` was blocked on
        # the wire is only visible to a fresh check.  With none, the
        # zero-listener hot path pays one int compare (the aggregator
        # gen-caches the lookup) instead of creating a coroutine per message.
        if (self._on_message is not None
                and (self._read_ahead_needed is None
                     or self._read_ahead_needed())):
            await self._on_message(message)
        elif self._dispatcher is not None and self._conn is not None:
            is_text = isinstance(message, str)
            await self._dispatcher.emit(Event(
                'websocket_message',
                detail={
                    'conn': self._conn,
                    'text': message if is_text else None,
                    'bytes': None if is_text else message,
                },
            ))
        await self._emit(message)

    async def _handle_control_frame(self, opcode, payload: bytes) -> bool:
        """Handle CLOSE/PING/PONG frame; returns True if the connection should close."""
        if opcode == WSOpcode.CLOSE:
            # RFC 6455 §5.5.1 — when an endpoint receives a Close frame and
            # has not yet sent one, it MUST send a Close frame in response,
            # echoing the peer's status code if present.  Validate the code
            # and the reason text first; on any violation, send 1002 instead.
            code, reason_ok = _parse_close_payload(payload)
            echo_code = code if reason_ok else WSCloseCode.PROTOCOL_ERROR
            event_code = code if reason_ok else WSCloseCode.PROTOCOL_ERROR
            close = encode_frame(
                echo_code.to_bytes(2, 'big'),
                opcode=WSOpcode.CLOSE,
                mask=not self._require_masked,
            )
            try:
                await self._writer.write(close)
            except Exception:
                pass  # best-effort CLOSE frame; the socket may already be gone.
            await self._close_channel(event_code)
            return True
        if opcode == WSOpcode.PING:
            # RFC 6455 §5.5 — control-frame payload MUST be ≤125 bytes; the
            # frame-header reader catches that case before we get here.
            pong = encode_frame(payload, opcode=WSOpcode.PONG, mask=not self._require_masked)
            await self._writer.write(pong)
        # PONG: unsolicited pong — silently drop
        return False

    async def _handle_unknown_opcode(self) -> None:
        """Send a CLOSE frame and emit a disconnect event for an unknown opcode."""
        close = encode_frame(
            WSCloseCode.PROTOCOL_ERROR.to_bytes(2, 'big'), opcode=WSOpcode.CLOSE)
        try:
            await self._writer.write(close)
        except Exception:
            pass  # best-effort CLOSE frame; the socket may already be gone.
        await self._close_channel(WSCloseCode.PROTOCOL_ERROR)

    async def _close_channel(self, code: int) -> None:
        """Fire ``websocket_disconnected`` and end the channel with *code*.

        The close code used to be passed twice — once to the Level B event and
        again inside the disconnect envelope — which is one place for the two
        to disagree.  It is recorded once here, on ``_terminal_code``, and both
        channels read it from there.
        """
        await self._emit_disconnected(code)
        self._terminal_code = code
        await self._emit(_WS_CLOSED)

    async def _emit_disconnected(self, code: int) -> None:
        """Emit websocket_disconnected exactly once per connection.

        The de-dup flag rides the same client-disconnect marker the HTTP path
        uses (``disconnected``/``mark_disconnected``), so a native Connection
        needs no WS-specific extra.
        """
        conn = self._conn
        if (self._dispatcher is not None and conn is not None
                and not disconnected(conn)):
            mark_disconnected(conn)
            client = conn.client
            connection_id, path = conn.connection_id, conn.path
            await self._dispatcher.emit(Event(
                'websocket_disconnected',
                detail={
                    'conn':          conn,
                    'connection_id': connection_id,
                    'client_ip':     client[0] if client else '',
                    'path':          path,
                    'code':          code,
                },
            ))

    def _read_ahead_observed(self) -> bool:
        """Whether anything can tell the difference between the two modes.

        ``websocket_message`` is contractually emitted **when the server reads
        the message, not when the handler calls receive()** — a handler that
        never consumes must still produce the event.  Only a reader task
        running ahead of the app can do that, so a registered listener forces
        eager mode no matter what the depth says.  With no listener nothing
        observes the difference and the handoff is pure cost.

        Mirrors ``disconnect_events_observed`` on the HTTP path: pay for the
        machinery exactly when someone is watching it.

        The server path (actor) supplies a ``read_ahead_needed`` predicate
        instead of a dispatcher — its ``websocket_message`` events go through
        the read-time emit adapter, not the dispatcher — so the cached
        ``_listeners`` (refreshed once per receive cycle) answers for it.
        """
        if self._read_ahead_needed is not None:
            return self._listeners
        return (self._dispatcher is not None
                and self._dispatcher.has_listeners('websocket_message'))

    def _start_reader(self, depth: int) -> None:
        """Create the read-ahead queue and its task, carrying over the handoff.

        Anything the inline driver (or watchdog servicing) already left in
        ``_pending`` moves into the queue first: once the queue exists the app
        reads from it alone, so an event left behind in the deque would never
        be delivered.  ``_pending`` holds at most one item — the inline driver
        stops as soon as a frame produces something — so the bounded queue
        cannot overflow here.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=depth)
        while self._pending:
            queue.put_nowait(self._pending.popleft())
        self._event_queue = queue
        self._reader_task = asyncio.create_task(self._read_loop())
        self._deferred_pending = False

    def _ensure_reader_started(self) -> None:
        """Start the read-ahead task, or mark it deferred.

        Inline mode has no background reader at all, so this is where the
        per-message task handoff stops existing rather than being made cheaper.
        A positive ``ws_queue_depth`` is an explicit opt-in and starts the
        reader now; a listener that merely *needs* read-ahead does not, because
        the contract it depends on is that the message is read, not that it is
        read ahead.  A consuming handler drives the wire itself and keeps the
        inline path; only when the app goes quiet does the idle watchdog start
        the deferred reader, so nothing observes the difference and no
        consuming handler pays the handoff.
        """
        if self._event_queue is not None or self._read_finished:
            return
        if self._ws_queue_depth > 0:
            self._start_reader(self._ws_queue_depth)
        elif self._read_ahead_observed():
            self._deferred_pending = True

    def start_deferred_reader(self) -> None:
        """Start the deferred reader task.

        Called by the idle watchdog once the app has stopped driving
        ``receive()`` on a connection that needs read-ahead (a
        ``websocket_message`` listener).  Idempotent and safe: refuses while
        a reader already owns the wire, while the app is mid-read, or after
        the read side terminated.

        A listener can need read-ahead with the depth left at 0, so the queue
        falls back to the standard depth rather than a 0-maxsize (i.e.
        unbounded) one, which would drop the backpressure bound.
        """
        if (not self._deferred_pending or self._event_queue is not None
                or self._reader_task is not None or self._reading
                or self._read_finished):
            return
        self._start_reader(self._ws_queue_depth or _WS_EVENT_QUEUE_DEPTH)

    def _frame_bytes_needed(self) -> int | None:
        """Bytes required for the next *complete* frame, or None when it is
        not fully buffered yet.  Drives the non-blocking guarantee of
        :meth:`service_available_control_frames`: a partial frame is never
        read (which would block) — it is left for the next read or tick."""
        buffered = self._reader.buffered_len()
        if buffered < 2:
            return None
        head = self._reader.peek(2)
        len_code = head[1] & 0x7F
        ext = 2 if len_code == 126 else (8 if len_code == 127 else 0)
        if buffered < 2 + ext + 4:                # header + mask key
            return None
        if ext:
            payload_len = int.from_bytes(self._reader.peek(2 + ext)[2:], 'big')
        else:
            payload_len = len_code
        total = 2 + ext + 4 + payload_len
        return total if buffered >= total else None

    async def service_available_control_frames(self) -> bool:
        """Non-blocking servicing of fully-buffered inbound control frames.

        Answers PINGs and echoes CLOSE that arrived while the handler was
        doing something other than ``receive()`` (send-time servicing) or
        after it went quiet (the idle watchdog).  Reads only frames already
        fully buffered, so it never blocks and never steals the wire from an
        inline ``receive()`` (guarded by ``_reading`` / ``_servicing``).  A
        data frame stops the loop without being consumed — the app or a
        reader owns it.  Returns True if any frame was serviced.
        """
        if (self._servicing or self._reading or self._read_finished
                or self._event_queue is not None):
            # A reader task owns the wire when ``_event_queue`` is set — it
            # services control frames itself; the watchdog must not race it.
            return False
        if not self._reader.has_buffered():
            return False
        self._servicing = True
        try:
            serviced = False
            while (not self._read_finished and not self._reading
                   and self._reader.has_buffered()):
                if self._frame_bytes_needed() is None:
                    break
                head = self._reader.peek(2)
                if (head[0] & 0x0F) not in _WS_CONTROL_OPS:
                    break                     # data frame — owned by app/reader
                self._read_finished = await self._drive_once()
                serviced = True
            return serviced
        finally:
            self._servicing = False

    def _on_idle_tick(self) -> None:
        """Watchdog callback: the connection has been quiet for a tick.

        Runs from the scanner's timer context — cheap checks only, then a
        task for any actual work.  A reader already owns the wire (eager /
        deferred started) and services control frames itself; only a pure
        inline connection needs the watchdog's help.

        The liveness decision runs **first and unconditionally**, because it
        asks a different question from the servicing below: servicing is
        about a connection whose handler has gone quiet, liveness is about a
        connection whose *peer* has.  A connection with a reader parked on the
        wire is the normal shape of a silent peer, so the guards below would
        exempt exactly the case the probe exists for.
        """
        self._check_liveness()
        if self._read_finished or self._reading or self._closed:
            return
        if self._event_queue is not None:
            return
        if self._deferred_pending:
            self.start_deferred_reader()
        elif self._reader.has_buffered() and self.has_control_frames_buffered():
            asyncio.get_running_loop().create_task(
                self.service_available_control_frames())

    def _check_liveness(self) -> None:
        """Ask a silent peer whether it is there; close it if it is not.

        One clock read per idle connection per scanner tick, and none on the
        receive path: ``_inbound_seq`` moving is what "activity" means, and
        this is the only place it is turned into a time.
        """
        if self._ws_idle_timeout <= 0 or self._closed or self._read_finished:
            return
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._inbound_seq != self._seq_at_last_check:
            # Frames arrived since the last tick — the peer is alive, and the
            # read path has already cleared any outstanding probe.
            self._seq_at_last_check = self._inbound_seq
            self._last_inbound_at = now
            return
        if self._last_inbound_at == 0.0:
            # First quiet tick of the connection's life: start the clock here
            # rather than at construction, so a connection that was busy
            # before the watchdog armed is not judged on time it never spent
            # idle.
            self._last_inbound_at = now
            return
        if self._probe_sent_at is not None:
            if now - self._probe_sent_at >= self._ws_pong_timeout:
                loop.create_task(self._end_for_unresponsive_peer())
            return
        if now - self._last_inbound_at >= self._ws_idle_timeout:
            self._probe_sent_at = now
            loop.create_task(self._probe_peer())

    async def _probe_peer(self) -> None:
        """RFC 6455 §5.5.2 — ask whether the peer is still responsive.

        §5.5.3 obliges a PONG in reply, but any inbound frame clears the
        probe: a peer that is talking to us is alive, and requiring the
        specific answer would close a connection that is merely busy.
        """
        ping = encode_frame(b'', opcode=WSOpcode.PING,
                            mask=not self._require_masked)
        with contextlib.suppress(Exception):
            await self._writer.write(ping)

    async def _end_for_unresponsive_peer(self) -> None:
        """The probe went unanswered: the peer is gone, not merely quiet.

        ``1001 (Going Away)`` rather than a policy code, for the reason
        HTTP/2 answers its own unanswered probe with ``NO_ERROR``: nothing
        was violated.  We asked a question and got no reply, which is a fact
        about the network, not a complaint about the peer.  The CLOSE is
        best-effort — if the peer really is gone the write fails, and the
        channel is ended either way.
        """
        if self._closed or self._read_finished:
            return
        logger.info('WebSocket peer did not answer the liveness PING in '
                    '%.1fs — closing 1001', self._ws_pong_timeout)
        close = encode_frame(
            WSCloseCode.GOING_AWAY.to_bytes(2, 'big'),
            opcode=WSOpcode.CLOSE, mask=not self._require_masked)
        with contextlib.suppress(Exception):
            await self._writer.write(close)
        self.disarm_watchdog()
        await self._close_channel(WSCloseCode.GOING_AWAY)

    def has_buffered(self) -> bool:
        """True when inbound bytes are already buffered (a read won't block).

        The actor's send-time servicing probes this so it skips the servicing
        call entirely on the common empty-buffer path.
        """
        return self._reader.has_buffered()

    def has_control_frames_buffered(self) -> bool:
        """True when a control frame leads the inbound buffer.

        Synchronous, O(1) gate for send-time servicing: with only data
        frames buffered (a flood), the servicing coroutine's flag churn and
        ``_frame_bytes_needed`` scan would run per message for nothing — a
        data frame is owned by the app/reader, not the servicer.  Also marks
        the connection as having observed a control frame, which activates
        the per-message watchdog work.
        """
        if self._reader.buffered_len() < 2:
            return False
        is_ctrl = (self._reader.peek(2)[0] & 0x0F) in _WS_CONTROL_OPS
        if is_ctrl:
            self._saw_control_frame = True
        return is_ctrl

    def send_touch(self) -> None:
        """Mark send activity for the idle watchdog, at one bool's cost.

        The watchdog is armed once at connect (an idle connection with a
        buffered control frame must still be serviced even if it never
        touches); this only keeps the deadline fresh once control frames
        matter or a listener needs the deferred reader.  ``touch()`` itself
        re-arms a missing watchdog, so a send before the connect receive is
        still safe.  The send-time servicing fast path was removed — the
        watchdog alone bounds PONG latency to ~one scanner tick (the
        documented contract).
        """
        if self._deferred_pending or self._saw_control_frame:
            self.touch()

    def _ensure_watchdog(self) -> None:
        if self._watchdog is None:
            self._watchdog = WsIdleWatchdog(self._on_idle_tick)

    def _ensure_watchdog_armed(self) -> None:
        """Create + register the watchdog once (requires a running loop).

        Arming must not depend on a touch: the zero-listener echo never
        touches, yet an idle connection with a buffered control frame must
        still be serviced.  The ``_watchdog is None`` check is the only
        per-message cost after the first call.
        """
        if self._watchdog is None:
            self._watchdog = WsIdleWatchdog(self._on_idle_tick)
            self._watchdog.touch()      # register with the deadline scanner

    def touch(self) -> None:
        """Mark connection activity (receive or send) for the idle watchdog.

        The default hot path pays one ``loop.time()`` + a comparison per
        message; an actively-driven connection never fires the watchdog.
        """
        self._ensure_watchdog()
        self._watchdog.touch()

    def disarm_watchdog(self) -> None:
        if self._watchdog is not None:
            self._watchdog.disarm()

    async def shutdown(self) -> None:
        """Cancel and await the background read-loop task, and disarm the
        idle watchdog.

        Client sessions call this from ``close()`` so no reader task
        outlives the session (a leaked task
        warns at event-loop shutdown and keeps reading a dead transport).
        Idempotent, and safe to call before the first ``__call__`` ever
        started the loop.
        """
        self._closed = True
        self.disarm_watchdog()
        task = self._reader_task
        self._reader_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass  # Expected: the task was cancelled intentionally.

    def _mark_connect_sent(self) -> None:
        """Claim the handshake read and arm the connection's timers.

        Shared by both channels.  Arming the idle watchdog once here (not per
        message) keeps the zero-listener echo free of a per-message arm call
        while an idle connection with a buffered control frame is still
        serviced.
        """
        self._connect_sent = True
        self._ensure_watchdog_armed()
        self._ensure_reader_started()

    def _refresh_listeners(self) -> None:
        """Once per receive cycle — the hot path then reads a plain attr
        instead of calling the predicate per frame."""
        ra = self._read_ahead_needed
        self._listeners = (ra is None) or ra()

    async def _next_item(self):
        """Pop the next thing the read side produced, or ``_WS_CLOSED``.

        The shared body of both channels: a complete message (``str`` /
        ``bytes``), an exception to re-raise app-side, or the close marker.
        Everything above this is encoding; everything below it is the wire.
        """
        if self._deferred_pending or self._saw_control_frame:
            self.touch()
        self._ensure_reader_started()
        if self._terminal_delivered:
            # Canonical across both modes: once the terminal event has been
            # handed to the app, reading again keeps answering the close so a
            # handler that reads past it can never block on a dead connection.
            return _WS_CLOSED
        if self._event_queue is not None:
            return await self._event_queue.get()
        # Inline: drive the wire in the app's own task until this read has
        # something to hand back.  Frames that produce nothing (fragments,
        # PING, unsolicited PONG) simply loop, so control frames are still
        # serviced — just at the app's read cadence rather than ahead of it.
        #
        # ``_reading`` claims the transport for the whole drive.  The
        # watchdog's servicing path and the deferred-reader start both yield
        # on it, because a second reader entering here would resume at
        # whatever offset this one is parked at — mid-frame, the buffer front
        # is payload, and peeking it as a frame header desyncs the stream.
        self._reading = True
        try:
            while not self._pending and not self._read_finished:
                self._read_finished = await self._drive_once()
        finally:
            self._reading = False
        if not self._pending:
            # The read side finished without leaving anything: the app is
            # reading past the terminal event it already got.
            self._terminal_delivered = True
            return _WS_CLOSED
        return self._pending.popleft()

    async def await_connect(self) -> None:
        """Consume the opening handshake on the native channel.

        The raw ``(conn, receive, send)`` form reads a ``websocket.connect``
        dict for this; the object form has no use for the envelope, so the
        native channel just records that the handshake was taken.  A peer that
        gave up mid-handshake raises :class:`WebSocketDisconnect`, the same
        signal :meth:`next_message` gives.
        """
        self._refresh_listeners()
        if not self._connect_sent:
            self._mark_connect_sent()
            return
        # Already consumed — the caller is re-entering; surface the terminal
        # state rather than silently eating the client's first message.
        if self._terminal_delivered or self._read_finished:
            raise _ws_disconnect(self._terminal_code)

    async def next_message(self) -> str | bytes:
        """The next complete application message: ``str`` text, ``bytes`` binary.

        The native receive channel.  Fragments are already reassembled
        (RFC 6455 §5.4), so what comes back is always a whole message, and the
        Python type *is* the text/binary discriminator — the same contract
        :meth:`blackbull.websocket.WebSocket.receive` publishes.

        Raises :class:`~blackbull.websocket.WebSocketDisconnect` when the peer
        closes, carrying the RFC 6455 §7.4 status code, and re-raises a
        :class:`ProtocolError` the read side recorded.
        """
        self._refresh_listeners()
        if not self._connect_sent:
            self._mark_connect_sent()
        item = await self._next_item()
        if item is _WS_CLOSED:
            self._terminal_delivered = True
            raise _ws_disconnect(self._terminal_code)
        if isinstance(item, Exception):
            self._terminal_delivered = True
            raise item
        return item

    async def __call__(self) -> dict:
        """The ASGI receive channel: the same messages, encoded as dicts.

        The compat surface, and the only place a ``websocket.*`` receive dict
        is built — minted per call for whoever wants that encoding: a raw
        ``(conn, receive, send)`` handler, or an external host.  The object
        form takes :meth:`next_message` and pays nothing.
        """
        self._refresh_listeners()
        if not self._connect_sent:
            self._mark_connect_sent()
            return {'type': ASGIEvent.WS_CONNECT}
        item = await self._next_item()
        if item is _WS_CLOSED:
            self._terminal_delivered = True
            return {'type': ASGIEvent.WS_DISCONNECT,
                    'code': self._terminal_code or WSCloseCode.ABNORMAL}
        if isinstance(item, Exception):
            self._terminal_delivered = True
            raise item
        if isinstance(item, str):
            return {'type': ASGIEvent.WS_RECEIVE, 'text': item, 'bytes': None}
        return {'type': ASGIEvent.WS_RECEIVE, 'text': None, 'bytes': item}



# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

class RecipientFactory:
    """Creates the appropriate ``BaseRecipient`` for the given protocol.

    All methods that need a reader accept a raw asyncio-compatible stream reader
    and wrap it in ``AsyncioReader`` internally.
    """

    @staticmethod
    def http1(reader, conn: Connection, *,
              body_timeout: float = 0.0,
              deadline: ConnectionDeadline | None = None) -> HTTP1Recipient:
        if not isinstance(reader, AbstractReader):
            reader = AsyncioReader(reader)
        return HTTP1Recipient(reader, conn, body_timeout=body_timeout,
                              deadline=deadline)

    @staticmethod
    def http2(frame: FrameBase | None = None,
              queue_depth: int = _HTTP2_STREAM_QUEUE_DEPTH,
              credit_callback: Optional[
                  Callable[[int], Awaitable[None]]] = None,
              credit_budget: int = DEFAULT_INITIAL_WINDOW_SIZE,
              max_body: int | None = None,
              min_rate: float | None = None,
              min_rate_grace: float | None = None) -> HTTP2Recipient:
        # The body limits are forwarded rather than left to the recipient's own
        # fallback: this runs once per stream, and resolving them there costs a
        # function-level import per request.  See ``HTTP2Recipient.__init__``.
        return HTTP2Recipient(frame, queue_depth=queue_depth,
                              credit_callback=credit_callback,
                              credit_budget=credit_budget,
                              max_body=max_body, min_rate=min_rate,
                              min_rate_grace=min_rate_grace)

    @staticmethod
    def websocket(reader, writer, *,
                  dispatcher: EventDispatcher | None = None,
                  conn: Connection | None = None,
                  ws_queue_depth: int = _WS_READ_INLINE,
                  decompressor=None,
                  on_message: Callable[[dict], Awaitable[None]] | None = None,
                  read_ahead_needed: Callable[[], bool] | None = None) -> WebSocketRecipient:
        if not isinstance(reader, AbstractReader):
            reader = AsyncioReader(reader)
        if not isinstance(writer, AbstractWriter):
            writer = AsyncioWriter(writer)
        # The liveness probe is read here, not in the recipient: this factory
        # is the *server's* entry point, and the probe answers a question only
        # the server has — how long an untrusted peer may hold a connection.
        # The bundled clients build a recipient directly and are unaffected.
        from ..env import get_settings  # noqa: PLC0415
        _cfg = get_settings()
        return WebSocketRecipient(reader, writer, dispatcher=dispatcher, conn=conn,
                                  ws_queue_depth=ws_queue_depth,
                                  decompressor=decompressor,
                                  on_message=on_message,
                                  read_ahead_needed=read_ahead_needed,
                                  ws_idle_timeout=_cfg.ws_idle_timeout,
                                  ws_pong_timeout=_cfg.ws_pong_timeout)
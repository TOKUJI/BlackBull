import asyncio
import contextlib
from abc import ABC, abstractmethod
from collections import deque
from inspect import signature
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

# Defaults behind BB_STREAM_QUEUE_DEPTH / BB_WS_QUEUE_DEPTH (see
# docs/reference/env-vars.md); ``WebSocketRecipient`` documents the two modes.
_HTTP2_STREAM_QUEUE_DEPTH = 64
_WS_EVENT_QUEUE_DEPTH = 256     # a deferred reader's depth when the knob is 0
_WS_READ_INLINE = 0

# What :meth:`HTTP1Recipient.after_dispatch` answers.  Plain ints rather than an
# Enum: read once per request on every keep-alive connection, and compared with
# ``is`` on small values the interpreter interns.
CONNECTION_REUSABLE = 0
CONNECTION_NEEDS_DRAIN = 1
CONNECTION_MUST_CLOSE = 2

# Frame-COUNT total for a consume-credited stream queue, whose byte total is the
# advertised inbound window.  A conformant peer may legally burst that window as
# 1-byte frames, so the count sits far above it; what it refuses is the
# zero/tiny-frame flood (CVE-2019-9518) no byte budget can see.
_EVENT_CAP_MULTIPLIER = 16

# End-of-channel marker.  Both native channels carry bare values — ``str`` /
# ``bytes`` here, ``(chunk, end_of_stream)`` for H2 — so the end needs a sentinel
# outside that domain rather than a tagged envelope every reader would unwrap.
# The WS close code rides ``_terminal_code``.
_WS_CLOSED = object()


def _ws_disconnect(code: int | None):
    """Build the app-facing close signal for the native WS channel.

    Imported lazily: ``websocket`` imports ``connection``, which this module
    already depends on, so a module-level import would close a cycle.
    """
    from ..websocket import WebSocketDisconnect  # noqa: PLC0415
    return WebSocketDisconnect(code or WSCloseCode.ABNORMAL)

_H2_DISCONNECT = object()       # as ``_WS_CLOSED``, for the H2 queue

# RFC 6455 §5.5 control opcodes.
_WS_CONTROL_OPS = (WSOpcode.CLOSE, WSOpcode.PING, WSOpcode.PONG)


# ---------------------------------------------------------------------------
# Reader abstraction
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
    """A bounded reader operation was given a byte budget and passed it.

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


def _accepts_read_limit(readuntil) -> bool:
    """Whether a bound ``readuntil`` method accepts the budget argument.

    Compatibility is decided before the read, never by catching a ``TypeError``
    raised from inside reader code.
    """
    try:
        signature(readuntil).bind(b'\n', 1)
    except (TypeError, ValueError):
        return False
    return True


#: End of an HTTP message head; the H/1.1 actor imports it rather than copying it.
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
    """The framework's status-carrying 400.

    Lazy import so ``recipient`` (loaded early via the server) never depends on
    ``router`` at module-import time.  ``HTTPException`` is the dispatcher's
    typed-error seam, so a malformed chunked frame surfaces as 400 rather than
    as a fabricated 500.
    """
    from http import HTTPStatus  # noqa: PLC0415
    from ..router import HTTPException  # noqa: PLC0415
    return HTTPException(HTTPStatus.BAD_REQUEST, detail)


def _content_too_large(detail: str):
    """The 413 for a body that outgrew ``BB_MAX_BODY_SIZE`` mid-stream.

    Only a body that declared nothing reaches here; a declared one is refused at
    head time (docs/reference/env-vars.md, ``BB_MAX_BODY_SIZE``).  The verdict
    travels as the dispatcher's typed error because the handler is already
    running — the same seam a malformed chunk uses to become a 400.

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

    *ext* is the chunk line **from the first ``;``** onward.  A bare ``;`` (empty
    ext-name), a non-token ext-name/val, and control characters are all
    silent-acceptance smuggling vectors, so each is rejected.  A quoted-string
    ext-val is accepted leniently (matched quotes, no bare CTLs) since chunk
    extensions are ignored on receipt.
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


# MAL-CHUNK-EXT-64K (CVE-2023-39326 class) — the unit bound on one chunk-framing
# line (chunk-size + chunk-ext, or one trailer field line).  Mirrors the
# BB_HEADER_MAX_LINE default: extensions and trailers are ignored on receipt, so
# nothing legitimate needs more.
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
    either.
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
        # No chunk-ext, and no trailing OWS: a bare ``5 \r\n`` smuggles.
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
        # One read, not a loop to exactly *n*: asking again for the remainder
        # would block a peer that sent a complete short frame (the shared-port
        # MQTT hang).  The caller re-peeks and calls again, so it converges.
        chunk = await self.read(n - len(ahead))
        if not chunk:
            return False
        ahead += chunk
        return True

    @property
    def _ahead(self) -> bytearray:
        # Lazily attached so subclasses need no cooperating ``__init__``: a test
        # double that forgot ``super()`` would fail only during detection.
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

    async def readuntil(self, sep: bytes, limit: int = 0) -> bytes:
        """Read until *sep* is seen, choosing one limit policy at entry."""
        if limit <= 0:
            return await self._readuntil_unbounded(sep)
        return await self._readuntil_bounded(sep, limit)

    async def _readuntil_unbounded(self, sep: bytes) -> bytes:
        buf = bytearray()
        while sep not in buf:
            chunk = await self.read(1)
            if not chunk:
                break
            buf += chunk
        return bytes(buf)

    async def _readuntil_bounded(self, sep: bytes, limit: int) -> bytes:
        buf = bytearray()
        while sep not in buf:
            chunk = await self.read(1)
            if not chunk:
                break
            buf += chunk
            if len(buf) > limit:
                raise ReadLimitExceeded(
                    f'readuntil exceeds {limit} bytes', bytes(buf))
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
        if limit <= 0:
            return await self._read_head_unbounded()
        return await self._read_head_bounded(limit)

    async def _read_head_unbounded(self) -> bytes:
        buf = bytearray()
        while not buf.endswith(_HEAD_END):
            try:
                line = await self.readuntil(b'\r\n')
            except asyncio.LimitOverrunError as exc:
                raise ReadLimitExceeded(
                    f'stream buffer overflow ({exc.consumed} bytes) '
                    f'while reading the head', bytes(buf)) from exc
            except IncompleteReadError as exc:
                partial = bytes(buf) + exc.partial
                if not partial:
                    return b''
                raise IncompleteReadError(partial) from None
            if not line:
                if not buf:
                    return b''
                raise IncompleteReadError(bytes(buf))
            buf += line
        return bytes(buf)

    async def _read_head_bounded(self, limit: int) -> bytes:
        buf = bytearray()
        native_limit = self.__dict__.get('_readuntil_accepts_limit')
        if native_limit is None:
            native_limit = _accepts_read_limit(self.readuntil)
            self.__dict__['_readuntil_accepts_limit'] = native_limit
        while not buf.endswith(_HEAD_END):
            try:
                if native_limit:
                    line = await self.readuntil(b'\r\n', limit)
                else:
                    line = await AbstractReader._readuntil_bounded(
                        self, b'\r\n', limit)
            except ReadLimitExceeded as exc:
                # Evidence from earlier lines is prepended: classification is a
                # whole-head question (internals.md §One breach, two verdicts).
                seen = bytes(buf) + exc.seen
                raise ReadLimitExceeded(str(exc), seen) from exc
            except asyncio.LimitOverrunError as exc:
                # ``asyncio.StreamReader``'s own buffer limit gets there first
                # for one enormous line.  Its buffer is unreachable from here, so
                # ``seen`` is only what we accumulated — enough to classify,
                # because a line that long has no CRLF in it.
                raise ReadLimitExceeded(
                    f'stream buffer overflow ({exc.consumed} bytes) '
                    f'while reading the head', bytes(buf)) from exc
            except IncompleteReadError as exc:
                # Budget before truncation: a peer that overran and *then* went
                # away overran, and letting EOF mask the breach would make the
                # two reader kinds answer differently.
                self._raise_if_head_over_budget(
                    bytes(buf) + exc.partial, limit)
                partial = bytes(buf) + exc.partial
                if not partial:
                    return b''
                raise IncompleteReadError(partial) from None
            if not line:
                # A reader that reports EOF by returning empty rather than raising.
                self._raise_if_head_over_budget(bytes(buf), limit)
                if not buf:
                    return b''
                raise IncompleteReadError(bytes(buf))
            buf += line
            self._raise_if_head_over_budget(bytes(buf), limit)
        return bytes(buf)

    @staticmethod
    def _raise_if_head_over_budget(seen: bytes, limit: int) -> None:
        if len(seen) > limit:
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
        # A native stream read consumes only on success.  A stream-like double
        # lacking asyncio's ``_buffer``/``_limit`` gets the same property here:
        # bytes consumed before an overrun is detected are replayed.
        self._replay = bytearray()

    async def read(self, n: int) -> bytes:
        if self._replay:
            take = len(self._replay) if n < 0 else min(n, len(self._replay))
            out = bytes(self._replay[:take])
            del self._replay[:take]
            if n < 0 or take == n:
                return out
            return out + await self.read(n - take)
        try:
            return await self._sr.read(n)
        except asyncio.IncompleteReadError as exc:
            raise IncompleteReadError(exc.partial) from exc

    def at_eof(self) -> bool:
        if self._replay:
            return False
        at_eof = getattr(self._sr, 'at_eof', None)
        return bool(at_eof()) if at_eof is not None else False

    async def _readuntil_unbounded(self, sep: bytes) -> bytes:
        if self._replay:
            replay = PrefixReader(bytes(self._take_replay()), self)
            try:
                return await replay.readuntil(sep)
            finally:
                if replay._buf:
                    self._replay[:0] = replay._buf
        try:
            return await self._sr.readuntil(sep)
        except asyncio.IncompleteReadError as exc:
            raise IncompleteReadError(exc.partial) from exc

    async def _readuntil_bounded(self, sep: bytes, limit: int) -> bytes:
        if self._replay:
            replay = PrefixReader(bytes(self._take_replay()), self)
            try:
                return await replay.readuntil(sep, limit)
            finally:
                if replay._buf:
                    self._replay[:0] = replay._buf
        old_limit = getattr(self._sr, '_limit', None)
        if old_limit is not None:
            # ``StreamReader``'s limit excludes the separator; the total budget
            # still caps separator-free accumulation at the right boundary, and
            # the inclusive contract is checked below before a native result is
            # allowed to remain consumed.
            self._sr._limit = limit
        try:
            out = await self._sr.readuntil(sep)
        except asyncio.LimitOverrunError as exc:
            seen = self._resident_prefix(limit + len(sep))
            raise ReadLimitExceeded(
                f'readuntil exceeds {limit} bytes', seen) from exc
        except asyncio.IncompleteReadError as exc:
            if len(exc.partial) > limit:
                self._restore(exc.partial)
                raise ReadLimitExceeded(
                    f'readuntil exceeds {limit} bytes',
                    exc.partial[:limit + len(sep)]) from exc
            raise IncompleteReadError(exc.partial) from exc
        finally:
            if old_limit is not None:
                self._sr._limit = old_limit
        if len(out) <= limit:
            return out
        self._restore(out)
        raise ReadLimitExceeded(
            f'readuntil exceeds {limit} bytes', out[:limit + len(sep)])

    async def readexactly(self, n: int) -> bytes:
        if self._replay:
            head = bytes(self._replay[:n])
            del self._replay[:len(head)]
            if len(head) == n:
                return head
            try:
                return head + await self._sr.readexactly(n - len(head))
            except asyncio.IncompleteReadError as exc:
                raise IncompleteReadError(head + exc.partial) from exc
        try:
            return await self._sr.readexactly(n)
        except asyncio.IncompleteReadError as exc:
            raise IncompleteReadError(exc.partial) from exc

    def has_buffered(self) -> bool:
        # ``StreamReader`` holds every delivered byte in ``_buffer`` until a read
        # consumes it, so "buffer non-empty" is the honest "won't block" probe.
        if self._replay or self.__dict__.get('_ahead_buf'):
            return True
        buf = getattr(self._sr, '_buffer', None)
        if buf is not None:
            return bool(buf)
        return getattr(self._sr, 'buffered', 0) > 0

    def buffered_len(self) -> int:
        ahead = (len(self._replay)
                 + len(self.__dict__.get('_ahead_buf') or b''))
        buf = getattr(self._sr, '_buffer', None)
        if buf is not None:
            return ahead + len(buf)
        return ahead + getattr(self._sr, 'buffered', 0)

    def peek(self, n: int) -> bytes:
        # What detection parked sits in front of what the stream still holds.
        replay = bytes(self._replay)
        ahead = self.__dict__.get('_ahead_buf')
        if replay:
            if len(replay) >= n:
                return replay[:n]
            following = bytes(ahead) if ahead else b''
            return (replay + following + self._peek_stream(n))[:n]
        if ahead:
            if len(ahead) >= n:
                return bytes(ahead[:n])
            return (bytes(ahead) + self._peek_stream(n))[:n]
        return self._peek_stream(n)

    def _resident_prefix(self, n: int) -> bytes:
        return self.peek(n)

    def _restore(self, data: bytes) -> None:
        """Replay bytes a non-native stream consumed before limit failure."""
        stream_buf = getattr(self._sr, '_buffer', None)
        if isinstance(stream_buf, bytearray):
            stream_buf[:0] = data
        else:
            self._replay[:0] = data

    def _take_replay(self) -> bytes:
        out = bytes(self._replay)
        self._replay.clear()
        return out

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

    This is what keeps the dispatcher from consuming protocol-specific bytes
    on the connection's behalf.  The
    fast native ``readuntil`` / ``readexactly`` of the underlying reader are
    used once the prefix is drained, including the seam case where the separator
    straddles the prefix/underlying boundary.
    """

    def __init__(self, prefix: bytes, reader: AbstractReader) -> None:
        self._buf = bytearray(prefix)
        self._reader = reader
        self._reader_accepts_limit = _accepts_read_limit(reader.readuntil)

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

    async def _readuntil_unbounded(self, sep: bytes) -> bytes:
        if not self._buf:
            return await self._reader.readuntil(sep)
        idx = self._buf.find(sep)
        if idx != -1:
            end = idx + len(sep)
            chunk = bytes(self._buf[:end])
            del self._buf[:end]
            return chunk
        # Re-resolve against the seam so a straddling separator is honoured.
        head = bytes(self._buf)
        self._buf.clear()
        combined = head + await self._reader.readuntil(sep)
        end = combined.find(sep) + len(sep)
        self._buf[:0] = combined[end:]          # push back over-read (usually none)
        return combined[:end]

    async def _readuntil_bounded(self, sep: bytes, limit: int) -> bytes:
        """Read a bounded line without consuming the replay prefix on error."""
        if not self._buf:
            if self._reader_accepts_limit:
                return await self._reader.readuntil(sep, limit)
            try:
                return await AbstractReader._readuntil_bounded(self, sep, limit)
            except ReadLimitExceeded as exc:
                self._buf[:0] = exc.seen
                raise
        prefix = bytes(self._buf)
        idx = prefix.find(sep)
        if idx >= 0:
            end = idx + len(sep)
            if end > limit:
                raise ReadLimitExceeded(
                    f'readuntil exceeds {limit} bytes',
                    prefix[:limit + len(sep)])
            del self._buf[:end]
            return prefix[:end]
        if len(prefix) > limit:
            raise ReadLimitExceeded(
                f'readuntil exceeds {limit} bytes',
                prefix[:limit + len(sep)])

        # The underlying reader cannot see separator candidates beginning in the
        # prefix.  Searching only from the previous overlap keeps the seam scan
        # linear and handles overlapping candidates (``...\r`` + ``\r\n``).
        combined = bytearray(prefix)
        scan_from = max(0, len(combined) - len(sep) + 1)
        while True:
            chunk = await self._reader.read(1)
            if not chunk:
                self._buf.clear()
                raise IncompleteReadError(bytes(combined))
            combined += chunk
            idx = combined.find(sep, scan_from)
            if idx >= 0:
                end = idx + len(sep)
                if end <= limit:
                    self._buf.clear()
                    self._buf[:0] = combined[end:]
                    return bytes(combined[:end])
            if len(combined) > limit:
                # Nothing is consumed on the error path: seam bytes join the
                # prefix, which was never removed.
                self._buf[:] = combined
                raise ReadLimitExceeded(
                    f'readuntil exceeds {limit} bytes',
                    bytes(combined[:limit + len(sep)]))
            scan_from = max(scan_from, len(combined) - len(sep) + 1)

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
        # RSV1 of the message-opener frame (RFC 7692: only the first frame of a
        # compressed message carries RSV1=1; continuations keep it clear).
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
            if self.in_progress:
                raise ProtocolError(
                    'New data frame received while a fragmented message is in progress'
                )
            if fin:
                return (opcode, payload, rsv1)
            # The opener is bounded too — the continuation check runs on the
            # *next* frame, so without this the frame cap would stand in for the
            # message total (security-model.md §The invariant).
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
        # ``chunk_size`` slices a chunked body, ``chunk_max`` bounds one
        # transport-paced Content-Length read; see docs/reference/env-vars.md.
        # All five fall back to settings when a caller does not inject them.
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
        # ``BB_MAX_BODY_SIZE``, counted on the octets rather than on the
        # declaration, so a directly-driven recipient or an external ASGI host
        # gets the same ceiling the actor applies at head time.
        self._max_body = max_body
        # ``BB_MIN_BODY_RATE`` / ``BB_MIN_BODY_RATE_GRACE`` — the anti-trickle
        # floor a per-read deadline cannot express; see env-vars.md.
        self._min_rate = min_rate
        self._min_rate_grace = min_rate_grace
        self._chunk_max = max(chunk_max, 1)
        # ``BB_BODY_TIMEOUT``, applied per ``_read_with_timeout`` call — which is
        # per *slice* on both framings; see env-vars.md.
        self._body_timeout = body_timeout
        self._deadline = deadline
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
        # Deliberately no back-reference to the Connection: the actor binds this
        # recipient as ``conn._receive``, so holding it would close a per-request
        # cycle reclaimable only by the cyclic GC — the v0.60.0 tail-latency
        # regression.  Only the path is kept, for the cap-hit diagnostics.
        headers = conn.headers
        self._req_path: str | None = conn.path
        te = headers.get(b'transfer-encoding', b'').strip().lower()
        cl = headers.get(b'content-length', b'')
        if te and te != b'chunked':
            raise NotImplementedError(
                f'Transfer-Encoding "{te.decode()}" is not supported.'
            )
        self._chunked = (te == b'chunked')
        self._content_length = int(cl) if cl else None
        # Per request, not per connection: a rebound recipient that inherited a
        # half-read chunk would splice request N's body into request N+1.
        self._chunk_remaining = 0
        self._done = False
        self._body_seen = 0
        # Transport-wait seconds only, never the handler's own — the delivery
        # rate's denominator (env-vars.md, ``BB_MIN_BODY_RATE_GRACE``).
        self._body_wait = 0.0
        # ``_rate_window_wait`` is the accumulated wait when the current window
        # opened, ``_rate_window_seen`` the octets delivered inside it; the
        # window's width and purpose are ``BB_MIN_BODY_RATE`` in env-vars.md.
        self._rate_window_wait = 0.0
        self._rate_window_seen = 0
        # Over the size cap or below the rate floor; see :attr:`must_close`.
        self._body_refused = False
        # A chunked-framing violation; see :attr:`must_close`.
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
            # Draining a refused body would read the very octets the refusal
            # declined, and re-raise the 413 on the way.
            return False
        return not self._done and (self._chunked or bool(self._content_length))

    def after_dispatch(self) -> int:
        """What the connection should do now the handler has answered.

        One question, because it is one judgement.  Asking ``must_close`` and
        ``needs_drain()`` separately and combining them puts the verdict in
        the caller and leaves the two predicates free to drift apart — the
        recipient is the object that knows whether the message boundary
        survived, so it should say what follows from that.

        Also one call per request instead of two on the keep-alive path.
        """
        if self.framing_broken or self._body_refused:
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
                # EOF or body_timeout mid-drain: nothing left to desync.
                return True
            except HTTPException:
                # A body limit tripped while draining: reachable only for an
                # *undeclared* over-cap body the handler never read.  Report
                # "could not drain" — the caller closes, which is what a refused
                # body asks for — rather than let a 413 with no request left to
                # answer reach the connection's generic error handler.
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

        The slice, not the whole chunk, because a peer-declared ``chunk-size``
        must not choose how much the server buffers or reopen the pause the
        high-water mark just applied — internals.md §Backpressure pauses for a
        handler that is behind, not one that is waiting.

        ``readexactly`` (not the up-to-n ``read``) still backs every slice, so a
        chunk split across TCP segments cannot return short and silently corrupt
        the body.
        """
        n = min(self._chunk_remaining, self._chunk_size)
        data = await self._read_with_timeout(self._reader.readexactly(n))
        self._chunk_remaining -= n
        if self._chunk_remaining == 0:
            # RFC 9112 §7.1 — chunk-data is followed by exactly CRLF.  Reading
            # *until* CRLF would swallow trailing spill (SMUG-CHUNK-SPILL) and
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
        (SMUG-CHUNK-LF-TERM / SMUG-CHUNK-LF-TRAILER).
        """
        try:
            line = await self._read_with_timeout(self._readuntil_chunk_line())
        except ReadLimitExceeded as exc:
            self.framing_broken = True
            log_cap_hit('h1_chunk_line_length',
                        requested=len(exc.seen), limit=_CHUNK_LINE_MAX,
                        scope_path=self._req_path,
                        protocol='http1')
            raise _bad_request(
                'chunk framing line exceeds length limit') from None
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

    async def _readuntil_chunk_line(self) -> bytes:
        return await self._reader.readuntil(b'\n', limit=_CHUNK_LINE_MAX)

    def _account(self, chunk: bytes) -> bytes:
        """Weigh *chunk* against the two body limits, giving up if it fails one.

        Every delivered octet passes through here on both framings, so the
        limits belong to the recipient rather than to whichever caller drives it.
        Both verdicts are permanent for the connection (:attr:`must_close`).

        They answer differently, and that is the point: too large is a judgement
        about the *request*, which the peer is entitled to hear — 413.  Too slow
        is a judgement about the *peer*, and answering it politely would be
        answering the attack, so the connection is abandoned as ``body_timeout``
        abandons a silent one.
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
                # Earned its keep: roll the window forward.
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
                # Fallback for a caller with no ConnectionDeadline; same
                # per-call semantics, but a fresh Timeout per chunk.
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
                    # RFC 9112 §7.1.2 — last-chunk, an optional trailer-part,
                    # then a final CRLF.  Each line must be CRLF-terminated (a
                    # bare LF is the same violation as on the chunk-size line),
                    # and RFC 9110 §6.5.1-prohibited fields are rejected.
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
                # Transport-paced up-to-n slices (env-vars.md,
                # ``BB_BODY_CHUNK_MAX``).  ``b''`` means the peer is gone — a
                # reader parks rather than returning short when more may come —
                # so an unspent Content-Length is a truncated upload.
                if self._content_length:
                    n = min(self._content_length, self._chunk_max)
                    body = await self._read_with_timeout(self._reader.read(n))
                    # Gate on the *length*, not truthiness: forward progress is
                    # the reader's return value, so "zero bytes came back" is
                    # what ends the loop.  Identical for ``bytes``; the
                    # difference is a reader whose reads do not return ``bytes``.
                    if not len(body):
                        raise IncompleteReadError(b'')
                    self._content_length -= len(body)
                    if self._content_length == 0:
                        self._done = True
                    return self._account(body)
                self._done = True
                return None

        except (asyncio.TimeoutError, TimeoutError):
            # Distinguished from EOF mid-body only so operators see the cap hit;
            # the app sees the same HTTP_DISCONNECT either way.
            log_cap_hit('body_timeout',
                        requested=self._body_timeout,
                        limit=self._body_timeout,
                        scope_path=self._req_path,
                        protocol='http1')
            self._done = True
            raise ClientDisconnected() from None
        except IncompleteReadError:
            # EOF mid-body — not a cap hit, and no synthetic 408.
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
    backstop.  Without a callback the queue is bounded and credit is issued
    at enqueue instead (push streams, direct test use).
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
        # Bytes enqueued but not yet consumed, and therefore not yet credited
        # back.  For a conformant peer this can never exceed ``credit_budget``:
        # the un-credited bytes ARE the closed part of the window it must respect.
        self._uncredited: int = 0
        self._end_of_stream_on_headers: bool = False
        self._initial_consumed: bool = False
        self._done: bool = False
        if max_body is None or min_rate is None or min_rate_grace is None:
            # Fallback for a directly-instantiated recipient (tests).  One
            # recipient is built *per stream*, so the production path must not
            # take it: a function-level relative import resolves through
            # ``importlib._bootstrap`` on every execution.
            from ..env import get_settings as _get_settings  # noqa: PLC0415
            _s = _get_settings()
            if max_body is None:
                max_body = _s.max_body_size
            if min_rate is None:
                min_rate = _s.min_body_rate
            if min_rate_grace is None:
                min_rate_grace = _s.min_body_rate_grace
        # The two body limits, shared with HTTP/1.1; what each refuses and how
        # HTTP/2 answers it is env-vars.md, ``BB_MAX_BODY_SIZE``.
        self._max_body = max_body
        self._min_rate = min_rate
        self._min_rate_grace = min_rate_grace
        self._body_seen = 0
        #: Wall clock, unlike HTTP/1.1's transport-wait denominator: DATA arrives
        #: whether or not the handler reads, so elapsed time is the peer's alone.
        self._rate_window_start: float | None = None
        self._rate_window_seen = 0
        #: Exempts the peer from the rate judgement: it was blocked by our own
        #: closed inbound window, so its pace is partly our doing.
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
            # Consume-crediting mode bounds itself in put_DATAFrame, so the queue
            # is unbounded and ``put_disconnect`` can always deliver.  Without a
            # credit callback the queue keeps its frame-count maxsize.
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

        Judged on arrival rather than on consumption: DATA lands whether or not
        the handler is reading, so the memory this bounds is spent before anyone
        asks for it.

        The rate judgement is skipped once our own inbound window has
        back-pressured the peer — blaming it for obeying our flow control would
        turn a slow *handler* into a reset stream.  A trickle never fills the
        window, which is why the exemption cannot shelter one.
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
                # Observed as the window *closes*, not while it is closed: the
                # peer's next frame can only arrive after a replay has reopened
                # it, by which point the balance no longer shows the stall.
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
                # Earned its keep: roll the window forward.
                self._rate_window_start = now
                self._rate_window_seen = 0
        return False

    def put_DATAFrame(self, frame: Data) -> bool:
        """Enqueue a DATA frame event.  Returns False when the frame must be
        refused (the caller answers RST_STREAM): queue full when no credit
        callback is installed; inbound-window overrun, a tiny-frame flood, or
        a body limit (``BB_MAX_BODY_SIZE`` / ``BB_MIN_BODY_RATE``) in
        consume-crediting mode.
        """
        if self._body_limits_refuse(len(frame.payload)):
            return False
        if self._credit_cb is not None:
            # Flow-control debit is the full frame length including padding
            # (RFC 9113 §6.9.1) — credit must mirror it exactly.
            fc_len = frame.length
            if self._uncredited + fc_len > self._credit_budget:
                # Past the advertised inbound window it was never credited for:
                # a conformant peer is back-pressured well before this.
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
                # Zero/tiny-frame flood; see ``_EVENT_CAP_MULTIPLIER``.
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
        stream ends the body without carrying any.  Enqueues the native pair:
        building an ``http.request`` dict here only to translate it back one
        line later would put a request-dict producer back on the native path.
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
            # The app task is hopelessly behind; TaskGroup cancellation cleans up
            # the stream regardless.  Unreachable in consume-crediting mode.
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
        # Checked even when a queue exists: ``put_disconnect`` may have raced
        # ahead of the app's first read while the stream still ended cleanly at
        # HEADERS, and the complete (empty) body must precede any disconnect —
        # otherwise a body-reading handler on a body-less request sees a
        # spurious client disconnect.
        if self._end_of_stream_on_headers and not self._initial_consumed:
            self._initial_consumed = True
            self._done = True
            return {'type': ASGIEvent.HTTP_REQUEST, 'body': b'', 'more_body': False}
        item = await self._take()
        if item is _H2_DISCONNECT:
            self._done = True
            return {'type': ASGIEvent.HTTP_DISCONNECT}
        payload, end_stream = item
        # Set here, never read here — internals.md §Receive-path invariant.
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
                # The exemption marks an *interval* we back-pressured, so it has
                # to end when the window reopens: a flag that only ever turns on
                # would let one window-filling burst buy an unlimited drip.  The
                # window restarts rather than resumes, so the exempted interval
                # is not averaged into the next judgement.
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

    Client callers inject the ownership names 'client_ws_max_frame_payload',
    and 'client_ws_max_message_size', at these shared rejection sites.

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
    produce it.  A registered listener does not force read-ahead on, though:
    a consuming handler is already reading, so the reader is only marked
    *deferred* and the idle watchdog starts it if the handler goes quiet.
    """

    # Fallback for ``BB_WS_MAX_FRAME_PAYLOAD`` (env-vars.md, which carries the
    # rationale).  64 MiB because a 1 MiB cap regresses the Autobahn 9.x cases,
    # whose largest frame is 16 MiB (case 9.1.6).
    _MAX_FRAME_PAYLOAD: int = 64 * 1024 * 1024

    # Fallback for ``BB_WS_MAX_MESSAGE_SIZE``, likewise.  16 MiB is the Autobahn
    # suite's largest message (9.1.6 text / 9.2.6 binary).
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
                 ws_pong_timeout: float | None = None,
                 frame_cap_name: str = 'ws_max_frame_payload',
                 message_cap_name: str = 'ws_max_message_size'):
        super().__init__(reader)
        self._writer = writer
        self._frame_cap_name = frame_cap_name
        self._message_cap_name = message_cap_name
        self._connect_sent = False
        # Late import keeps ``recipient`` importable without the settings stack,
        # for callers that drive a recipient with no Settings populated.
        if max_frame_payload is not None:
            self._max_frame_payload: int = max_frame_payload
        else:
            try:
                from ..env import get_settings  # noqa: PLC0415
                self._max_frame_payload = get_settings().ws_max_frame_payload
            except Exception:
                self._max_frame_payload = self._MAX_FRAME_PAYLOAD
        if max_message_size is not None:
            self._max_message_size: int = max_message_size
        else:
            try:
                from ..env import get_settings  # noqa: PLC0415
                self._max_message_size = get_settings().ws_max_message_size
            except Exception:
                self._max_message_size = self._MAX_MESSAGE_SIZE
        self._assembler = FragmentAssembler(max_total=self._max_message_size)
        # Per connection, not shared: the budget is what *one* peer may spend.
        try:
            from ..env import get_settings  # noqa: PLC0415
            _s = get_settings()
            self._control_meter = RateWindow(_s.frame_rate_limit,
                                             _s.frame_rate_window)
        except Exception:
            self._control_meter = RateWindow(20, 1.0)
        # RFC 6455 §5.1 — a client MUST mask, a server MUST NOT.  Masking is
        # therefore symmetric in one flag: whoever requires it *in* must not mask
        # *out*, so this also decides the recipient's own PONG and CLOSE frames.
        self._require_masked = require_masked
        self._dispatcher = dispatcher
        self._conn = conn
        self._ws_queue_depth = ws_queue_depth
        self._event_queue: asyncio.Queue | None = None
        self._reader_task: asyncio.Task | None = None
        # Inline mode's handoff slot, not a queue: it holds at most one item,
        # because the inline driver stops as soon as a frame produces something.
        # The bounded read-ahead the depth knob describes is eager mode only.
        self._pending: deque = deque()
        # Stops the inline driver from touching a dead transport.
        self._read_finished = False
        # Post-terminal behaviour, identical in both modes: once the terminal
        # event is handed to the app, receive() keeps answering a disconnect with
        # this code instead of blocking forever.
        self._terminal_code: int | None = None
        self._terminal_delivered = False
        # ``None`` disables permessage-deflate, which makes any inbound RSV1=1
        # frame a protocol violation.
        self._decompressor = decompressor
        # Read-time emit adapter for ``websocket_message`` (server path); the
        # dispatcher/conn pair below is the equivalent for a direct drive.
        self._on_message = on_message
        self._read_ahead_needed = read_ahead_needed
        # Wire ownership: exactly one of {inline receive, reader task, watchdog
        # servicing} drives the wire at a time.  The reader task and the watchdog
        # both yield on these two flags.
        self._reading = False
        self._servicing = False
        # A control frame has been read or peeked on this connection.  Gates the
        # per-message watchdog work, which before the first one is pure overhead
        # — an echo workload pays one bool check per message instead.
        self._saw_control_frame = False
        self._listeners = False
        # A listener needs read-ahead but the reader task has not started; the
        # idle watchdog starts it if the app goes quiet.
        self._deferred_pending = False
        # Created lazily on the first touch(), so constructing a recipient never
        # requires a running loop.
        self._watchdog: WsIdleWatchdog | None = None
        self._closed = False
        # The *time* column for a WebSocket connection (env-vars.md,
        # ``BB_WS_IDLE_TIMEOUT``).  Off unless a caller asks: this class is the
        # read side of both roles, and ``RecipientFactory.websocket`` — the
        # server's entry point — is the one place the Settings value is read.
        self._ws_idle_timeout: float = ws_idle_timeout or 0.0
        self._ws_pong_timeout: float = ws_pong_timeout or 30.0
        # A **counter**, not a timestamp: the receive path is the hot path, and a
        # per-message clock read does not belong on it.  The tick callback turns
        # the counter into a time, once per idle connection per scanner tick.
        self._inbound_seq: int = 0
        self._seq_at_last_check: int = 0
        self._last_inbound_at: float = 0.0
        self._probe_sent_at: float | None = None

    @property
    def terminal_code(self) -> int | None:
        """The RFC 6455 §7.4 close code, once the read side has finished.

        The single record of how this connection ended.  The actor keeps no
        copy of its own: that would mean intercepting every event to look for
        a disconnect, and two records of one fact is one place for them to
        disagree.
        """
        return self._terminal_code

    async def _emit(self, item) -> None:
        """Hand one ASGI event (or an exception to re-raise app-side) to the app.

        The only place the two read modes diverge: eager mode pushes through the
        bounded queue, which is what applies backpressure to a fast peer; inline
        mode drops it in the handoff slot, where the caller one frame up the
        stack is already waiting for it.

        The terminal code is recorded here so a receive() past the terminal event
        keeps answering a disconnect with the same code.
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

        A frame that produces nothing (an incomplete fragment, a PING, an
        unsolicited PONG) emits nothing and returns False, so whichever driver
        is running simply reads again.  Keeping every RFC decision here is what
        stops the two modes drifting apart.
        """
        _CONTROL_OPS = (WSOpcode.CLOSE, WSOpcode.PING, WSOpcode.PONG)
        h = await read_frame_header(self._reader)
        # Liveness at the cost of one integer add; not a ``loop.time()``.
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
            # Metered by count (env-vars.md, ``BB_FRAME_RATE_LIMIT``), and
            # checked before the payload is read: the answer to too many is to
            # stop, not to keep reading them faster.
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

        # RFC 6455 §5.2 — RSV bits MUST be 0 unless an extension defining them
        # was negotiated.  RSV1 is permessage-deflate's (RFC 7692); RSV2/RSV3
        # belong to no extension we negotiate, and RSV1 on a control frame is
        # always a violation per RFC 7692 §6.
        if h.rsv2 or h.rsv3:
            raise ProtocolError(
                f'RSV2/RSV3 set without negotiated extension '
                f'(rsv2={h.rsv2} rsv3={h.rsv3})')
        if h.rsv1 and (self._decompressor is None or h.opcode in _CONTROL_OPS):
            raise ProtocolError(
                f'RSV1 set on frame (opcode={h.opcode}) without '
                f'negotiated permessage-deflate')

        # ``h.length`` is the wire indicator (0–125, 126 or 127); the resolved
        # extended length is judged inside ``read_payload``, which raises before
        # any body byte is read off the wire.
        try:
            payload = await read_payload(
                self._reader, h.masked, h.length,
                max_length=self._max_frame_payload)
        except FramePayloadTooLarge as exc:
            log_cap_hit(self._frame_cap_name,
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
                # A data frame is never terminal.
                await self._handle_data_frame(h.opcode, payload, h.fin, h.rsv1)
                return False
            case WSOpcode.CLOSE | WSOpcode.PING | WSOpcode.PONG:
                return await self._handle_control_frame(h.opcode, payload)
            case _:
                await self._handle_unknown_opcode()
                return True
        # Unreachable — the wildcard arm covers every opcode.  Present so every
        # path has an explicit ``bool`` return (the CodeQL mixed-returns rule).
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
            # Any exception in the read loop is raised back to the app on its
            # next receive(); the close frame has already gone out.
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

        Three ways to outgrow the bound — fragment total, inflate output, a
        single oversized frame — and one thing to do about it.
        """
        log_cap_hit(self._message_cap_name,
                    requested=exc.produced,
                    limit=exc.maximum,
                    scope_path=self._conn.path if self._conn else None,
                    protocol='ws')
        raise ProtocolError(str(exc), close_code=WSCloseCode.MESSAGE_TOO_BIG) from exc

    async def _handle_data_frame(self, opcode, payload: bytes, fin: bool,
                                 rsv1: bool = False) -> None:
        """Handle TEXT/BINARY/CONTINUATION frame.

        Returns nothing deliberately: whether a message was emitted is not a
        signal either driver needs, and a ``bool`` here would invite a reader to
        mistake "a message was emitted" for "the read side is done".
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
            # An unfragmented, uncompressed frame reaches neither bound above:
            # the assembler passes it straight through and there is no inflate
            # step.  Without this the message total would be weaker than the
            # frame cap for the simplest message there is.
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
        # ``websocket_message`` fires HERE, when the message is read, in every
        # mode.  The guard is re-evaluated per message rather than read from the
        # receive-cycle cache: a listener registered while the app's
        # ``receive()`` was blocked on the wire is only visible to a fresh check.
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
            # RFC 6455 §5.5.1 — an endpoint that has not sent one MUST answer a
            # Close frame, echoing the peer's status code if present; on any
            # violation of the code or the reason text, send 1002 instead.
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

        The close code is recorded once here and both channels read it from
        there; passing it twice would be one place for the two to disagree.
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

        With no ``websocket_message`` listener nothing observes it and the
        handoff is pure cost.  Mirrors ``disconnect_events_observed`` on the HTTP
        path: pay for the machinery exactly when someone is watching it.

        The server path supplies a ``read_ahead_needed`` predicate rather than a
        dispatcher, so the cached ``_listeners`` answers for it.
        """
        if self._read_ahead_needed is not None:
            return self._listeners
        return (self._dispatcher is not None
                and self._dispatcher.has_listeners('websocket_message'))

    def _start_reader(self, depth: int) -> None:
        """Create the read-ahead queue and its task, carrying over the handoff.

        Once the queue exists the app reads from it alone, so anything left in
        ``_pending`` must move across or it is never delivered.  It holds at most
        one item, so the bounded queue cannot overflow here.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=depth)
        while self._pending:
            queue.put_nowait(self._pending.popleft())
        self._event_queue = queue
        self._reader_task = asyncio.create_task(self._read_loop())
        self._deferred_pending = False

    def _ensure_reader_started(self) -> None:
        """Start the read-ahead task, or mark it deferred.

        A positive ``ws_queue_depth`` is an explicit opt-in and starts the reader
        now.  A listener that merely *needs* read-ahead does not: the contract it
        depends on is that the message is read, not that it is read ahead, and a
        consuming handler is already reading (env-vars.md, ``BB_WS_QUEUE_DEPTH``).
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
        """Bytes required for the next *complete* frame, or None when it is not
        fully buffered.  The non-blocking guarantee of
        :meth:`service_available_control_frames` rests on this."""
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

        Runs from the scanner's timer context — cheap checks only, then a task
        for any actual work.  Only a pure inline connection needs the servicing
        help; a reader that owns the wire does it itself.

        Liveness runs **first and unconditionally**, because it asks a different
        question: servicing is about a connection whose handler has gone quiet,
        liveness about one whose *peer* has.  A reader parked on the wire is the
        normal shape of a silent peer, so the guards below would exempt exactly
        the case the probe exists for.
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
            # Frames arrived since the last tick; the read path has already
            # cleared any outstanding probe.
            self._seq_at_last_check = self._inbound_seq
            self._last_inbound_at = now
            return
        if self._last_inbound_at == 0.0:
            # First quiet tick: the clock starts here rather than at
            # construction, so a connection that was busy before the watchdog
            # armed is not judged on time it never spent idle.
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

        ``1001 (Going Away)`` rather than a policy code, for the reason HTTP/2
        answers its own unanswered probe with ``NO_ERROR``: nothing was violated.
        No reply is a fact about the network, not a complaint about the peer.
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
        still safe.  There is deliberately no send-time servicing fast path:
        the watchdog alone bounds PONG latency to ~one scanner tick, which is
        the documented contract.
        """
        if self._deferred_pending or self._saw_control_frame:
            self.touch()

    def _ensure_watchdog(self) -> None:
        if self._watchdog is None:
            self._watchdog = WsIdleWatchdog(self._on_idle_tick)

    def _ensure_watchdog_armed(self) -> None:
        """Create + register the watchdog once (requires a running loop).

        Arming must not depend on a touch: the zero-listener echo never touches,
        yet an idle connection with a buffered control frame must still be
        serviced.
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

        Shared by both channels.  Arming once here rather than per message keeps
        the zero-listener echo free of a per-message arm call.
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
            return _WS_CLOSED
        if self._event_queue is not None:
            return await self._event_queue.get()
        # Inline: drive the wire in the app's own task until this read has
        # something to hand back.  Frames that produce nothing (fragments, PING,
        # unsolicited PONG) simply loop, so control frames are still serviced —
        # at the app's read cadence rather than ahead of it.
        #
        # ``_reading`` claims the transport for the whole drive: a second reader
        # entering here would resume at whatever offset this one is parked at,
        # and mid-frame the buffer front is payload, so peeking it as a frame
        # header desyncs the stream.
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
        # Forwarded rather than left to the recipient's own per-stream fallback;
        # see ``HTTP2Recipient.__init__``.
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
        # The liveness probe is read here, not in the recipient: this factory is
        # the *server's* entry point, and the probe answers a question only the
        # server has — how long an untrusted peer may hold a connection.
        from ..env import get_settings  # noqa: PLC0415
        _cfg = get_settings()
        return WebSocketRecipient(reader, writer, dispatcher=dispatcher, conn=conn,
                                  ws_queue_depth=ws_queue_depth,
                                  decompressor=decompressor,
                                  on_message=on_message,
                                  read_ahead_needed=read_ahead_needed,
                                  ws_idle_timeout=_cfg.ws_idle_timeout,
                                  ws_pong_timeout=_cfg.ws_pong_timeout)

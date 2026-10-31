"""The HTTP/1.1 message codec (RFC 9112), shared by the server and the client.

Both roles read a head, a field section, a body and a trailer section with the
same rules.  A role chooses its start-line grammar and how it answers an error:
[`MessageError`][] is 400 to the server and ``ProtocolError`` to the client.
Rules on which the roles differ are in the role policy section below.
"""
import asyncio
import re

from .field_grammar import (COMMON_METHODS_OCTETS, FIELD_VALUE_ALLOWED_OCTETS,
                            PROHIBITED_TRAILER_FIELDS, TCHAR_OCTETS, FieldError,
                            field_line, list_members, method_token_is_valid)
from .framing import (_CRLF, is_informational, method_is, parse_content_length,
                      parse_status, response_has_content, split_transfer_codings)

__all__ = ('CHUNKED', 'CLOSE_DELIMITED', 'LENGTH', 'NO_BODY', 'ChunkedDecoder',
           'FieldsTooLarge', 'IncompleteReadError', 'LineCache', 'MessageError',
           'ReadLimitExceeded', 'Unimplemented', 'UnsupportedVersion',
           'connection_options', 'content_length', 'parse_head', 'persistent',
           'request_framing', 'response_framing')


class MessageError(ValueError):
    """The message breaks RFC 9112; its reader cannot tell where it ends."""


class FieldsTooLarge(MessageError):
    """A head line, chunk line or trailer section is over its limit.

    ``section`` is true for a whole trailer section, false for one line.
    """

    def __init__(self, message: str, length: int, limit: int, *,
                 section: bool = False) -> None:
        super().__init__(message)
        self.length = length
        self.limit = limit
        self.section = section


class Unimplemented(MessageError):
    """Well-formed, but uses a transfer coding or method the recipient lacks."""


class UnsupportedVersion(MessageError):
    """A well-formed ``HTTP-version`` with a major version other than 1."""


class IncompleteReadError(EOFError):
    """A reader's peer closed mid-read; ``partial`` is what had arrived.

    The caller answers a truncated head and an idle close differently, so
    ``partial`` is part of the contract.
    """

    @property
    def partial(self) -> bytes:
        return self.args[0] if self.args else b''


class ReadLimitExceeded(Exception):
    """A bounded read passed its byte budget; ``seen`` is what it read.

    The reader reports and the protocol decides the status
    (``docs/about/internals.md`` §One breach, two verdicts).
    """

    def __init__(self, message: str, seen: bytes = b'') -> None:
        super().__init__(message)
        self.seen = seen


# --- head and field section (RFC 9112 §2-§5) ------------------------------

#: RFC 9112 §4 — HTTP-version = "HTTP/" DIGIT "." DIGIT
_HTTP_VERSION_RE = re.compile(rb'\AHTTP/\d\.\d\Z')
#: RFC 9110 §8.6 as written: one ``1*DIGIT``, no leading zero, after one SP at most.
_CL_STRICT_RE = re.compile(rb'\A ?(?:0|[1-9][0-9]*)\Z')
#: Spellings a proxy may normalise into a framing field.
_UNDERSCORE_FRAMING_NAMES = frozenset((b'content_length', b'transfer_encoding'))


# Bound attacker-controlled cache keys by bytes per connection; an entry
# count alone does not bound retained memory.
_LINE_CACHE_MAX = 64
_LINE_CACHE_MAX_LINE = 1024
_LINE_CACHE_MAX_BYTES = 8192


class LineCache(dict):
    """Field lines one peer repeats, mapped to their parsed pairs; ``size`` is
    the bytes of the lines held."""

    size = 0


#: Names a shared table must never carry: framing is read from each message.
_FRAMING_NAMES = frozenset({
    b'content-length', b'transfer-encoding', b'host', b'expect', b'upgrade',
    b'trailer', b'te-framing',
})

# Only lines with a closed value set; no framing field.
_SPEC_ENUMERATED_LINES: tuple[bytes, ...] = (
    # Fetch Metadata Request Headers (W3C).
    *(b'Sec-Fetch-Site: ' + v for v in
      (b'cross-site', b'same-origin', b'same-site', b'none')),
    *(b'Sec-Fetch-Mode: ' + v for v in
      (b'cors', b'navigate', b'no-cors', b'same-origin', b'websocket')),
    *(b'Sec-Fetch-Dest: ' + v for v in
      (b'audio', b'audioworklet', b'document', b'embed', b'empty', b'font',
       b'frame', b'iframe', b'image', b'manifest', b'object', b'paintworklet',
       b'report', b'script', b'serviceworker', b'sharedworker', b'style',
       b'track', b'video', b'worker', b'xslt')),
    b'Sec-Fetch-User: ?1',
    # UA client hints (RFC 8942 / W3C).
    b'sec-ch-ua-mobile: ?0',
    b'sec-ch-ua-mobile: ?1',
    *(b'sec-ch-ua-platform: "' + v + b'"' for v in
      (b'Android', b'Chrome OS', b'Chromium OS', b'iOS', b'Linux', b'macOS',
       b'Windows', b'Unknown')),
    # Fixed tokens (RFC 9110 / RFC 9112 / RFC 6797 / DNT).
    b'Upgrade-Insecure-Requests: 1',
    b'DNT: 0',
    b'DNT: 1',
    b'TE: trailers',
    b'Pragma: no-cache',
    b'Connection: keep-alive',
    b'Connection: Keep-Alive',
    b'Connection: close',
    *(b'Cache-Control: ' + v for v in
      (b'no-cache', b'no-store', b'max-age=0')),
    # Universal idioms, few on purpose.
    b'Accept: */*',
    b'Accept-Encoding: gzip',
    b'Accept-Encoding: gzip, deflate',
    b'Accept-Encoding: gzip, deflate, br',
    b'Accept-Encoding: gzip, deflate, br, zstd',
    b'Accept-Encoding: identity',
)


def _build_default_lines() -> dict[bytes, tuple[bytes, bytes]]:
    table: dict[bytes, tuple[bytes, bytes]] = {}
    for line in _SPEC_ENUMERATED_LINES:
        lkey, value = field_line(line)
        if lkey in _UNDERSCORE_FRAMING_NAMES or lkey in _FRAMING_NAMES:
            raise ValueError(f'framing header must not be pre-seeded: {line!r}')
        if len(line) > _LINE_CACHE_MAX_LINE:
            raise ValueError(f'default header line too long: {line!r}')
        table[line] = (lkey, value)
    return table


_DEFAULT_LINES = _build_default_lines()


def _block_values_are_clean(data: bytes) -> bool:
    """True when no field value in *data* can hold a forbidden octet.

    False only turns the per-value check on; it rejects nothing.
    """
    residue = data.translate(None, FIELD_VALUE_ALLOWED_OCTETS)
    return residue.count(b'\r\n') * 2 == len(residue)


def parse_head(head: bytes, max_line: int, cache: LineCache, request: bool
               ) -> tuple[list[bytes] | tuple[bytes, int], list[tuple[bytes, bytes]],
                          dict[bytes, list[tuple[bytes, bytes]]]]:
    """``(start, pairs, index)`` of a message head.

    *start* is ``(method, request-target, HTTP-version)`` for a *request* and
    ``(HTTP-version, status)`` for a response.  Field names are lowercase
    tchar and values carry no edge SP/HTAB and no CTL.  A line over *max_line*
    is [`FieldsTooLarge`][].  *cache* holds lines this peer sent before.
    """
    lines = head.split(_CRLF)
    # A line cannot be longer than its head, so a short head skips the walk.
    if max_line > 0 and len(head) > max_line:
        for line in lines:
            if len(line) > max_line:
                raise FieldsTooLarge(f'head line {len(line)} bytes > {max_line}',
                                     len(line), max_line)
    if request:
        # RFC 9112 §2.2 — one stray empty line before the request is tolerated.
        first = 1 if lines[0] == b'' else 0
        if first >= len(lines):
            raise MessageError('empty request')
        start = lines[first].split(b' ')
        if len(start) != 3:
            raise MessageError(f'request line must have exactly 3 SP-separated '
                               f'parts, got {len(start)}: {lines[first]!r}')
        method, _target, version = start
        # RFC 9110 §9.1; a common method is a token, which tests/architecture pins.
        if method not in COMMON_METHODS_OCTETS and not method_token_is_valid(method):
            raise MessageError(f'invalid method {method!r}')
        if not _HTTP_VERSION_RE.match(version):
            raise MessageError(f'invalid HTTP-version {version!r}')
        # RFC 9110 §2.5: a higher 1.x minor is served as 1.1.
        if version[5:6] != b'1':
            raise UnsupportedVersion(f'HTTP version {version!r} is not supported')
    else:
        first = 0
        parts = lines[0].split(b' ', 2)
        if len(parts) < 2:
            raise MessageError(f'malformed status line: {lines[0]!r}')
        if parts[0] not in (b'HTTP/1.0', b'HTTP/1.1'):
            raise MessageError(f'unsupported response version: {parts[0]!r}')
        status = parse_status(parts[1])
        if status is None:
            raise MessageError(f'invalid status code: {parts[1]!r}')
        start = (parts[0], status)
    values_need_checking = not _block_values_are_clean(head)
    known = cache
    do_lookup = len(known) > 0
    raw: list[tuple[bytes, bytes]] = []
    index: dict[bytes, list[tuple[bytes, bytes]]] = {}
    for line in lines[first + 1:]:
        if not line:
            continue
        cacheable = len(line) <= _LINE_CACHE_MAX_LINE
        if cacheable:
            hit = known.get(line) if do_lookup else None
            if hit is None:
                hit = _DEFAULT_LINES.get(line)
            if hit is not None:
                # Proved clean when admitted; its bytes have not changed.
                raw.append(hit)
                same = index.get(hit[0])
                if same is None:
                    index[hit[0]] = [hit]
                else:
                    same.append(hit)
                continue
        try:
            lkey, value = field_line(line, check=values_need_checking)
            if lkey in _UNDERSCORE_FRAMING_NAMES:
                raise FieldError(f'framing-confusable header name {lkey!r} '
                                 f'(NORM-UNDERSCORE)')
            # Judged before OWS is stripped.
            if lkey == b'content-length' and not _CL_STRICT_RE.match(line[len(lkey) + 1:]):
                raise FieldError(f'ambiguous Content-Length value '
                                 f'{line[len(lkey) + 1:]!r} (RFC 9110 §8.6)')
        except FieldError as exc:
            raise MessageError(str(exc)) from None
        pair = (lkey, value)
        raw.append(pair)
        same = index.get(lkey)
        if same is None:
            index[lkey] = [pair]
        else:
            same.append(pair)
        # Admission is tested against the resulting total, so no line steps over it.
        if (cacheable
                and len(known) < _LINE_CACHE_MAX
                and known.size + len(line) <= _LINE_CACHE_MAX_BYTES):
            known[line] = pair
            known.size += len(line)
    # RFC 9110 §8.3: Content-Type is a singleton (COMP-DUPLICATE-CT).
    content_types = index.get(b'content-type')
    if content_types is not None and len(content_types) > 1:
        raise MessageError('multiple Content-Type headers')
    return start, raw, index


# --- persistence (RFC 9112 §9.3) -------------------------------------------

def connection_options(headers) -> list[bytes]:
    """The lowercase ``Connection`` options of a ``Headers``, or
    [`MessageError`][] for one that is not a token."""
    fields = headers._index.get(b'connection')
    if not fields:
        return []
    options = list_members(b','.join([value for _, value in fields]))
    for option in options:
        if option.translate(None, TCHAR_OCTETS):
            raise MessageError(f'invalid Connection option {option!r}')
    return options


def persistent(version: bytes, headers) -> bool:
    """Whether the connection persists after this message: ``close`` ends it,
    and HTTP/1.0 needs ``keep-alive``."""
    fields = headers._index.get(b'connection')
    if not fields:
        return version != b'HTTP/1.0'
    if len(fields) == 1:
        # The common spellings need no list parse.
        value = fields[0][1]
        if value == b'close':
            return False
        if value == b'keep-alive':
            return True
    options = connection_options(headers)
    if b'close' in options:
        return False
    return version != b'HTTP/1.0' or b'keep-alive' in options


# --- role policy: body length (RFC 9112 §6) --------------------------------
# The rules that differ by role.  Field lines (§5), chunk-ext (§7.1.1) and the
# trailer section (§7.1.2) are read alike by both.

NO_BODY = 'none'
LENGTH = 'length'
CHUNKED = 'chunked'
CLOSE_DELIMITED = 'close'


def request_framing(headers) -> tuple[int | None, bool]:
    """``(Content-Length or None, chunked)`` of a request's ``Headers``.

    RFC 9112 §6.1: a request has no close-delimited body, so a
    ``Transfer-Encoding`` other than exactly ``chunked`` cannot be read:
    chunked not alone at the end is [`MessageError`][], another coding or a
    parameter is [`Unimplemented`][].  §6.3: CL with TE is an error.
    """
    index = headers._index
    tes = index.get(b'transfer-encoding')
    cls = index.get(b'content-length')
    if not tes:
        if not cls:
            return None, False
        try:
            return parse_content_length(cls), False
        except ValueError as exc:
            raise MessageError(str(exc)) from exc
    if cls:
        raise MessageError('Content-Length and Transfer-Encoding both present '
                           '(smuggling vector)')
    try:
        members = split_transfer_codings(tes)
    except ValueError as exc:
        raise MessageError(str(exc)) from exc
    if members == [(b'chunked', ())]:
        return None, True
    codings = [name for name, _params in members]
    if b'chunked' not in codings:
        raise Unimplemented(f'Transfer-Encoding {codings!r} is not implemented')
    if codings[-1] != b'chunked' or codings.count(b'chunked') > 1:
        raise MessageError(f'Transfer-Encoding with chunked not the sole final '
                           f'coding: {codings!r}')
    raise Unimplemented(f'Transfer-Encoding {members!r} applies an unimplemented '
                        f'coding or parameter with chunked')


def response_framing(headers, method, status: int
                     ) -> tuple[str, int | None, bool, bool]:
    """``(mode, length, reusable, switched)`` of a response to *method*.

    RFC 9112 §6.3: method and status come first (HEAD, 1xx, 204, 304 have no
    body; a 2xx to CONNECT and a 101 switch protocols).  A response whose
    final coding is not chunked ends at the close (item 4), where a request
    is refused.  A 1xx or 204 declaring a body is not reusable (§6.1).
    """
    index = headers._index
    if method_is(method, 'CONNECT') and 200 <= status < 300:
        return NO_BODY, None, False, True
    switched = status == 101
    if not response_has_content(method, status):
        # Fields are read only here, so a HEAD or 304 never parses them.
        if (is_informational(status) or status == 204) and (
                index.get(b'transfer-encoding')
                or (content_length(index) or 0) != 0):
            return NO_BODY, None, False, False
        return NO_BODY, None, not switched, switched
    tes = index.get(b'transfer-encoding')
    if tes:
        if index.get(b'content-length'):
            raise MessageError('Content-Length and Transfer-Encoding both present '
                               '(response-splitting vector)')
        try:
            members = split_transfer_codings(tes)
        except ValueError as exc:
            raise MessageError(str(exc)) from exc
        # RFC 9110 §5.6.1: empty list members are ignored.
        codings = [name for name, _params in members if name]
        if codings.count(b'chunked') > 1:
            raise MessageError(f'chunked applied more than once: {codings!r}')
        if codings and codings[-1] == b'chunked':
            return CHUNKED, None, True, False
        return CLOSE_DELIMITED, None, False, False
    declared = content_length(index)
    if declared is not None:
        return LENGTH, declared, True, False
    return CLOSE_DELIMITED, None, False, False


def content_length(index: dict) -> int | None:
    """The agreed ``Content-Length`` in a ``Headers`` index, or ``None``."""
    try:
        return parse_content_length(index.get(b'content-length', ()))
    except ValueError as exc:
        raise MessageError(str(exc)) from exc


# --- chunked body (RFC 9112 §7.1) ------------------------------------------

_HEXDIG = frozenset(b'0123456789abcdefABCDEF')


def _chunk_size(line: bytes) -> int:
    """``chunk-size [ chunk-ext ]`` of a CRLF-terminated chunk line."""
    if not line.endswith(_CRLF) or line.count(b'\n') != 1:
        raise MessageError(f'chunk-size line not CRLF-terminated: {line!r}')
    line = line[:-2]
    size, semicolon, _ext = line.partition(b';')
    if semicolon:
        # BWS before ';' only; no trailing OWS on a bare size.
        size = size.rstrip(b' \t')
        _check_chunk_ext(line[len(size):])
    if not size or not _HEXDIG.issuperset(size):
        raise MessageError(f'invalid chunk-size {size!r}')
    return int(size, 16)


def _check_chunk_ext(ext: bytes) -> None:
    """RFC 9112 §7.1.1 ``chunk-ext`` from its first ``;``."""
    for element in ext.split(b';')[1:]:
        name, eq, value = element.strip(b' \t').partition(b'=')
        name = name.rstrip(b' \t')
        if not name or name.translate(None, TCHAR_OCTETS):
            raise MessageError(f'invalid chunk-ext-name {name!r}')
        if eq:
            value = value.strip(b' \t')
            if value[:1] == b'"':
                if (len(value) < 2 or not value.endswith(b'"')
                        or value.translate(None, FIELD_VALUE_ALLOWED_OCTETS)):
                    raise MessageError(f'invalid quoted chunk-ext-val {value!r}')
            elif not value or value.translate(None, TCHAR_OCTETS):
                raise MessageError(f'invalid chunk-ext-val {value!r}')


class ChunkedDecoder:
    """Yield the data of a chunked body in slices, then read its trailer.

    *wait* runs each read as ``await wait(read, payload)``, where *payload* is
    whether the read returns body octets; a role bounds and meters reads there.
    Lines are bounded by *line_max*; the trailer section, when *trailer_max*,
    by that total.  EOF part-way raises ``EOFError``.
    """

    __slots__ = ('_reader', '_wait', '_line_max', '_trailer_max', '_slice', '_remaining')

    def __init__(self, reader, wait, *, line_max: int, slice_max: int,
                 trailer_max: int = 0) -> None:
        self._reader = reader
        self._wait = wait
        self._line_max = line_max
        self._trailer_max = trailer_max
        self._slice = max(slice_max, 1)
        self._remaining = 0

    async def next(self) -> bytes | None:
        """The next data slice, or ``None`` after the trailer section."""
        if not self._remaining:
            size = _chunk_size(await self._line())
            if size == 0:
                await self._trailer_section()
                return None
            self._remaining = size
        data = await self._wait(
            self._reader.read(min(self._remaining, self._slice)), True)
        if not data:
            raise EOFError('connection closed mid-chunk')
        self._remaining -= len(data)
        if not self._remaining:
            # Exactly CRLF: reading until one would swallow spill (SMUG-CHUNK-SPILL).
            if await self._wait(self._reader.readexactly(2), False) != _CRLF:
                raise MessageError('chunk-data not CRLF-terminated')
        return data

    async def _line(self) -> bytes:
        # To LF, so a bare-LF line fails the CRLF check instead of waiting.
        limit = self._line_max
        try:
            line = await self._wait(self._reader.readuntil(b'\n', limit), False)
        except ReadLimitExceeded as exc:
            raise FieldsTooLarge('chunk framing line exceeds its limit',
                                 len(exc.seen), limit) from None
        except asyncio.LimitOverrunError as exc:
            raise FieldsTooLarge('chunk framing line exceeds its limit',
                                 exc.consumed, limit) from None
        if limit and len(line) > limit:
            raise FieldsTooLarge('chunk framing line exceeds its limit',
                                 len(line), limit)
        return line

    async def _trailer_section(self) -> None:
        total = 0
        while (line := await self._line()) != _CRLF:
            if not line.endswith(_CRLF):
                raise MessageError(f'trailer line not CRLF-terminated: {line[-8:]!r}')
            total += len(line)
            if self._trailer_max and total > self._trailer_max:
                raise FieldsTooLarge('trailer section exceeds its limit',
                                     total, self._trailer_max, section=True)
            try:
                name, _value = field_line(line[:-2])
            except FieldError as exc:
                raise MessageError(f'trailer: {exc}') from None
            if name in PROHIBITED_TRAILER_FIELDS:
                raise MessageError(f'prohibited trailer field {name!r} (RFC 9110 §6.5.1)')


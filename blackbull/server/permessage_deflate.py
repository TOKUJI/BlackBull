"""permessage-deflate (RFC 7692) negotiation and per-message codec.

This module is small on purpose.  It does two things:

1. **Handshake**: parse a peer's ``Sec-WebSocket-Extensions`` offer, decide
   whether to accept ``permessage-deflate``, and produce the response-side
   ``Sec-WebSocket-Extensions`` header value.

2. **Per-connection state**: hold the streaming inflate/deflate state plus
   the ``*_no_context_takeover`` flags so :class:`WebSocketRecipient` can
   decompress inbound messages and :class:`WebSocketSender` can compress
   outbound ones.

The actual wire-level use of RSV1 happens in :mod:`ws_codec` and
:mod:`recipient`; this module just owns the policy + state.
"""
from __future__ import annotations

from dataclasses import dataclass
import zlib


# RFC 7692 §7.1 — the four parameters that may appear in the offer/response.
# We accept any window_bits value the peer offers in [8, 15]; we never demand
# a window-bits restriction ourselves.
_VALID_WBITS_RANGE = range(8, 16)

# Raw DEFLATE (no zlib header / trailer) — wbits negative selects this mode
# in CPython's zlib.  RFC 7692 §7.2 strips the four trailing bytes
# ``\x00\x00\xff\xff`` from each compressed message; the inflate side appends
# them back before feeding the inflater.
_DEFLATE_TAIL = b'\x00\x00\xff\xff'


@dataclass(slots=True)
class DeflateParams:
    """Per-connection permessage-deflate parameters as negotiated."""
    server_no_context_takeover: bool = False
    client_no_context_takeover: bool = False
    server_max_window_bits:     int = 15
    client_max_window_bits:     int = 15


def negotiate(offer_header: bytes | None) -> tuple[DeflateParams | None, bytes | None]:
    """Decide whether to accept permessage-deflate based on the client's offer.

    Returns ``(params, response_value)`` where:

    * ``params`` is the :class:`DeflateParams` to install on the connection
      (or ``None`` to decline).
    * ``response_value`` is the bytes to put after
      ``Sec-WebSocket-Extensions:`` in the 101 response (or ``None`` when
      declining).

    Policy: accept context-takeover by default on both sides (better
    compression).  Honour ``server_no_context_takeover`` and
    ``client_no_context_takeover`` when the client asks for them.  Ignore
    unknown parameter names — that's what RFC 7692 §5.1 calls for.

    ``offer_header`` is the raw bytes of the client's
    ``Sec-WebSocket-Extensions`` header (or ``None`` when absent).  The
    client may send multiple comma-separated offers; we pick the first one
    that names ``permessage-deflate`` and that we can satisfy.
    """
    if not offer_header:
        return None, None

    for raw_offer in _split_offers(offer_header):
        try:
            name, params = _parse_offer(raw_offer)
        except ValueError:
            continue
        if name != b'permessage-deflate':
            continue

        # Reject offers with invalid window-bits *values* (e.g. =7 / =16).
        # Empty values (no `=`) are fine — the peer signals support, we pick.
        sb = params.get(b'server_max_window_bits')
        cb = params.get(b'client_max_window_bits')
        if sb is not None and sb is not _EMPTY and sb not in _VALID_WBITS_RANGE:
            continue
        if cb is not None and cb is not _EMPTY and cb not in _VALID_WBITS_RANGE:
            continue

        accepted = DeflateParams(
            server_no_context_takeover=b'server_no_context_takeover' in params,
            client_no_context_takeover=b'client_no_context_takeover' in params,
            server_max_window_bits=sb if isinstance(sb, int) else 15,
            client_max_window_bits=cb if isinstance(cb, int) else 15,
        )
        return accepted, _render(accepted)
    return None, None


class _Empty:
    """Sentinel for parameters that appear without an ``=value``."""
    __slots__ = ()
    def __repr__(self) -> str:  # pragma: no cover - debug only
        return '<empty>'

_EMPTY = _Empty()


def _split_offers(header: bytes) -> list[bytes]:
    """Split a Sec-WebSocket-Extensions header into individual offers.

    Multiple extensions are separated by ``,``; offer parameters by ``;``.
    We keep this lexical and don't try to validate the whole RFC 7230 grammar
    — the offers we actually accept go through :func:`_parse_offer` next.
    """
    return [p.strip() for p in header.split(b',') if p.strip()]


def _parse_offer(offer: bytes) -> tuple[bytes, dict[bytes, object]]:
    """Parse a single offer of the form ``name; key=value; flag``.

    Returns the extension name and a dict of parameters.  Values are
    coerced to ``int`` for window-bits parameters, kept as bytes otherwise.
    Bare flag parameters (no ``=``) map to ``_EMPTY``.
    """
    parts = [p.strip() for p in offer.split(b';') if p.strip()]
    if not parts:
        raise ValueError('empty offer')
    name = parts[0].lower()
    params: dict[bytes, object] = {}
    for raw in parts[1:]:
        if b'=' in raw:
            key, _, value = raw.partition(b'=')
            key = key.strip().lower()
            value = value.strip().strip(b'"')
            if key in (b'server_max_window_bits', b'client_max_window_bits'):
                try:
                    params[key] = int(value)
                except ValueError as exc:
                    raise ValueError(f'invalid integer for {key!r}: {value!r}') from exc
            else:
                params[key] = value
        else:
            params[raw.strip().lower()] = _EMPTY
    return name, params


def _render(p: DeflateParams) -> bytes:
    """Produce the Sec-WebSocket-Extensions response value for ``p``.

    Emit only non-default parameters — ``server_max_window_bits`` and
    ``client_max_window_bits`` are omitted when they are 15 (the default).
    """
    out = [b'permessage-deflate']
    if p.server_no_context_takeover:
        out.append(b'server_no_context_takeover')
    if p.client_no_context_takeover:
        out.append(b'client_no_context_takeover')
    if p.server_max_window_bits != 15:
        out.append(b'server_max_window_bits=' + str(p.server_max_window_bits).encode())
    if p.client_max_window_bits != 15:
        out.append(b'client_max_window_bits=' + str(p.client_max_window_bits).encode())
    return b'; '.join(out)


class InboundDecompressor:
    """Streaming decompressor for inbound permessage-deflate messages.

    The peer (client) is doing the compression here, so the ``no_context_takeover``
    flag we care about is ``client_no_context_takeover``.  When set, the inflater
    is recreated for every message; otherwise the same inflater is reused so
    the sliding-window context carries forward across messages.
    """
    __slots__ = ('_wbits', '_reset_per_message', '_inflater')

    def __init__(self, wbits: int, reset_per_message: bool):
        # wbits is the *client*'s compression window; receiver inflates with
        # the negative form to skip zlib header parsing.
        self._wbits = -wbits
        self._reset_per_message = reset_per_message
        self._inflater = zlib.decompressobj(wbits=self._wbits)

    def decompress(self, payload: bytes) -> bytes:
        """Inflate one whole compressed message.  Raises ``zlib.error`` on bad data."""
        out = self._inflater.decompress(payload + _DEFLATE_TAIL)
        if self._reset_per_message:
            self._inflater = zlib.decompressobj(wbits=self._wbits)
        return out


class OutboundCompressor:
    """Streaming compressor for outbound permessage-deflate messages.

    Symmetric to :class:`InboundDecompressor`: the ``no_context_takeover`` flag
    that matters here is ``server_no_context_takeover`` (we are the server),
    deciding whether to reset the deflater between messages.
    """
    __slots__ = ('_wbits', '_reset_per_message', '_deflater')

    def __init__(self, wbits: int, reset_per_message: bool):
        self._wbits = -wbits
        self._reset_per_message = reset_per_message
        self._deflater = zlib.compressobj(wbits=self._wbits, level=zlib.Z_DEFAULT_COMPRESSION)

    def compress(self, payload: bytes) -> bytes:
        """Compress one whole message; strip the trailing 0x00 0x00 0xff 0xff per RFC 7692 §7.2.1."""
        out = self._deflater.compress(payload) + self._deflater.flush(zlib.Z_SYNC_FLUSH)
        if out.endswith(_DEFLATE_TAIL):
            out = out[:-4]
        if self._reset_per_message:
            self._deflater = zlib.compressobj(wbits=self._wbits, level=zlib.Z_DEFAULT_COMPRESSION)
        return out

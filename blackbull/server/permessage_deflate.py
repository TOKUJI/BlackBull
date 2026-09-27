"""permessage-deflate (RFC 7692) negotiation and per-message codec.

This module is small on purpose.  It does two things:

1. **Handshake**: parse a peer's ``Sec-WebSocket-Extensions`` offer, decide
   whether to accept ``permessage-deflate``, and produce the response-side
   ``Sec-WebSocket-Extensions`` header value.

2. **Per-connection state**: hold the streaming inflate/deflate state plus
   the ``*_no_context_takeover`` flags so ``WebSocketRecipient`` can
   decompress inbound messages and ``WebSocketSender`` can compress
   outbound ones.

The actual wire-level use of RSV1 happens in [`ws_codec`][] and
[`recipient`][]; this module just owns the policy + state.
"""
from __future__ import annotations

from dataclasses import dataclass
import zlib

from .ws_codec import MessageTooLarge


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

    * ``params`` is the [`DeflateParams`][] to install on the connection
      (or ``None`` to decline).
    * ``response_value`` is the bytes to put after
      ``Sec-WebSocket-Extensions:`` in the 101 response (or ``None`` when
      declining).

    Policy: accept context-takeover by default on both sides (better
    compression).  Honour ``server_no_context_takeover`` and
    ``client_no_context_takeover`` when the client asks for them.

    ``offer_header`` is the raw bytes of the client's
    ``Sec-WebSocket-Extensions`` header (or ``None`` when absent).  The
    client may send multiple comma-separated offers; we pick the first one
    that names ``permessage-deflate`` and that we can satisfy.
    """
    if not offer_header:
        return None, None

    for raw_offer in offer_header.split(b','):
        accepted = _accept_offer(raw_offer)
        if accepted is not None:
            return accepted, _render(accepted)
    return None, None


def _accept_offer(raw_offer: bytes) -> DeflateParams | None:
    """Parse one ``;``-separated offer and validate it; ``None`` declines.

    RFC 7692 §7.1.1 requires declining an offer with a parameter not
    defined for an offer, an invalid value, a repeated name, or a
    configuration the server does not support — including a window this
    runtime's zlib cannot instantiate (8, on CPython, measured into
    ``_SERVED_WBITS`` once per process).  An unusable window declines the
    offer; it is never rounded up past the peer's constraint.

    One pass over the offer, in three stages per parameter: lexical
    splitting, the §7.1.1 checks, and the params assembly.
    """
    seen: set[bytes] = set()
    snc = cnc = False
    sb = cb = None
    is_extension_name = True
    for raw in raw_offer.split(b';'):
        part = raw.strip()
        if not part:
            continue
        if is_extension_name:
            is_extension_name = False
            if part.lower() != b'permessage-deflate':
                return None
            continue
        # Lexical: key=value with an optional quoted value.
        if b'=' in part:
            key, _, value = part.partition(b'=')
            key = key.strip().lower()
            value = value.strip()
            if value.startswith(b'"'):
                if len(value) < 2 or not value.endswith(b'"'):
                    return None
                value = value[1:-1]
            has_value = True
        else:
            key = part.lower()
            has_value = False
        # §7.1.1 checks: a repeated name, a value where none is allowed,
        # or an out-of-range window declines the offer.
        if key in seen:
            return None
        seen.add(key)
        if key in (b'server_no_context_takeover', b'client_no_context_takeover'):
            if has_value:
                return None
            if key == b'server_no_context_takeover':
                snc = True
            else:
                cnc = True
        elif key in (b'server_max_window_bits', b'client_max_window_bits'):
            if key == b'server_max_window_bits' and not has_value:
                return None       # §7.1.2.1: an offer carries the value
            if has_value:
                # A bare 8-15 decimal: ASCII digits, no leading zero.
                if not value.isdigit() or value.startswith(b'0'):
                    return None
                w = int(value)
                if not 8 <= w <= 15:
                    return None
                if key == b'server_max_window_bits':
                    sb = w
                else:
                    cb = w
        else:
            return None
    # Assembly: absent constraints stay at the defaults.
    server_w = sb if sb is not None else 15
    client_w = cb if cb is not None else 15
    if server_w not in _SERVED_WBITS or client_w not in _SERVED_WBITS:
        return None
    return DeflateParams(
        server_no_context_takeover=snc,
        client_no_context_takeover=cnc,
        server_max_window_bits=server_w,
        client_max_window_bits=client_w,
    )


# The windows whose codecs this runtime can build and use: measured once
# per process after the codec classes below, never per handshake.
def _measure_served_wbits() -> frozenset[int]:
    served = set()
    for w in range(8, 16):
        try:
            out = OutboundCompressor(w, reset_per_message=False)
            inc = InboundDecompressor(w, reset_per_message=False)
            if inc.decompress(out.compress(b'x')) == b'x':
                served.add(w)
        except (ValueError, zlib.error):
            pass
    return frozenset(served)


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

    def decompress(self, payload: bytes, *, max_length: int | None = None) -> bytes:
        """Inflate one whole compressed message.  Raises ``zlib.error`` on bad data.

        With *max_length* set, the inflated output is bounded by zlib
        itself rather than measured after the fact: a bomb is refused
        without ever being built.  Asking for ``max_length + 1`` is what
        makes "exactly at the bound" and "one byte over" distinguishable
        in a single call — zlib stops at the byte it was given, so a
        return of exactly ``max_length`` bytes is ambiguous on its own.

        Raises [`MessageTooLarge`][] when the peer's message inflates
        past the bound.  ``unconsumed_tail`` is the other half of that
        check: zlib stops early when it hits the limit, so leftover input
        means there was more to come even if the output landed on the
        boundary.
        """
        if max_length is None:
            out = self._inflater.decompress(payload + _DEFLATE_TAIL)
        else:
            out = self._inflater.decompress(payload + _DEFLATE_TAIL,
                                            max_length=max_length + 1)
            if len(out) > max_length or self._inflater.unconsumed_tail:
                # The inflater is now mid-message and its window holds a
                # partial result.  The connection is closing, so dropping
                # it is the honest end state — a reused inflater would
                # decode the next message against a corrupt context.
                self._inflater = zlib.decompressobj(wbits=self._wbits)
                raise MessageTooLarge(len(out), max_length)
        if self._reset_per_message:
            self._inflater = zlib.decompressobj(wbits=self._wbits)
        return out


class OutboundCompressor:
    """Streaming compressor for outbound permessage-deflate messages.

    Symmetric to [`InboundDecompressor`][]: the ``no_context_takeover`` flag
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


_SERVED_WBITS = _measure_served_wbits()

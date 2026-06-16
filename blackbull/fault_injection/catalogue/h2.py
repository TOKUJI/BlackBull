"""Four named HTTP/2 misbehaviour scenarios.

See :mod:`blackbull.fault_injection.catalogue` for the catalogue
overview and the four spec-grade categories.

Each builder returns a :class:`~blackbull.fault_injection.ScenarioH2`.
The scenarios are deliberately tight: just enough state for the named
pathology, nothing more.  Real client / proxy / security-research
suites can stack ``parametrize`` over the catalogue to assert
resilience across all four categories with a few lines of test code.
"""
from __future__ import annotations

from blackbull.fault_injection.scenario_h2 import (
    CloseGracefully,
    ScenarioH2,
    SendRawBytes,
    Sleep,
    WaitForClientFrame,
)


# Setting identifier numbers (RFC 9113 §6.5.2).
_SETTING_INITIAL_WINDOW_SIZE = 0x4
_SETTING_MAX_FRAME_SIZE = 0x5

# Minimum legal value of SETTINGS_MAX_FRAME_SIZE per RFC 9113 §6.5.2.
_MAX_FRAME_SIZE_MIN_LEGAL = 16_384


def _encode_frame(length: int, type_byte: int, flags: int,
                  stream_id: int, payload: bytes = b'') -> bytes:
    """RFC 9113 §4.1 — 9-byte frame header + payload."""
    return (
        length.to_bytes(3, 'big')
        + type_byte.to_bytes(1, 'big')
        + flags.to_bytes(1, 'big')
        + (stream_id & 0x7fffffff).to_bytes(4, 'big')
        + payload
    )


# Wire payload for a minimal HEADERS frame announcing ``:status 200``.
# Built as raw bytes — sending it through SendRawBytes keeps the
# scenario independent of the FrameFactory's HPACK encoder, which
# would otherwise complicate JSON round-trip on the catalogue.
# Static-table index 8 (":status 200"); single byte 0x88 with the
# indexed-header bit set is sufficient for a status-only HEADERS.
_HEADERS_STATUS_200 = b'\x88'


def half_closed_stream_no_data() -> ScenarioH2:
    """Server sends HEADERS without END_STREAM, then nothing.

    A real H2 server would follow HEADERS with DATA + END_STREAM.
    This scenario stops mid-stream — flags END_HEADERS but not
    END_STREAM, then sleeps until the client gives up.  Clients
    must time out or RST_STREAM rather than block forever.
    """
    type_headers = 0x01
    flags_end_headers_only = 0x04  # END_HEADERS without END_STREAM
    headers_frame = _encode_frame(
        length=len(_HEADERS_STATUS_200),
        type_byte=type_headers,
        flags=flags_end_headers_only,
        stream_id=1,
        payload=_HEADERS_STATUS_200,
    )
    return ScenarioH2(
        steps=(
            WaitForClientFrame(match={'type': 'HEADERS', 'stream_id': 1},
                               timeout=5.0),
            SendRawBytes(headers_frame),
            # Hold the stream open without sending the DATA frame the
            # client is waiting for.  Long enough to outlast a normal
            # client read timeout.
            Sleep(duration=3.0),
            CloseGracefully(error_code=0, last_stream_id=1),
        ),
        send_preface=True,
    )


def exhausted_window_zero_initial() -> ScenarioH2:
    """Server advertises SETTINGS_INITIAL_WINDOW_SIZE = 0.

    RFC 9113 §6.9.2 — a zero initial window means the client cannot
    send any DATA bytes until the server grants WINDOW_UPDATE.  This
    scenario never grants WINDOW_UPDATE, so a client trying to send
    a request body must respect the zero window rather than spin or
    flood the wire.
    """
    return ScenarioH2(
        steps=(
            WaitForClientFrame(match={'type': 'HEADERS', 'stream_id': 1},
                               timeout=5.0),
            # No DATA-budget grant ever arrives — client must block on
            # the request body until it gives up or the server closes.
            Sleep(duration=2.0),
            CloseGracefully(error_code=0, last_stream_id=1),
        ),
        send_preface=True,
        initial_settings=((_SETTING_INITIAL_WINDOW_SIZE, 0),),
    )


def settings_max_frame_size_below_minimum() -> ScenarioH2:
    """Server advertises SETTINGS_MAX_FRAME_SIZE below the legal floor.

    RFC 9113 §6.5.2 — SETTINGS_MAX_FRAME_SIZE values below 16_384 are
    a PROTOCOL_ERROR.  The client must close the connection with
    PROTOCOL_ERROR rather than try to honour the illegal value (e.g.
    splitting frames into 1-byte chunks).
    """
    return ScenarioH2(
        steps=(
            # No further server work — the illegal SETTINGS in the
            # handshake is the entire payload.
            Sleep(duration=2.0),
        ),
        send_preface=True,
        initial_settings=(
            (_SETTING_MAX_FRAME_SIZE, _MAX_FRAME_SIZE_MIN_LEGAL - 1),
        ),
    )


def headers_continuation_dropped() -> ScenarioH2:
    """Server sends HEADERS without END_HEADERS, then no CONTINUATION.

    RFC 9113 §6.2 / §6.10 — HEADERS without END_HEADERS MUST be
    followed by CONTINUATION frames for the same stream until one
    sets END_HEADERS.  This scenario sends the partial HEADERS and
    then nothing, leaving the stream in an unresolved header-block
    state.  Clients must enforce a timeout (or PROTOCOL_ERROR on
    intervening frame for a different stream) rather than hang
    indefinitely.
    """
    type_headers = 0x01
    flags_no_end_headers = 0x00  # neither END_HEADERS nor END_STREAM
    partial_headers = _encode_frame(
        length=len(_HEADERS_STATUS_200),
        type_byte=type_headers,
        flags=flags_no_end_headers,
        stream_id=1,
        payload=_HEADERS_STATUS_200,
    )
    return ScenarioH2(
        steps=(
            WaitForClientFrame(match={'type': 'HEADERS', 'stream_id': 1},
                               timeout=5.0),
            SendRawBytes(partial_headers),
            # No CONTINUATION ever follows.
            Sleep(duration=3.0),
            CloseGracefully(error_code=0, last_stream_id=1),
        ),
        send_preface=True,
    )

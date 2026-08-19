"""Named HTTP/2 client-side misbehaviour cases.

Each entry is one thing a real client does wrong to a server, named so a
suite can ``parametrize`` over the set and report which case broke it.

The HTTP/2 rows of the attack-surface audit are what this set is drawn
from, so the names line up with the defences on the other side: if
``rapid_reset_burst`` stops failing, the meter that answers it changed.
"""
from __future__ import annotations

from blackbull.protocol.frame_types import ErrorCodes, FrameTypes, SettingFrameFlags

from ..scenario_h2_client import (
    Abort, ReadResponse, ScenarioH2Client, SendRawBytes, SendFrame, SendPreface, Sleep,
)

#: A minimal, valid SETTINGS frame — the handshake a well-behaved client
#: sends straight after the preface.
_EMPTY_SETTINGS = SendFrame(FrameTypes.SETTINGS, flags=0, stream_id=0)


def preface_never_arrives() -> ScenarioH2Client:
    """Connect, send nothing, hold the connection open.

    What `BB_HEADER_TIMEOUT` and the connection-detect deadline answer.
    """
    return ScenarioH2Client(name='preface_never_arrives', steps=(
        Sleep(30.0),
    ))


def preface_trickled() -> ScenarioH2Client:
    """The preface, one byte every 100 ms — legal bytes, hostile pacing."""
    return ScenarioH2Client(name='preface_trickled', steps=(
        SendRawBytes(b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n', byte_interval=0.1),
        _EMPTY_SETTINGS,
        ReadResponse(timeout=2.0),
    ))


def rapid_reset_burst() -> ScenarioH2Client:
    """CVE-2023-44487 — open streams and reset them immediately.

    Each HEADERS costs the server a stream; each RST_STREAM frees it before
    `SETTINGS_MAX_CONCURRENT_STREAMS` ever bites, so the cost is unbounded
    without a rate meter.
    """
    steps = [SendPreface(), _EMPTY_SETTINGS]
    for i in range(1, 60, 2):
        steps.append(SendFrame(FrameTypes.HEADERS, flags=0x04, stream_id=i,
                               data=b'\x82\x84\x86'))
        steps.append(SendFrame(FrameTypes.RST_STREAM, stream_id=i,
                               data=int(ErrorCodes.CANCEL).to_bytes(4, 'big')))
    steps.append(ReadResponse(timeout=2.0))
    return ScenarioH2Client(name='rapid_reset_burst', steps=tuple(steps))


def ping_flood() -> ScenarioH2Client:
    """CVE-2019-9512 — every PING obliges an ACK write, at no byte cost."""
    steps = [SendPreface(), _EMPTY_SETTINGS]
    steps += [SendFrame(FrameTypes.PING, stream_id=0, data=b'\x00' * 8)
              for _ in range(60)]
    steps.append(ReadResponse(timeout=2.0))
    return ScenarioH2Client(name='ping_flood', steps=tuple(steps))


def settings_flood() -> ScenarioH2Client:
    """CVE-2019-9515 — same shape, SETTINGS instead of PING."""
    steps = [SendPreface()]
    steps += [SendFrame(FrameTypes.SETTINGS, stream_id=0) for _ in range(60)]
    steps.append(ReadResponse(timeout=2.0))
    return ScenarioH2Client(name='settings_flood', steps=tuple(steps))


def empty_continuation_flood() -> ScenarioH2Client:
    """CVE-2024-27983 shape — a header block extended by zero-length frames.

    No byte budget can see these: they add nothing to the block, so the
    defence has to count them.
    """
    steps = [SendPreface(), _EMPTY_SETTINGS,
             SendFrame(FrameTypes.HEADERS, flags=0x00, stream_id=1,
                       data=b'\x82')]
    steps += [SendFrame(FrameTypes.CONTINUATION, flags=0x00, stream_id=1)
              for _ in range(60)]
    steps.append(ReadResponse(timeout=2.0))
    return ScenarioH2Client(name='empty_continuation_flood',
                            steps=tuple(steps))


def header_block_never_finished() -> ScenarioH2Client:
    """HEADERS without END_HEADERS, then silence.

    HPACK state is connection-wide, so the server cannot abandon this
    per-stream — the reason its answer is a connection error.
    """
    return ScenarioH2Client(name='header_block_never_finished', steps=(
        SendPreface(),
        _EMPTY_SETTINGS,
        SendFrame(FrameTypes.HEADERS, flags=0x00, stream_id=1, data=b'\x82'),
        Sleep(30.0),
    ))


def data_frame_lies_about_length() -> ScenarioH2Client:
    """A frame header declaring 100 payload bytes, carrying 2."""
    return ScenarioH2Client(name='data_frame_lies_about_length', steps=(
        SendPreface(),
        _EMPTY_SETTINGS,
        SendFrame(FrameTypes.HEADERS, flags=0x04, stream_id=1,
                  data=b'\x82\x84\x86'),
        SendFrame(FrameTypes.DATA, stream_id=1, data=b'ab',
                  declared_length=100),
        Sleep(30.0),
    ))


def unknown_frame_type() -> ScenarioH2Client:
    """RFC 9113 §4.1 — an unregistered type MUST be ignored, not fatal."""
    return ScenarioH2Client(name='unknown_frame_type', steps=(
        SendPreface(),
        _EMPTY_SETTINGS,
        SendFrame(0xfa, stream_id=0, data=b'whatever'),
        ReadResponse(timeout=2.0),
    ))


def settings_ack_with_payload() -> ScenarioH2Client:
    """§6.5 — a SETTINGS frame with ACK set MUST have an empty payload."""
    return ScenarioH2Client(name='settings_ack_with_payload', steps=(
        SendPreface(),
        SendFrame(FrameTypes.SETTINGS, flags=int(SettingFrameFlags.ACK),
                  stream_id=0, data=b'\x00' * 6),
        ReadResponse(timeout=2.0),
    ))


def abort_mid_header_block() -> ScenarioH2Client:
    """Open a header block, then RST the transport rather than the stream."""
    return ScenarioH2Client(name='abort_mid_header_block', steps=(
        SendPreface(),
        _EMPTY_SETTINGS,
        SendFrame(FrameTypes.HEADERS, flags=0x00, stream_id=1, data=b'\x82'),
        Abort(),
    ))


#: Every case, for ``parametrize``.
CATALOGUE = {
    fn.__name__: fn for fn in (
        preface_never_arrives,
        preface_trickled,
        rapid_reset_burst,
        ping_flood,
        settings_flood,
        empty_continuation_flood,
        header_block_never_finished,
        data_frame_lies_about_length,
        unknown_frame_type,
        settings_ack_with_payload,
        abort_mid_header_block,
    )
}

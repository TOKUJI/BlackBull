"""What the HTTP/2 client advertises in SETTINGS, and what enforces it.

The client sent an empty SETTINGS frame, so it announced nothing at all —
not the field-section size its own HPACK decoder would refuse, and not the
push it may not want.  Three changes land together because they share one
method: ``FrameFactory.settings()`` could express neither identifier.

The invariant every test here is written against: **a value the client
advertises and the mechanism that enforces it are built from one setting**.
An advertisement the decoder does not honour is a lie to the peer; a
decoder limit the peer is never told is a refusal it could not have
avoided.  So the tests that matter are the ones that move a single setting
and watch *both* ends follow.

``settings()`` is shared with the server, which calls it on every
connection.  The no-argument call producing an empty payload, and the
server's own three-argument call producing exactly the bytes it produced
before, are the regression guards for that.
"""
from __future__ import annotations

import asyncio

import pytest
from hpack import Encoder

from blackbull.client.http2 import HTTP2Client
from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import (ErrorCodes, FrameTypes,
                                            HeaderFrameFlags,
                                            SettingFrameFlags)
from blackbull.server.recipient import AbstractReader
from blackbull.server.sender import AbstractWriter


# ---------------------------------------------------------------------------
# Wire helpers
# ---------------------------------------------------------------------------

def _frame(type_: FrameTypes, flags: int, stream_id: int,
           payload: bytes = b'') -> bytes:
    return (len(payload).to_bytes(3, 'big') + type_.value + bytes([flags])
            + stream_id.to_bytes(4, 'big') + payload)


def _settings_ack() -> bytes:
    return _frame(FrameTypes.SETTINGS, int(SettingFrameFlags.ACK), 0)


def _push_promise(block: bytes, *, parent: int = 1, promised: int = 2) -> bytes:
    payload = promised.to_bytes(4, 'big') + block
    return _frame(FrameTypes.PUSH_PROMISE,
                  int(HeaderFrameFlags.END_HEADERS), parent, payload)


def _headers(block: bytes, *, stream_id: int = 1) -> bytes:
    flags = int(HeaderFrameFlags.END_HEADERS) | int(HeaderFrameFlags.END_STREAM)
    return _frame(FrameTypes.HEADERS, flags, stream_id, block)


class _RecordingWriter(AbstractWriter):
    """Records every buffer written, which is one frame per call here."""

    def __init__(self) -> None:
        self.frames: list[bytes] = []

    async def write(self, data: bytes) -> None:
        self.frames.append(data)

    async def writelines(self, parts) -> None:
        self.frames.append(b''.join(parts))


class _FakeRawWriter:
    """The preface sink.  ``_start`` writes the preface here, not through
    the sender."""

    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


class _CannedReader(AbstractReader):
    """Feeds queued bytes, then EOF — a peer that said its piece and left."""

    def __init__(self, data: bytes = b'') -> None:
        self._buf = bytearray(data)

    async def read(self, n: int) -> bytes:
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    async def readexactly(self, n: int) -> bytes:
        if len(self._buf) < n:
            raise asyncio.IncompleteReadError(bytes(self._buf), n)
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk


async def _connected(peer_bytes: bytes = b'') -> HTTP2Client:
    """A started client whose receive loop has run to the peer's EOF.

    Goes through ``_start`` rather than setting attributes, so the SETTINGS
    frame under test is the one the client actually sends and the GOAWAY
    goes out through a real sender — a hand-installed ``_control_sender``
    would let a missing GOAWAY pass as an assertion.
    """
    c = HTTP2Client('localhost', 1)
    c._raw_writer = _FakeRawWriter()          # type: ignore[assignment]
    c._writer = _RecordingWriter()            # type: ignore[assignment]
    c._reader = _CannedReader(peer_bytes)     # type: ignore[assignment]
    await c._start()
    assert c._receive_task is not None
    await asyncio.wait_for(c._receive_task, 1.0)
    return c


def _written(c: HTTP2Client, type_: FrameTypes) -> list:
    """Every frame of *type_* the client put on the wire, parsed back."""
    parser = FrameFactory()
    out = []
    for buf in c._writer.frames:            # type: ignore[union-attr]
        if len(buf) >= 4 and buf[3:4] == type_.value:
            out.append(parser.load(buf))
    return out


def _sent_settings(c: HTTP2Client):
    frames = [f for f in _written(c, FrameTypes.SETTINGS)
              if not f.flags & SettingFrameFlags.ACK]
    assert frames, 'the client sent no SETTINGS frame'
    return frames[0]


# ===========================================================================
# 7a — the factory can express ENABLE_PUSH and MAX_HEADER_LIST_SIZE
# ===========================================================================

class TestSettingsCanExpressBothIdentifiers:
    """``settings()`` emitted only 0x3, 0x8 and 0x4, so neither sibling
    could send what it needed to.  The parser on the other side —
    ``SettingFrame`` — has always understood both identifiers; only the
    emitter could not produce them."""

    @staticmethod
    def _roundtrip(**kwargs):
        """What ``settings()`` emits, read back by the server's own parser."""
        return FrameFactory().load(FrameFactory().settings(**kwargs).save())

    def test_enable_push_round_trips(self):
        parsed = self._roundtrip(enable_push=0)
        assert getattr(parsed, 'enable_push', None) == 0

    def test_max_header_list_size_round_trips(self):
        parsed = self._roundtrip(max_header_list_size=65536)
        assert getattr(parsed, 'max_header_list_size', None) == 65536

    def test_both_at_once_round_trip(self):
        parsed = self._roundtrip(enable_push=0, max_header_list_size=4096)
        assert getattr(parsed, 'enable_push', None) == 0
        assert getattr(parsed, 'max_header_list_size', None) == 4096

    def test_an_unset_parameter_emits_no_entry(self):
        """The ``is not None`` idiom: 0 is a value, absence is not.  A
        default that emitted ``ENABLE_PUSH=1`` would announce a position the
        caller never took."""
        parsed = self._roundtrip(max_header_list_size=4096)
        assert getattr(parsed, 'enable_push', None) is None

    def test_a_no_argument_call_still_emits_an_empty_payload(self):
        """The regression guard for the server, which shares this method."""
        raw = FrameFactory().settings().save()
        assert raw[:3] == b'\x00\x00\x00', 'the empty SETTINGS frame grew a payload'
        assert len(raw) == 9

    def test_the_servers_own_call_is_byte_identical(self):
        """``HTTP2Actor.run`` passes exactly these three.  Appending
        identifiers must not reorder or disturb them."""
        raw = FrameFactory().settings(
            enable_connect_protocol=True,
            initial_window_size=65535,
            max_concurrent_streams=100,
        ).save()
        assert raw[9:] == (b'\x00\x03\x00\x00\x00\x64'
                           b'\x00\x08\x00\x00\x00\x01'
                           b'\x00\x04\x00\x00\xff\xff')

    def test_an_ack_carries_no_payload_whatever_is_asked_for(self):
        """RFC 9113 §6.5 — an ACK's length MUST be 0."""
        raw = FrameFactory().settings(
            ack=True, enable_push=0, max_header_list_size=4096).save()
        assert raw[:3] == b'\x00\x00\x00'


# ===========================================================================
# 7b — one setting drives the advertisement and the decoder
# ===========================================================================

@pytest.mark.asyncio
class TestHeaderListSizeIsOneSetting:
    """``FrameFactory`` default-constructed its ``Decoder``, so the limit
    that actually refuses a field section was hpack's, not ours, and the
    peer was told nothing about it.

    RFC 9113 §6.5.2 calls the advertisement *advisory*: it is advice to the
    peer, and the decoder's limit is the defence.  Which is exactly why they
    must come from one number — advice that does not match the defence is
    worse than no advice.
    """

    @staticmethod
    def _oversized_block(target: int) -> bytes:
        """A field block whose *decoded* size clears *target*.

        hpack charges ``len(name) + len(value) + 32`` per field, so this is
        arithmetic on the decoded side, not the encoded one.
        """
        fields = []
        charged = 0
        i = 0
        while charged <= target:
            name, value = f'x-pad-{i}', 'v' * 64
            fields.append((name, value))
            charged += len(name) + len(value) + 32
            i += 1
        return Encoder().encode(fields)

    @staticmethod
    def _amplified_block() -> bytes:
        """A block small on the wire and large decoded.

        One 4000-octet value entered in the dynamic table and referenced
        twenty times: 3528 encoded octets, 80,740 charged.  Compression is
        why this bound cannot be a byte count taken off the wire.
        """
        return Encoder().encode([('x-big', 'v' * 4000)] * 20)

    async def test_the_default_is_advertised(self, monkeypatch):
        c = await _connected()
        assert getattr(_sent_settings(c), 'max_header_list_size', None) == 65536

    async def test_the_advertised_value_follows_the_setting(self, monkeypatch):
        monkeypatch.setenv('BB_CLIENT_H2_MAX_HEADER_LIST_SIZE', '4096')
        c = await _connected()
        assert getattr(_sent_settings(c), 'max_header_list_size', None) == 4096

    async def test_the_decode_boundary_follows_the_same_setting(self, monkeypatch):
        """The one that proves *one* setting rather than two coincidences:
        the number on the wire and the number that refuses a section are
        read off the same connection."""
        monkeypatch.setenv('BB_CLIENT_H2_MAX_HEADER_LIST_SIZE', '2048')
        block = self._oversized_block(2048)
        c = await _connected(_headers(block))

        assert getattr(_sent_settings(c), 'max_header_list_size', None) == 2048
        goaway = _written(c, FrameTypes.GOAWAY)
        assert goaway, 'a section over the advertised limit was accepted'
        assert goaway[0].error_code == ErrorCodes.COMPRESSION_ERROR
        assert c._failure is not None and '2048' in c._failure

    async def test_the_same_section_passes_under_a_larger_setting(self, monkeypatch):
        """The boundary moves with the setting — otherwise the test above
        only proves hpack has *a* limit."""
        monkeypatch.setenv('BB_CLIENT_H2_MAX_HEADER_LIST_SIZE', '65536')
        block = self._oversized_block(2048)
        c = await _connected(_headers(block))

        assert not _written(c, FrameTypes.GOAWAY), (
            'a section well under the configured limit was refused')

    async def test_the_amplified_section_is_refused_at_the_default(self):
        """The control for the test below, and the shape that motivates the
        bound: 3528 encoded octets decode to 80,740, so every bound counted
        in wire octets — the frame cap, the encoded field-block total — lets
        it through."""
        c = await _connected(_headers(self._amplified_block()))

        goaway = _written(c, FrameTypes.GOAWAY)
        assert goaway and goaway[0].error_code == ErrorCodes.COMPRESSION_ERROR

    async def test_zero_advertises_nothing_and_refuses_nothing(self, monkeypatch):
        """``0`` is the fault-injection escape every client bound has.  RFC
        9113 §6.5.2 makes the setting's initial value unlimited, so saying
        nothing *is* the way to say unlimited."""
        monkeypatch.setenv('BB_CLIENT_H2_MAX_HEADER_LIST_SIZE', '0')
        c = await _connected(_headers(self._amplified_block()))

        assert getattr(_sent_settings(c), 'max_header_list_size', None) is None
        assert not _written(c, FrameTypes.GOAWAY), (
            'a section was refused although the bound was disabled')


# ===========================================================================
# 7c — ENABLE_PUSH=0, and the ACK that makes it binding
# ===========================================================================

@pytest.mark.asyncio
class TestEnablePushDefaultsToTodaysBehaviour:
    """Advertising ``0`` without refusing a push is a MUST violation
    (§6.5.2); refusing one against BlackBull's own server, which does not
    yet read the peer's ENABLE_PUSH, breaks the pair.  So the mechanism
    ships and the default is what the client does today."""

    async def test_nothing_is_advertised_by_default(self):
        assert getattr(_sent_settings(await _connected()),
                       'enable_push', None) is None

    async def test_a_push_promise_is_accepted_by_default(self):
        block = Encoder().encode([(':method', 'GET'), (':path', '/pushed')])
        c = await _connected(_settings_ack() + _push_promise(block))

        assert not _written(c, FrameTypes.GOAWAY), (
            'a promise was refused although nothing was advertised')
        assert c._failure is None


@pytest.mark.asyncio
class TestEnablePushZero:
    """RFC 9113 §6.5.2 conditions the receiver's MUST on having *both* set
    the parameter and had it acknowledged, and §6.5.3 makes the ACK the
    synchronisation point.  A promise in the round-trip before the ACK is
    conforming traffic, so the refusal is generation-tracked, not a flag
    flipped at send time."""

    @pytest.fixture(autouse=True)
    def _push_refused(self, monkeypatch):
        monkeypatch.setenv('BB_CLIENT_H2_ENABLE_PUSH', '0')

    @staticmethod
    def _promise_and_reference() -> tuple[bytes, bytes]:
        """A promise block, and a later block that only decodes if the
        promise's insertions reached the connection-wide table."""
        encoder = Encoder()
        promise = encoder.encode([(':method', 'GET'), ('x-pushed', 'yes')])
        later = encoder.encode([('x-pushed', 'yes')])
        return promise, later

    async def test_the_zero_is_advertised(self):
        assert getattr(_sent_settings(await _connected()), 'enable_push', None) == 0

    async def test_a_promise_before_the_ack_is_accepted(self):
        """The window the ACK condition exists to protect: refusing here
        would reject conforming traffic."""
        block = Encoder().encode([(':method', 'GET'), (':path', '/pushed')])
        c = await _connected(_push_promise(block))

        assert not _written(c, FrameTypes.GOAWAY), (
            'a promise was refused in the round-trip before our SETTINGS '
            'was acknowledged')
        assert c._failure is None

    async def test_a_promise_after_the_ack_is_a_connection_error(self):
        block = Encoder().encode([(':method', 'GET'), (':path', '/pushed')])
        c = await _connected(_settings_ack() + _push_promise(block))

        goaway = _written(c, FrameTypes.GOAWAY)
        assert goaway, 'the promise was accepted after ENABLE_PUSH=0 was acked'
        assert goaway[0].error_code == ErrorCodes.PROTOCOL_ERROR

    async def test_the_refusal_is_not_a_silent_close(self):
        block = Encoder().encode([(':method', 'GET'), (':path', '/pushed')])
        c = await _connected(_settings_ack() + _push_promise(block))

        assert c._connection_lost
        assert c._failure is not None and 'PUSH_PROMISE' in c._failure

    async def test_the_refused_block_is_still_decoded(self):
        """BLA-267's regression guard.  The HPACK table is connection-wide,
        so a block refused *instead of* decoded leaves every later block on
        the connection reading a table missing its insertions — silent
        corruption, not an error.  Refuse after the decode, never in place
        of it."""
        promise, later = self._promise_and_reference()
        c = await _connected(_settings_ack() + _push_promise(promise))

        assert _written(c, FrameTypes.GOAWAY), 'the promise was not refused'
        assert c._factory.decoder.decode(later, raw=True) == [
            (b'x-pushed', b'yes')], (
            'the promise was refused without decoding it — the connection-'
            'wide table never saw its insertions')

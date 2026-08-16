"""WebSocket message bounds — the inflate ceiling and the fragment total.

``BB_WS_MAX_FRAME_PAYLOAD`` bounds one frame *as it arrives on the wire*.
It cannot bound what that frame becomes, and two paths grow a message
past it:

* **permessage-deflate** inflates with no output bound.  Deflate ratios
  measured in this tree reach 1028.8:1, so a frame well under the frame
  cap still costs gigabytes of server memory.
* **fragmentation** appends CONTINUATION frames with no total.  Each
  frame is legal; the sum is unbounded.

``BB_WS_MAX_MESSAGE_SIZE`` bounds the message the *application* receives
— post-reassembly, post-inflation — and answers 1009 (MESSAGE_TOO_BIG,
RFC 6455 §7.4.1).

Every test here sets the cap explicitly.  None of them assert the shipped
default, so changing that default cannot silently rewrite what these
tests mean.
"""
import asyncio
import tracemalloc
import zlib

import pytest

from blackbull.server.constants import WSCloseCode
from blackbull.server.permessage_deflate import InboundDecompressor
from blackbull.server.recipient import (AsyncioReader, ProtocolError,
                                        WebSocketRecipient)
from blackbull.server.sender import AsyncioWriter
from blackbull.server.ws_codec import MessageTooLarge, WSOpcode


# ---------------------------------------------------------------------------
# Wire helpers — client frames, so masked (RFC 6455 §5.1).
# ---------------------------------------------------------------------------

_MASK = b'\xde\xad\xbe\xef'


def _frame(payload: bytes, *, opcode: int = WSOpcode.TEXT, fin: bool = True,
           rsv1: bool = False) -> bytes:
    """One masked client frame with explicit FIN / RSV1 control."""
    first = (0x80 if fin else 0x00) | (0x40 if rsv1 else 0x00) | opcode
    length = len(payload)
    if length < 126:
        header = bytes([first, 0x80 | length])
    elif length < 65536:
        header = bytes([first, 0x80 | 126]) + length.to_bytes(2, 'big')
    else:
        header = bytes([first, 0x80 | 127]) + length.to_bytes(8, 'big')
    masked = bytes(b ^ _MASK[i % 4] for i, b in enumerate(payload))
    return header + _MASK + masked


def _deflate(payload: bytes) -> bytes:
    """Compress one message the way a permessage-deflate peer would.

    Raw DEFLATE, trailing ``\\x00\\x00\\xff\\xff`` stripped per RFC 7692
    §7.2.1 — the mirror of what ``OutboundCompressor`` emits.
    """
    c = zlib.compressobj(wbits=-15, level=zlib.Z_DEFAULT_COMPRESSION)
    out = c.compress(payload) + c.flush(zlib.Z_SYNC_FLUSH)
    return out[:-4] if out.endswith(b'\x00\x00\xff\xff') else out


class _FakeReader:
    """Byte buffer with the StreamReader surface the recipient uses.

    ``remaining`` is the point of it: a refusal that happens at the frame
    which crosses the cap leaves the following frames unread, and that is
    observable here without reaching into the recipient.
    """

    def __init__(self, data: bytes):
        self._buf = bytearray(data)

    @property
    def remaining(self) -> int:
        return len(self._buf)

    async def readexactly(self, n: int) -> bytes:
        if len(self._buf) < n:
            raise asyncio.IncompleteReadError(bytes(self._buf), n)
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    async def read(self, n: int) -> bytes:
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    async def readuntil(self, sep: bytes) -> bytes:
        idx = self._buf.find(sep)
        if idx == -1:
            raise asyncio.IncompleteReadError(bytes(self._buf), None)
        chunk = bytes(self._buf[:idx + len(sep)])
        del self._buf[:idx + len(sep)]
        return chunk


class _FakeWriter:
    def __init__(self):
        self.written = bytearray()

    def write(self, data: bytes):
        self.written += data

    def writelines(self, parts):
        for part in parts:
            self.written += part

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass


def _driver(raw: bytes, *, max_message_size: int,
            compressed: bool = False,
            max_frame_payload: int = 64 * 1024 * 1024):
    """A recipient over *raw*, plus the reader and writer to assert on."""
    reader = _FakeReader(raw)
    writer = _FakeWriter()
    recipient = WebSocketRecipient(
        AsyncioReader(reader),
        AsyncioWriter(writer),
        max_frame_payload=max_frame_payload,
        max_message_size=max_message_size,
        decompressor=(InboundDecompressor(15, reset_per_message=False)
                      if compressed else None),
        ws_queue_depth=0,
    )
    return recipient, reader, writer


def _closed_with(writer: _FakeWriter, code: WSCloseCode) -> bool:
    return code.to_bytes(2, 'big') in bytes(writer.written)


# ---------------------------------------------------------------------------
# The inflate ceiling (audit G1)
# ---------------------------------------------------------------------------

class TestInflateBound:
    """A compressed frame may not inflate past the message cap."""

    @pytest.mark.asyncio
    async def test_bomb_is_refused_with_1009(self):
        bomb = _deflate(b'\x00' * (32 * 1024 * 1024))
        # The premise of the whole defence: the frame that carries this is
        # tiny, so no frame-level cap can see the attack coming.
        assert len(bomb) < 64 * 1024, (
            f'compressed bomb is {len(bomb)} bytes — the test no longer '
            f'demonstrates amplification'
        )
        recipient, _, writer = _driver(
            _frame(bomb, opcode=WSOpcode.BINARY, rsv1=True),
            max_message_size=1024 * 1024, compressed=True)

        assert await recipient() == {'type': 'websocket.connect'}
        with pytest.raises(ProtocolError) as exc:
            await recipient()
        assert exc.value.close_code == WSCloseCode.MESSAGE_TOO_BIG
        assert _closed_with(writer, WSCloseCode.MESSAGE_TOO_BIG)

    @pytest.mark.asyncio
    async def test_bomb_never_materialises(self):
        """The refusal must cost the cap, not the bomb.

        Closing with 1009 *after* inflating 32 MiB into memory would pass
        every behavioural assertion above and defend nothing — the whole
        point is that the allocator never sees the inflated payload.
        """
        inflated = 32 * 1024 * 1024
        cap = 256 * 1024
        bomb = _deflate(b'\x00' * inflated)
        recipient, _, _ = _driver(
            _frame(bomb, opcode=WSOpcode.BINARY, rsv1=True),
            max_message_size=cap, compressed=True)
        assert await recipient() == {'type': 'websocket.connect'}

        tracemalloc.start()
        try:
            with pytest.raises(ProtocolError):
                await recipient()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        # Generous: zlib's own window and the cap-sized output buffer both
        # count.  The assertion that matters is the order of magnitude —
        # anything near ``inflated`` means the bomb was materialised.
        budget = cap * 8
        assert peak < budget, (
            f'peak allocation {peak:,} B during the refusal; expected under '
            f'{budget:,} B (cap {cap:,}).  The {inflated:,} B payload was '
            f'materialised before the check.'
        )

    @pytest.mark.asyncio
    async def test_message_exactly_at_the_cap_is_delivered(self):
        """The off-by-one is the likely regression, so it gets its own test."""
        cap = 64 * 1024
        payload = b'x' * cap
        recipient, _, _ = _driver(
            _frame(_deflate(payload), opcode=WSOpcode.BINARY, rsv1=True),
            max_message_size=cap, compressed=True)

        assert await recipient() == {'type': 'websocket.connect'}
        event = await recipient()
        assert event['type'] == 'websocket.receive'
        assert event['bytes'] == payload

    @pytest.mark.asyncio
    async def test_one_byte_over_the_cap_is_refused(self):
        cap = 64 * 1024
        recipient, _, _ = _driver(
            _frame(_deflate(b'x' * (cap + 1)), opcode=WSOpcode.BINARY,
                   rsv1=True),
            max_message_size=cap, compressed=True)

        assert await recipient() == {'type': 'websocket.connect'}
        with pytest.raises(ProtocolError) as exc:
            await recipient()
        assert exc.value.close_code == WSCloseCode.MESSAGE_TOO_BIG


# ---------------------------------------------------------------------------
# The fragment total (audit G2)
# ---------------------------------------------------------------------------

class TestFragmentTotal:
    """N legal frames may not sum past the message cap."""

    @pytest.mark.asyncio
    async def test_total_over_cap_is_refused_at_the_crossing_frame(self):
        cap = 4096
        chunk = b'y' * 1024
        # Four chunks reach the cap exactly; the fifth crosses it.  Two more
        # follow so that "stopped at the crossing frame" is distinguishable
        # from "stopped at the end of the buffer".
        raw = _frame(chunk, opcode=WSOpcode.BINARY, fin=False)
        raw += b''.join(
            _frame(chunk, opcode=WSOpcode.CONTINUATION, fin=False)
            for _ in range(6))
        recipient, reader, writer = _driver(raw, max_message_size=cap)

        assert await recipient() == {'type': 'websocket.connect'}
        with pytest.raises(ProtocolError) as exc:
            await recipient()

        assert exc.value.close_code == WSCloseCode.MESSAGE_TOO_BIG
        assert _closed_with(writer, WSCloseCode.MESSAGE_TOO_BIG)
        assert reader.remaining > 0, (
            'every frame was consumed — the total was checked only after the '
            'whole message had been accumulated, which is the defect'
        )

    @pytest.mark.asyncio
    async def test_total_exactly_at_cap_is_delivered(self):
        cap = 4096
        chunk = b'y' * 1024
        raw = _frame(chunk, opcode=WSOpcode.BINARY, fin=False)
        raw += b''.join(
            _frame(chunk, opcode=WSOpcode.CONTINUATION, fin=(i == 2))
            for i in range(3))
        recipient, _, _ = _driver(raw, max_message_size=cap)

        assert await recipient() == {'type': 'websocket.connect'}
        event = await recipient()
        assert event['type'] == 'websocket.receive'
        assert event['bytes'] == chunk * 4

    @pytest.mark.asyncio
    async def test_unfragmented_frame_over_the_cap_is_refused(self):
        """The message cap binds even when no fragment and no deflate is involved.

        The frame cap is deliberately left far above the message cap here:
        a single frame under ``BB_WS_MAX_FRAME_PAYLOAD`` but over
        ``BB_WS_MAX_MESSAGE_SIZE`` is exactly the case where only the new
        bound can refuse.
        """
        cap = 4096
        recipient, _, writer = _driver(
            _frame(b'z' * (cap + 1), opcode=WSOpcode.BINARY),
            max_message_size=cap, max_frame_payload=1024 * 1024)

        assert await recipient() == {'type': 'websocket.connect'}
        with pytest.raises(ProtocolError) as exc:
            await recipient()
        assert exc.value.close_code == WSCloseCode.MESSAGE_TOO_BIG
        assert _closed_with(writer, WSCloseCode.MESSAGE_TOO_BIG)


# ---------------------------------------------------------------------------
# Observability and the disable switch
# ---------------------------------------------------------------------------

class TestCapPolicy:
    @pytest.mark.asyncio
    async def test_zero_disables_the_cap(self):
        """``0`` means unbounded, consistent with every other cap knob."""
        payload = b'w' * 200_000
        recipient, _, _ = _driver(
            _frame(payload, opcode=WSOpcode.BINARY), max_message_size=0)

        assert await recipient() == {'type': 'websocket.connect'}
        event = await recipient()
        assert event['bytes'] == payload

    @pytest.mark.asyncio
    async def test_refusal_is_visible_in_the_cap_log(self, caplog):
        """Every other limit reports on ``blackbull.caps``; so does this one."""
        cap = 4096
        recipient, _, _ = _driver(
            _frame(b'z' * (cap + 1), opcode=WSOpcode.BINARY),
            max_message_size=cap, max_frame_payload=1024 * 1024)
        assert await recipient() == {'type': 'websocket.connect'}

        with caplog.at_level('WARNING', logger='blackbull.caps'):
            with pytest.raises(ProtocolError):
                await recipient()

        hits = [r for r in caplog.records
                if getattr(r, 'cap', None) == 'ws_max_message_size']
        assert hits, (
            'no ws_max_message_size record on blackbull.caps; the limit is '
            f'invisible to operators.  saw={[getattr(r, "cap", None) for r in caplog.records]}'
        )
        assert hits[0].limit == cap
        assert hits[0].requested > cap
        assert hits[0].protocol == 'ws'


class TestShippedDefault:
    """The one place the default value itself is the claim under test.

    Every other test here sets the cap explicitly.  This one asserts the
    property the default was *chosen* for: that the Autobahn suite's
    largest message (16 MiB, cases 9.1.6 / 9.2.6) passes with nothing
    configured.  Autobahn itself runs in CI — Docker bind mounts do not
    work from a WSL2 checkout, so the suite cannot gate a local change —
    which is exactly why the boundary deserves an in-tree guard rather
    than a promise to remember.
    """

    @pytest.mark.asyncio
    async def test_autobahns_largest_message_fits_the_default(self):
        from blackbull.env import get_settings
        cap = get_settings().ws_max_message_size
        autobahn_largest = 16 * 1024 * 1024  # 9.1.6 text / 9.2.6 binary
        assert cap >= autobahn_largest, (
            f'BB_WS_MAX_MESSAGE_SIZE default is {cap:,}, under the '
            f'{autobahn_largest:,} B message Autobahn case 9.1.6 sends — the '
            f'suite now needs a non-default configuration to pass, which '
            f'weakens the conformance claim.  If that is intended, record the '
            f'deviation in docs/about/conformance.md and update this test.'
        )

    @pytest.mark.asyncio
    async def test_a_16_mib_message_is_delivered_unconfigured(self):
        """End-to-end at the boundary, through the recipient's real path."""
        payload = b'\x00' * (16 * 1024 * 1024)
        reader = _FakeReader(_frame(payload, opcode=WSOpcode.BINARY))
        writer = _FakeWriter()
        recipient = WebSocketRecipient(
            AsyncioReader(reader), AsyncioWriter(writer), ws_queue_depth=0)

        assert await recipient() == {'type': 'websocket.connect'}
        event = await recipient()
        assert event['type'] == 'websocket.receive'
        assert len(event['bytes']) == len(payload)


# ---------------------------------------------------------------------------
# The decompressor's own contract, independent of the recipient
# ---------------------------------------------------------------------------

class TestInboundDecompressorBound:
    def test_max_length_raises_before_returning_oversized_output(self):
        d = InboundDecompressor(15, reset_per_message=False)
        with pytest.raises(MessageTooLarge) as exc:
            d.decompress(_deflate(b'\x00' * (1024 * 1024)), max_length=1024)
        assert exc.value.maximum == 1024

    def test_no_max_length_is_unbounded(self):
        """``None`` keeps the pre-existing contract for callers with no cap."""
        d = InboundDecompressor(15, reset_per_message=False)
        payload = b'\x00' * 100_000
        assert d.decompress(_deflate(payload)) == payload

    def test_output_exactly_at_max_length_is_returned(self):
        d = InboundDecompressor(15, reset_per_message=False)
        payload = b'q' * 4096
        assert d.decompress(_deflate(payload), max_length=4096) == payload

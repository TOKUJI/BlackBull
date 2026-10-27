"""WebSocket connection retention — the last ✗ the attack-surface grid held.

HTTP/1.1 closes an idle keep-alive connection and HTTP/2 probes a silent
peer with a PING.  WebSocket did neither: ``WsIdleWatchdog`` services
buffered control frames and never closes, so a peer that completes the
handshake and then says nothing held its connection, its actor task and
its buffers for the life of the process.

The purpose is the one HTTP/2's probe already serves, on the same triad
column and the same audit axis (connection · slots · retention): an idle
WebSocket is *normal* — a subscription channel pushes nothing for hours —
so reaping on idleness alone would break the legitimate case.  Probing
distinguishes *idle* from *gone*.  RFC 6455 §5.5.2 provides PING for
exactly this and §5.5.3 makes the PONG answer mandatory, which is a
stronger guarantee than HTTP/2's "any frame counts".

Both knobs default to HTTP/2's numbers because the purpose is the same;
no test here asserts a default, so changing one cannot silently rewrite
what these tests mean.

Timing is not what is under test.  The decision is driven directly
through ``_on_idle_tick`` — which is exactly what the shared scanner
calls — so the assertions are deterministic.  One test at the end lets
the real scanner drive it, because a decision nothing ever invokes is
not a bound.
"""
import asyncio

import pytest

from blackbull.server.constants import WSCloseCode
from blackbull.server.recipient import WebSocketRecipient
from blackbull.server.sender import AsyncioWriter
from blackbull.server.ws_codec import WSOpcode

from tests.harness import fed, feed

pytestmark = pytest.mark.asyncio

_MASK = b'\xde\xad\xbe\xef'


def _frame(payload: bytes = b'', *, opcode: int = WSOpcode.TEXT) -> bytes:
    """One masked client frame (RFC 6455 §5.1 — client frames are masked)."""
    first = 0x80 | opcode
    header = bytes([first, 0x80 | len(payload)])
    masked = bytes(b ^ _MASK[i % 4] for i, b in enumerate(payload))
    return header + _MASK + masked


class _FakeWriter:
    def __init__(self):
        self.written = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written += data

    def writelines(self, parts) -> None:
        for p in parts:
            self.written += p

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


def _opcodes_written(writer: _FakeWriter) -> list[int]:
    """Server frames are unmasked, so the opcodes read straight off."""
    out, buf, i = [], bytes(writer.written), 0
    while i + 2 <= len(buf):
        opcode = buf[i] & 0x0F
        length = buf[i + 1] & 0x7F
        masked = bool(buf[i + 1] & 0x80)
        i += 2
        if length == 126:
            length = int.from_bytes(buf[i:i + 2], 'big'); i += 2
        elif length == 127:
            length = int.from_bytes(buf[i:i + 8], 'big'); i += 8
        if masked:
            i += 4
        out.append(opcode)
        i += length
    return out


async def _connected(reader, writer: _FakeWriter, **kw):
    """A recipient past the handshake, with its watchdog armed."""
    recipient = WebSocketRecipient(
        reader.reader, AsyncioWriter(writer),
        ws_queue_depth=0, **kw)
    assert await recipient() == {'type': 'websocket.connect'}
    return recipient


@pytest.mark.parametrize('require_masked', [False, True])
@pytest.mark.parametrize('opcode,payload', [(WSOpcode.PING, b''),
                                         (WSOpcode.CLOSE, b'\x03\xe8')])
async def test_buffered_controls_service_exact_frame_for_either_role(require_masked,
                                                                   opcode, payload):
    frame = (_frame(payload, opcode=opcode) if require_masked else
             bytes([0x80 | opcode, len(payload)]) + payload)
    writer = _FakeWriter()
    receiver = await _connected(fed(frame, eof=False), writer,
                                require_masked=require_masked)
    try:
        assert await receiver.service_available_control_frames()
        assert writer.written[0] & 15 == (WSOpcode.PONG if opcode == WSOpcode.PING else WSOpcode.CLOSE)
        assert bool(writer.written[1] & 128) is not require_masked
    finally:
        await receiver.shutdown()


@pytest.mark.parametrize('require_masked', [False, True])
@pytest.mark.parametrize('valid_mask', [False, True])
async def test_buffered_partial_control_rejects_only_a_wrong_role_mask(require_masked,
                                                                     valid_mask):
    masked = require_masked if valid_mask else not require_masked
    writer = _FakeWriter()
    receiver = await _connected(fed(bytes([0x89, 0x81 if masked else 1]), eof=False),
                                writer, require_masked=require_masked)
    try:
        assert await receiver.service_available_control_frames() is not valid_mask
        if valid_mask:
            assert not writer.written
        else:
            assert writer.written[0] & 15 == WSOpcode.CLOSE
            assert bool(writer.written[1] & 128) is not require_masked
    finally:
        await receiver.shutdown()


@pytest.mark.parametrize('require_masked', [False, True])
async def test_control_service_leaves_data_for_the_application(require_masked):
    frame = _frame(b'x') if require_masked else b'\x81\x01x'
    writer = _FakeWriter()
    receiver = await _connected(fed(frame, eof=False), writer,
                                require_masked=require_masked)
    try:
        assert not await receiver.service_available_control_frames()
        assert not writer.written
        assert await asyncio.wait_for(receiver.next_message(), 1) == 'x'
    finally:
        await receiver.shutdown()


def _go_silent_for(recipient, seconds: float) -> None:
    """Pretend the last inbound frame arrived *seconds* ago."""
    recipient._last_inbound_at = asyncio.get_running_loop().time() - seconds


# ===========================================================================
# A silent peer is asked, then closed
# ===========================================================================

class TestSilentPeerIsProbed:
    async def test_silence_past_the_idle_bound_sends_a_ping(self):
        reader, writer = fed(eof=False), _FakeWriter()
        r = await _connected(reader, writer, ws_idle_timeout=10.0,
                             ws_pong_timeout=5.0)

        _go_silent_for(r, 11.0)
        r._on_idle_tick()
        await asyncio.sleep(0)   # the tick runs in a timer context; it can
                                 # only schedule the write, not await it

        assert WSOpcode.PING in _opcodes_written(writer), (
            'a peer silent past the idle bound was never asked whether it '
            'is still there')

    async def test_an_unanswered_probe_closes_the_connection(self):
        reader, writer = fed(eof=False), _FakeWriter()
        r = await _connected(reader, writer, ws_idle_timeout=10.0,
                             ws_pong_timeout=5.0)

        _go_silent_for(r, 11.0)
        r._on_idle_tick()                      # probe goes out
        r._probe_sent_at -= 6.0                # ... and is never answered
        r._on_idle_tick()
        await asyncio.sleep(0)

        assert WSOpcode.CLOSE in _opcodes_written(writer)
        assert WSCloseCode.GOING_AWAY.to_bytes(2, 'big') in bytes(writer.written)

    async def test_only_one_probe_is_sent_while_one_is_outstanding(self):
        """Every tick past the bound must not become a PING flood of our own."""
        reader, writer = fed(eof=False), _FakeWriter()
        r = await _connected(reader, writer, ws_idle_timeout=10.0,
                             ws_pong_timeout=5.0)

        _go_silent_for(r, 11.0)
        for _ in range(5):
            r._on_idle_tick()
        await asyncio.sleep(0)

        assert _opcodes_written(writer).count(WSOpcode.PING) == 1


# ===========================================================================
# The false-positive tests — the ones that matter
# ===========================================================================

class TestAnsweringPeerIsNeverClosed:
    async def test_a_pong_clears_the_probe(self):
        reader, writer = fed(eof=False), _FakeWriter()
        r = await _connected(reader, writer, ws_idle_timeout=10.0,
                             ws_pong_timeout=5.0)

        _go_silent_for(r, 11.0)
        r._on_idle_tick()
        await asyncio.sleep(0)
        assert r._probe_sent_at is not None

        feed(reader, _frame(opcode=WSOpcode.PONG))
        await r._drive_once()

        assert r._probe_sent_at is None, 'the peer answered and is still suspect'
        r._probe_sent_at = None
        r._on_idle_tick()
        assert WSOpcode.CLOSE not in _opcodes_written(writer)

    async def test_any_inbound_frame_counts_as_an_answer(self):
        """Matches HTTP/2: a peer that sent anything is demonstrably alive.

        Requiring a PONG specifically would close a connection whose peer
        is busy talking to us.
        """
        reader, writer = fed(eof=False), _FakeWriter()
        r = await _connected(reader, writer, ws_idle_timeout=10.0,
                             ws_pong_timeout=5.0)

        _go_silent_for(r, 11.0)
        r._on_idle_tick()
        await asyncio.sleep(0)
        feed(reader, _frame(b'hi'))
        await r._drive_once()

        assert r._probe_sent_at is None
        assert WSOpcode.CLOSE not in _opcodes_written(writer)

    async def test_a_busy_connection_is_never_probed(self):
        reader, writer = fed(eof=False), _FakeWriter()
        r = await _connected(reader, writer, ws_idle_timeout=10.0,
                             ws_pong_timeout=5.0)

        for i in range(5):
            feed(reader, _frame(bytes([i])))
            await r._drive_once()
            r._on_idle_tick()

        assert WSOpcode.PING not in _opcodes_written(writer)


# ===========================================================================
# The knob, and what it costs when off
# ===========================================================================

class TestTheBoundIsConfigurable:
    async def test_zero_disables_the_probe(self):
        reader, writer = fed(eof=False), _FakeWriter()
        r = await _connected(reader, writer, ws_idle_timeout=0.0,
                             ws_pong_timeout=5.0)

        _go_silent_for(r, 3600.0)
        r._on_idle_tick()
        await asyncio.sleep(0)

        assert _opcodes_written(writer) == []

    async def test_the_receive_path_records_arrival_without_a_clock_read(self):
        """The hot path pays one integer increment, not a ``loop.time()``.

        Sprint 104 spent the sprint removing per-message clock reads from
        this codebase; a liveness bound that puts one back would cost more
        than it defends.  The tick callback — once per idle connection per
        scanner tick — is where the clock is read.
        """
        reader, writer = fed(eof=False), _FakeWriter()
        r = await _connected(reader, writer, ws_idle_timeout=10.0,
                             ws_pong_timeout=5.0)

        before = r._inbound_seq
        for i in range(3):
            feed(reader, _frame(bytes([i])))
            await r._drive_once()

        assert r._inbound_seq == before + 3

        loop = asyncio.get_running_loop()
        real_time, calls = loop.time, 0

        def _counting_time():
            nonlocal calls
            calls += 1
            return real_time()

        loop.time = _counting_time
        try:
            feed(reader, _frame(b'x'))
            await r._drive_once()
        finally:
            loop.time = real_time

        assert calls == 0, (
            f'the receive path read the clock {calls} time(s) per frame')


# ===========================================================================
# Whose probe it is
# ===========================================================================

class TestOnlyTheServerProbes:
    """This class is the read side of both roles, and only one asks.

    ``WebSocketRecipient`` is constructed by the server binding *and* by
    both bundled clients.  The probe answers a question only the server
    has — how long an untrusted peer may hold a connection — so reading
    the setting inside the recipient would silently make a client probe
    the server it chose to connect to: traffic nobody asked for, on the
    surface the attack-surface audit records as unaudited.
    """

    async def test_a_bare_recipient_never_probes(self):
        reader, writer = fed(eof=False), _FakeWriter()
        r = await _connected(reader, writer)          # no timeouts passed

        assert r._ws_idle_timeout == 0.0
        _go_silent_for(r, 86_400.0)
        r._on_idle_tick()
        await asyncio.sleep(0)

        assert _opcodes_written(writer) == []

    async def test_the_server_factory_arms_it_from_settings(self):
        from blackbull.env import get_settings
        from blackbull.server.recipient import RecipientFactory

        reader, writer = fed(eof=False), _FakeWriter()
        r = RecipientFactory.websocket(reader.reader,
                                       AsyncioWriter(writer),
                                       ws_queue_depth=0)
        cfg = get_settings()
        assert r._ws_idle_timeout == cfg.ws_idle_timeout
        assert r._ws_pong_timeout == cfg.ws_pong_timeout

    async def test_the_bundled_clients_do_not_inherit_it(self):
        """Constructed directly, so they must come out with the probe off."""
        import inspect

        from blackbull.client import websocket as ws_client
        from blackbull.client import websocket_h2 as ws_h2_client

        for mod in (ws_client, ws_h2_client):
            src = inspect.getsource(mod)
            assert 'ws_idle_timeout' not in src, (
                f'{mod.__name__} passes a liveness timeout — the client '
                f'surface is out of scope for this bound')


# ===========================================================================
# The scanner really invokes it
# ===========================================================================

class TestTheScannerDrivesIt:
    async def test_a_silent_peer_is_closed_without_anyone_calling_the_tick(self):
        reader, writer = fed(eof=False), _FakeWriter()
        r = await _connected(reader, writer, ws_idle_timeout=0.05,
                             ws_pong_timeout=0.05)
        # Drive the reader so the connection is genuinely live and parked.
        task = asyncio.ensure_future(r._drive_once())
        try:
            for _ in range(60):
                await asyncio.sleep(0.05)
                if writer.closed or WSOpcode.CLOSE in _opcodes_written(writer):
                    break
            ops = _opcodes_written(writer)
            assert WSOpcode.PING in ops, (
                'the scanner never invoked the liveness decision — the bound '
                'exists but nothing applies it')
            assert WSOpcode.CLOSE in ops
        finally:
            task.cancel()
            r.disarm_watchdog()


# ===========================================================================
# Both transports, one construction point
# ===========================================================================

class TestBothTransportsGetIt:
    """RFC 8441 WebSocket-over-HTTP/2 must not be the unbounded one.

    The RFC 8441 surface is the one `KNOWN_LIMITATIONS.md` names as
    reachable — an attacker opening Extended CONNECT streams and holding
    them idle — so "the probe covers WebSocket" is only true if it covers
    that path too.  It does, structurally: both protocol actors build a
    ``WebSocketActor``, which is the single caller of the factory that
    reads the setting.  This test is what keeps that structural claim
    from quietly becoming false.
    """

    @pytest.mark.asyncio(loop_scope='function')
    async def test_websocket_actor_is_the_only_thing_that_builds_the_recipient(self):
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parents[2] / 'blackbull'
        callers = {
            path.relative_to(root).as_posix()
            for path in root.rglob('*.py')
            if re.search(r'RecipientFactory\.websocket\(', path.read_text())
        }
        assert callers == {'server/websocket_actor.py'}, (
            f'the server factory has more callers than the WebSocket actor: '
            f'{sorted(callers)} — each one is a place the probe can be '
            f'forgotten')

    async def test_both_protocol_actors_build_that_actor(self):
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parents[2] / 'blackbull'
        builders = {
            path.relative_to(root).as_posix()
            for path in root.rglob('*.py')
            if re.search(r'^\s*ws_actor = WebSocketActor\(', path.read_text(),
                         re.M)
        }
        assert builders == {'server/http1_actor.py', 'server/http2_actor.py'}, (
            f'expected both protocol actors to route WebSocket through the '
            f'same actor; found {sorted(builders)}')

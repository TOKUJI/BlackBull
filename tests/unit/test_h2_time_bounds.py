"""HTTP/2's missing time axis.

Every other protocol here bounds how long a peer may take.  `HTTP2Actor`
bounded nothing: a peer could open a header block and dribble
CONTINUATION forever, complete the preface and go silent, or request a
large response and never open its flow-control window — each holding a
connection, its actor task and its buffers for as long as it liked, at no
cost to itself.

The three bounds are enforced from a watchdog rather than by wrapping the
frame read, because that read is the hot path and because interrupting it
mid-frame would desynchronise a connection the server intends to keep.

The idle bound is deliberately a *liveness* probe, not an idle reaper.
HTTP/2 connections are meant to be long-lived and idle — a browser holds
one across a page's lifetime, a gRPC channel idles between calls — so the
test that matters most here is the false-positive one: a peer that
answers must never be closed.
"""
from __future__ import annotations

import asyncio

import pytest
from hpack import Encoder

from blackbull.protocol.frame_types import (
    ErrorCodes, FrameTypes, HeaderFrameFlags, PingFrameFlags,
)
from blackbull.server.http2_actor import HTTP2Actor
from blackbull.server.sender import AsyncioWriter

pytestmark = pytest.mark.asyncio

# Short enough to keep the suite fast, long enough that a loaded CI box
# does not mistake scheduler jitter for a peer going silent.
_IDLE = 0.05
_PING_WAIT = 0.05
_HEADER = 0.05


@pytest.fixture(autouse=True)
def _short_h2_timeouts(monkeypatch):
    monkeypatch.setenv('BB_H2_IDLE_TIMEOUT', str(_IDLE))
    monkeypatch.setenv('BB_H2_PING_TIMEOUT', str(_PING_WAIT))
    monkeypatch.setenv('BB_HEADER_TIMEOUT', str(_HEADER))
    from blackbull.env import reset_settings_cache
    reset_settings_cache()
    yield
    reset_settings_cache()


def _frame(type_byte: FrameTypes, flags: int = 0, stream_id: int = 0,
           payload: bytes = b'') -> bytes:
    return (len(payload).to_bytes(3, 'big') + type_byte + bytes([flags])
            + stream_id.to_bytes(4, 'big') + payload)


def _settings() -> bytes:
    return _frame(FrameTypes.SETTINGS, 0, 0, b'')


def _partial_headers(stream_id: int = 1) -> bytes:
    """HEADERS without END_HEADERS — the peer promises CONTINUATION."""
    fields = [(b':method', b'GET'), (b':path', b'/'),
              (b':scheme', b'https'), (b':authority', b'example.com')]
    return _frame(FrameTypes.HEADERS, 0, stream_id, Encoder().encode(fields))


class _Wire:
    """A reader the test drives frame by frame, and the writer it closes.

    Models the one interaction the watchdog depends on: closing the writer
    makes the pending read return EOF, which is how a bound that fires
    while the frame loop is parked ends the connection without touching
    the read path.
    """

    def __init__(self, frames: list[bytes]):
        self._queue: asyncio.Queue = asyncio.Queue()
        for f in frames:
            self._queue.put_nowait(f)
        self.closed = False
        self.written = bytearray()

    # -- reader side --
    async def receive(self) -> bytes:
        if self.closed:
            return b''
        get = asyncio.ensure_future(self._queue.get())
        try:
            return await get
        except asyncio.CancelledError:
            get.cancel()
            raise

    def deliver(self, frame: bytes) -> None:
        self._queue.put_nowait(frame)

    # -- writer side --
    def write(self, data: bytes) -> None:
        self.written += data

    def writelines(self, parts) -> None:
        for p in parts:
            self.written += p

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True
        self._queue.put_nowait(b'')     # unpark the frame loop with EOF

    async def wait_closed(self) -> None:
        pass


def _run(frames: list[bytes]):
    """Start an actor over *wire*; returns (actor, wire, task)."""
    wire = _Wire(frames)

    async def _app(*a, **kw):  # pragma: no cover - never reached here
        pass

    actor = HTTP2Actor(None, AsyncioWriter(wire), _app, aggregator=None)
    actor.receive = wire.receive
    sent: list = []
    real_send = actor.send_frame

    async def _recording_send(frame):
        sent.append(frame)
        return await real_send(frame)

    actor.send_frame = _recording_send
    task = asyncio.ensure_future(actor.run())
    return actor, wire, sent, task


def _types(sent) -> list:
    return [f.FrameType() for f in sent if hasattr(f, 'FrameType')]


def _goaways(sent) -> list:
    return [f for f in sent
            if getattr(f, 'FrameType', None) and f.FrameType() == FrameTypes.GOAWAY]


def _pings(sent) -> list:
    return [f for f in sent
            if getattr(f, 'FrameType', None) and f.FrameType() == FrameTypes.PING
            and not (f.flags & PingFrameFlags.ACK)]


async def _settle(task, timeout=2.0):
    try:
        await asyncio.wait_for(task, timeout)
    except asyncio.TimeoutError:  # pragma: no cover - a hang is the failure
        task.cancel()
        raise AssertionError('the connection never ended; a bound did not fire')


# ===========================================================================
# The header block that never completes
# ===========================================================================

class TestHeaderBlockDeadline:
    async def test_a_dribbled_header_block_ends_the_connection(self):
        """An unfinished header block is a *connection* problem, not a stream one.

        HPACK state is connection-wide and order-dependent: a block whose
        bytes were never fed leaves the decoder unable to read any future
        block on that connection.  Abandoning the stream alone would keep
        a connection that can no longer decode anything — so this is
        GOAWAY, not RST_STREAM, however tempting the narrower answer looks.
        """
        actor, wire, sent, task = _run([_settings(), _partial_headers()])
        await _settle(task)

        goaways = _goaways(sent)
        assert goaways, f'no GOAWAY; frames sent were {_types(sent)}'
        assert goaways[0].error_code == ErrorCodes.ENHANCE_YOUR_CALM
        assert wire.closed

    async def test_the_cap_hit_is_logged(self, caplog):
        with caplog.at_level('WARNING', logger='blackbull.caps'):
            _, _, _, task = _run([_settings(), _partial_headers()])
            await _settle(task)

        hits = [r for r in caplog.records
                if getattr(r, 'cap', None) == 'header_timeout'
                and getattr(r, 'protocol', None) == 'http2']
        assert hits, 'the header deadline fired invisibly'

    async def test_a_header_block_that_completes_is_untouched(self):
        """The deadline must not fire on a peer that simply finished."""
        fields = [(b':method', b'GET'), (b':path', b'/'),
                  (b':scheme', b'https'), (b':authority', b'example.com')]
        complete = _frame(FrameTypes.HEADERS,
                          HeaderFrameFlags.END_HEADERS | HeaderFrameFlags.END_STREAM,
                          1, Encoder().encode(fields))
        actor, wire, sent, task = _run([_settings(), complete])
        await asyncio.sleep(_HEADER * 3)
        wire.close()
        await _settle(task)

        assert not any(g.error_code == ErrorCodes.ENHANCE_YOUR_CALM
                       for g in _goaways(sent)), (
            'a completed header block was reaped by the header deadline')


# ===========================================================================
# Idle: probe rather than reap
# ===========================================================================

class TestIdleLiveness:
    async def test_silence_earns_a_ping_not_a_close(self):
        actor, wire, sent, task = _run([_settings()])
        await asyncio.sleep(_IDLE * 2)
        try:
            assert _pings(sent), (
                f'no liveness PING after {_IDLE * 2}s of silence; '
                f'sent {_types(sent)}')
            assert not wire.closed, 'closed on idleness alone, without probing'
        finally:
            wire.close()
            await _settle(task)

    async def test_a_peer_that_answers_is_never_closed(self):
        """The false-positive case — the whole reason this is a probe.

        An idle gRPC channel or a browser holding a connection open across
        a page's lifetime must survive indefinitely.  Answering means any
        frame at all: a PING ACK proves liveness, and so does anything
        else the peer chooses to send.
        """
        actor, wire, sent, task = _run([_settings()])
        deadline = asyncio.get_running_loop().time() + _IDLE * 6
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(_IDLE / 4)
            wire.deliver(_frame(FrameTypes.PING, PingFrameFlags.ACK, 0,
                                b'\x00' * 8))

        try:
            assert not wire.closed, (
                'a peer answering every probe was closed anyway')
            assert not _goaways(sent), (
                f'GOAWAY sent to a responsive peer: '
                f'{[g.error_code for g in _goaways(sent)]}')
        finally:
            wire.close()
            await _settle(task)

    async def test_an_unanswered_probe_closes_the_connection(self):
        actor, wire, sent, task = _run([_settings()])
        await _settle(task, timeout=2.0)

        goaways = _goaways(sent)
        assert goaways, f'silent peer held forever; sent {_types(sent)}'
        assert goaways[0].error_code == ErrorCodes.NO_ERROR, (
            'the peer did nothing wrong — it stopped answering, which is '
            'NO_ERROR, not a protocol violation')
        assert wire.closed

    async def test_a_stalled_window_unparks_the_stream(self, monkeypatch):
        """CVE-2019-9511's shape: request a large response, never grant credit.

        Nothing bounded this wait, so the stream task parked forever —
        holding a ``max_concurrent_streams`` slot, its buffers, and the
        response body, for as long as the peer felt like saying nothing.
        """
        from blackbull.env import reset_settings_cache
        from blackbull.server.sender import (ConnectionWindow,
                                             FlowControlStalled, HTTP2Sender)
        monkeypatch.setenv('BB_WRITE_TIMEOUT', '0.05')
        reset_settings_cache()

        wire = _Wire([])
        sender = HTTP2Sender(AsyncioWriter(wire), None, 1,
                             conn_window=ConnectionWindow(0))
        sender.stream_window_size = 0

        with pytest.raises(FlowControlStalled):
            await asyncio.wait_for(
                sender._write_data(b'x' * 100, end_stream=True), timeout=2.0)

    async def test_credit_arriving_in_time_is_not_a_stall(self, monkeypatch):
        from blackbull.env import reset_settings_cache
        from blackbull.server.sender import ConnectionWindow, HTTP2Sender
        monkeypatch.setenv('BB_WRITE_TIMEOUT', '5.0')
        reset_settings_cache()

        wire = _Wire([])
        window = ConnectionWindow(0)
        sender = HTTP2Sender(AsyncioWriter(wire), None, 1, conn_window=window)
        sender.stream_window_size = 0

        async def _grant():
            await asyncio.sleep(0.01)
            window.size = 1000
            sender.window_update(1000)

        asyncio.ensure_future(_grant())
        await asyncio.wait_for(
            sender._write_data(b'x' * 100, end_stream=True), timeout=2.0)
        assert b'x' * 100 in bytes(wire.written)

    async def test_the_stall_is_visible_in_the_cap_log(self, monkeypatch, caplog):
        from blackbull.env import reset_settings_cache
        from blackbull.server.sender import (ConnectionWindow,
                                             FlowControlStalled, HTTP2Sender)
        monkeypatch.setenv('BB_WRITE_TIMEOUT', '0.05')
        reset_settings_cache()

        wire = _Wire([])
        sender = HTTP2Sender(AsyncioWriter(wire), None, 1,
                             conn_window=ConnectionWindow(0))
        sender.stream_window_size = 0

        with caplog.at_level('WARNING', logger='blackbull.caps'):
            with pytest.raises(FlowControlStalled):
                await sender._write_data(b'x' * 100, end_stream=True)

        hits = [r for r in caplog.records
                if getattr(r, 'cap', None) == 'write_timeout'
                and getattr(r, 'protocol', None) == 'http2']
        assert hits, 'a stream given up on with nothing in the cap log'

    async def test_zero_disables_probing(self, monkeypatch):
        from blackbull.env import reset_settings_cache
        monkeypatch.setenv('BB_H2_IDLE_TIMEOUT', '0')
        reset_settings_cache()

        actor, wire, sent, task = _run([_settings()])
        await asyncio.sleep(_IDLE * 4)
        try:
            assert not _pings(sent), 'probed with the probe disabled'
            assert not wire.closed
        finally:
            wire.close()
            await _settle(task)

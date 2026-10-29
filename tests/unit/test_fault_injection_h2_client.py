"""The HTTP/2 broken client — the grid's last cell.

`HTTP1Client` has `execute_scenario`; `HTTP2Client` did not, so an HTTP/2
client-side scenario could only be written as procedural code against a
raw socket.  That is what `test_fault_injection_h2.py` does with
`_synthetic_client_dial`, and it is the reason the cell reads "not
implemented" in the example.

**Written from its twin, not from the protocol.**  Sprint 107 drifted from
`scenario_h2` three times — an export collision, a `list` where the twin
used a `tuple`, result field names invented where the twin had them — and
every one was found by someone asking rather than by reading the code.  So
the vocabulary here mirrors `scenario_h1`'s client-side names
(`SendRawBytes` / `ReadResponse` / `Sleep` / `Abort`, `ScenarioResult`'s
fields) and departs only where HTTP/2 genuinely differs: it sends *frames*,
so there is a typed `SendFrame`, and the preface is a real step.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.client.http2 import HTTP2Client
from blackbull.fault_injection.scenario_h2_client import (
    ExpectServerFrame, ReadResponse, ScenarioH2Client, ScenarioH2ClientResult, SendFrame,
    SendHeaders, SendPreface, WaitForServerFrame,
)
from blackbull.protocol.frame_types import FrameTypes
from blackbull.server.recipient import AbstractReader
from blackbull.server.sender import AbstractWriter

pytestmark = pytest.mark.asyncio


class _RecordingWriter(AbstractWriter):
    def __init__(self) -> None:
        self.data = bytearray()
        self.aborted = False

    async def write(self, data: bytes) -> None:
        self.data += data

    @property
    def transport(self):
        outer = self

        class _T:
            def abort(self):
                outer.aborted = True
        return _T()


class _SilentReader(AbstractReader):
    """A peer that is connected and says nothing."""

    async def read(self, n: int) -> bytes:
        await asyncio.sleep(3600)
        return b''

    async def readexactly(self, n: int) -> bytes:
        await asyncio.sleep(3600)
        return b''


def _client(writer=None, reader=None) -> HTTP2Client:
    # scenario_mode: a scenario owns the wire from byte zero, so the client
    # sends no preface of its own for the scenario's to collide with.
    c = HTTP2Client('localhost', 1, scenario_mode=True)
    c._writer = writer or _RecordingWriter()
    c._reader = reader or _SilentReader()
    return c


# ===========================================================================
# The executor
# ===========================================================================

class TestExecuteScenario:
    async def test_the_preface_and_a_frame_reach_the_wire(self):
        writer = _RecordingWriter()
        c = _client(writer)
        scenario = ScenarioH2Client(steps=(
            SendPreface(),
            SendFrame(FrameTypes.PING, flags=0, stream_id=0, data=b'\x00' * 8),
        ))
        result = await c.execute_scenario(scenario)

        assert result.steps_completed == 2
        assert bytes(writer.data).startswith(b'PRI * HTTP/2.0\r\n')
        assert result.exception is None

    async def test_a_read_that_never_arrives_records_a_timeout(self):
        c = _client()
        result = await c.execute_scenario(ScenarioH2Client(steps=(
            ReadResponse(timeout=0.05),
        )))
        assert result.timed_out is True
        assert result.response is None
        assert result.steps_completed == 0, (
            'a step that timed out must not count as completed — the twin '
            'returns before incrementing')

    async def test_a_headers_step_reaches_the_wire(self):
        writer = _RecordingWriter()
        c = _client(writer)
        result = await c.execute_scenario(ScenarioH2Client(steps=(
            SendHeaders(raw_block=b'\x82', stream_id=1, end_stream=True),
        )))
        assert result.exception is None
        assert result.steps_completed == 1
        assert bytes(writer.data) == b'\x00\x00\x01\x01\x05\x00\x00\x00\x01\x82'

class TestAgainstARealServer:
    """The point of a broken client is what a real server does with it.

    BlackBull's own HTTP/2 server is the counterpart here — the same
    inversion the HTTP/1.1 client-side example makes with stdlib's server.
    """

    #: Only the cases that end on their own.  Three catalogue entries close
    #: with ``Sleep(30)`` because *holding the connection* is the fault they
    #: stage — what ends those is the server's own deadline, which is a
    #: different test at a different timescale (`test_h2_time_bounds.py`).
    @pytest.mark.parametrize('case_name', [
        'rapid_reset_burst', 'ping_flood', 'settings_flood',
        'unknown_frame_type', 'settings_ack_with_payload',
        'abort_mid_header_block',
    ])
    async def test_the_server_survives_the_case(self, case_name):
        from blackbull import BlackBull
        from blackbull.fault_injection.catalogue.h2_client import CATALOGUE
        from blackbull.testing import NativeTestServer

        app = BlackBull()

        @app.route(path='/')
        async def _root(conn):
            return 'ok'

        async with NativeTestServer(app) as server:
            client = HTTP2Client('127.0.0.1', server.port,
                                 scenario_mode=True)
            async with client:
                result = await asyncio.wait_for(
                    client.execute_scenario(CATALOGUE[case_name]()),
                    timeout=10.0)

        # The assertion is about the *server*: it must not have taken the
        # process down, and the executor must have folded whatever happened
        # into a result rather than raising.
        assert isinstance(result, ScenarioH2ClientResult)
        assert result.elapsed_s >= 0

    async def test_the_holding_cases_are_the_ones_left_out(self):
        """Pin the split, so a case that stops holding is noticed."""
        from blackbull.fault_injection.catalogue.h2_client import CATALOGUE
        from blackbull.fault_injection.scenario_h2_client import Sleep

        holding = {
            name for name, build in CATALOGUE.items()
            if any(isinstance(s, Sleep) and s.duration >= 10
                   for s in build().steps)
        }
        assert holding == {'preface_never_arrives',
                           'header_block_never_finished',
                           'data_frame_lies_about_length'}


# ===========================================================================
# A scenario owns the wire
# ===========================================================================

class TestScenarioOwnsTheWire:
    """HTTP/2 has a connect-time preamble; HTTP/1.1 does not.

    That single asymmetry is enough to make a whole grid cell measure
    nothing.  A client that has handshaked is not a blank wire, so a
    scenario's ``SendPreface`` lands *second* and corrupts the frame
    stream ahead of every later step.  A lenient peer skips the junk and
    the scenario reports success while the fault never arrived.

    These pin both halves: exactly one preface goes out in scenario mode,
    and the collision is refused rather than written.
    """

    async def test_scenario_mode_puts_exactly_one_preface_on_the_wire(self):
        writer = _RecordingWriter()
        c = HTTP2Client('localhost', 1, scenario_mode=True)
        c._writer = writer
        c._reader = _SilentReader()
        await c._start()          # the no-op that makes the mode work

        await c.execute_scenario(ScenarioH2Client(steps=(SendPreface(),)))

        assert bytes(writer.data).count(b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n') == 1

    async def test_a_preface_step_on_a_handshaked_client_is_refused(self):
        c = HTTP2Client('localhost', 1)     # not scenario_mode
        c._writer = _RecordingWriter()
        c._reader = _SilentReader()

        with pytest.raises(RuntimeError, match='already-prefaced'):
            await c.execute_scenario(
                ScenarioH2Client(steps=(SendPreface(),)))

    async def test_the_refusal_happens_before_any_byte_is_written(self):
        """Whole or nothing — a half-run scenario is worse than none."""
        writer = _RecordingWriter()
        c = HTTP2Client('localhost', 1)
        c._writer = writer
        c._reader = _SilentReader()

        with pytest.raises(RuntimeError):
            await c.execute_scenario(ScenarioH2Client(steps=(
                SendFrame(FrameTypes.PING, flags=0, stream_id=0,
                          data=b'\x00' * 8),
                SendPreface(),
            )))

        assert bytes(writer.data) == b'', (
            'the PING was written before the scenario was rejected')

    @pytest.mark.parametrize('read', [
        ReadResponse(timeout=0.1),
        WaitForServerFrame(match={'type': 'PING'}, timeout=0.1),
        ExpectServerFrame(match={'type': 'PING'}, timeout=0.1),
    ], ids=lambda step: type(step).__name__)
    async def test_a_read_step_is_refused_while_the_receive_loop_runs(self, read):
        """Both would read the same socket; which one wins is a coin toss."""
        c = HTTP2Client('localhost', 1)
        c._writer = writer = _RecordingWriter()
        c._reader = _SilentReader()
        c._receive_task = asyncio.create_task(asyncio.sleep(3600))
        try:
            with pytest.raises(RuntimeError, match='races the receive loop'):
                await c.execute_scenario(ScenarioH2Client(steps=(
                    SendFrame(FrameTypes.PING, data=b'\x00' * 8), read)))
        finally:
            c._receive_task.cancel()
        assert bytes(writer.data) == b''

    async def test_the_h1_twin_needs_no_such_mode(self):
        """The asymmetry is real and belongs to HTTP/2 alone.

        ``HTTP1Client._start`` is a no-op, so an H1 scenario already owns
        the wire.  If that ever stops being true, the H1 cell acquires the
        same silent failure and this test is where it surfaces.
        """
        from blackbull.client.http1 import HTTP1Client

        writer = _RecordingWriter()
        c = HTTP1Client('localhost', 1)
        c._writer = writer
        c._reader = _SilentReader()
        await c._start()

        assert bytes(writer.data) == b''

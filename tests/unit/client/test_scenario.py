"""Unit tests for the Scenario abstraction.

Covers:
  * ``Scenario.from_bytes`` totality (every byte string yields a valid
    scenario)
  * ``Scenario.well_formed`` convenience builder
  * ``HTTP1Client.execute_scenario`` dispatch — each step type invokes
    the expected primitive, ``Abort`` short-circuits, ``ReadResponse``
    timeout folds into ``ScenarioResult`` without raising.
"""
from __future__ import annotations

import os

import pytest

from blackbull.client import (
    Abort,
    ReadResponse,
    Scenario,
    SendRawBytes,
    Sleep,
)
from blackbull.client.http1 import HTTP1Client
from blackbull.server.recipient import AbstractReader
from blackbull.server.sender import AbstractWriter

from tests.harness import fed


# ---------------------------------------------------------------------------
# Test doubles (duplicated minimally from test_http1_client_primitives.py
# rather than imported, to keep test files independent.)
# ---------------------------------------------------------------------------


class _CapturingWriter(AbstractWriter):
    def __init__(self) -> None:
        self.data = bytearray()
        self.writes: list[bytes] = []

    async def write(self, data: bytes) -> None:
        self.data.extend(data)
        self.writes.append(bytes(data))


def _CannedReader(payload: bytes = b''):
    return fed(payload).reader


def _NeverReader(payload: bytes = b''):
    """Delivers *payload*, then stays connected and says nothing."""
    return fed(payload, eof=False).reader


class _FakeTransport:
    """Just enough for the Abort step's transport.abort() call."""

    def __init__(self) -> None:
        self.aborted = False

    def abort(self) -> None:
        self.aborted = True


class _FakeRawWriter:
    def __init__(self) -> None:
        self.transport = _FakeTransport()


def _client_with_fakes(*, reader: AbstractReader | None = None,
                       attach_raw_writer: bool = False
                       ) -> tuple[HTTP1Client, _CapturingWriter, _FakeRawWriter | None]:
    c = HTTP1Client('test-host', 8000)
    w = _CapturingWriter()
    c._writer = w  # type: ignore[assignment]
    if reader is not None:
        c._reader = reader  # type: ignore[assignment]
    raw: _FakeRawWriter | None = None
    if attach_raw_writer:
        raw = _FakeRawWriter()
        c._raw_writer = raw  # type: ignore[assignment]
    return c, w, raw


# ---------------------------------------------------------------------------
# Scenario data model + JSON round-trip
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Scenario.from_bytes — totality + opcode coverage
# ---------------------------------------------------------------------------


class TestScenarioFromBytes:
    def test_empty_bytes_yields_empty_scenario(self):
        assert Scenario.from_bytes(b'').steps == ()

    @pytest.mark.parametrize('seed', list(range(20)))
    def test_random_bytes_always_decode(self, seed: int):
        # Totality: every byte string must yield a valid Scenario without
        # raising.  Atheris coverage depends on this.
        rng = os.urandom(1 + seed * 7 % 64)
        scenario = Scenario.from_bytes(rng)
        assert isinstance(scenario, Scenario)
        assert isinstance(scenario.steps, tuple)

    def test_decoder_handles_short_send_payload(self):
        # SEND opcode followed by length=10 but only 3 bytes available —
        # should not raise; should consume what's there.
        raw = bytes([0]) + b'\x00\x0aabc'  # opcode 0, length 10, but only 3
        scenario = Scenario.from_bytes(raw)
        assert len(scenario.steps) == 1
        step = scenario.steps[0]
        assert isinstance(step, SendRawBytes)
        assert step.data == b'abc'

    def test_abort_terminates_decoding(self):
        # 0x03 → ABORT regardless of remaining bytes
        raw = bytes([3]) + b'lots-of-junk-after'
        scenario = Scenario.from_bytes(raw)
        assert len(scenario.steps) == 1
        assert isinstance(scenario.steps[0], Abort)

    @pytest.mark.parametrize('raw,cls,attr', [
        pytest.param(bytes([1, 0]), Sleep, 'duration', id='sleep-opcode'),
        pytest.param(bytes([2, 0]), ReadResponse, 'timeout', id='read-opcode'),
    ])
    def test_sleep_opcode_consumes_one_param_byte(self, raw, cls, attr):
        # 0x01 = SLEEP, 0x02 = READ; the next byte indexes the duration /
        # timeout table modulo len — one param byte per opcode.
        scenario = Scenario.from_bytes(raw)
        assert len(scenario.steps) == 1
        assert isinstance(scenario.steps[0], cls)
        assert getattr(scenario.steps[0], attr) > 0

    def test_multiple_steps_decoded_in_order(self):
        # SEND(b'X'), SLEEP, READ — encoded by hand
        raw = (
            bytes([0]) + b'\x00\x01X\x00'   # SEND length=1 data=b'X' bi-index=0
            + bytes([1, 1])                  # SLEEP duration-index=1
            + bytes([2, 2])                  # READ timeout-index=2
        )
        scenario = Scenario.from_bytes(raw)
        kinds = [type(s).__name__ for s in scenario.steps]
        assert kinds == ['SendRawBytes', 'Sleep', 'ReadResponse']


# ---------------------------------------------------------------------------
# Scenario.well_formed convenience
# ---------------------------------------------------------------------------


class TestWellFormed:
    def test_two_step_send_then_read(self):
        s = Scenario.well_formed(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n')
        assert len(s.steps) == 2
        assert isinstance(s.steps[0], SendRawBytes)
        assert s.steps[0].byte_interval == 0.0
        assert isinstance(s.steps[1], ReadResponse)

    def test_response_timeout_passed_through(self):
        s = Scenario.well_formed(b'GET / HTTP/1.1\r\nHost: a\r\n\r\n',
                                 response_timeout=12.5)
        assert isinstance(s.steps[1], ReadResponse)
        assert s.steps[1].timeout == 12.5


# ---------------------------------------------------------------------------
# HTTP1Client.execute_scenario — dispatch
# ---------------------------------------------------------------------------


class TestExecuteScenario:
    @pytest.mark.asyncio
    async def test_send_bytes_with_byte_interval_splits_writes(self):
        c, w, _ = _client_with_fakes()
        scenario = Scenario(steps=(SendRawBytes(data=b'ab', byte_interval=0.01),))
        await c.execute_scenario(scenario)
        assert w.writes == [b'a', b'b']

    @pytest.mark.asyncio
    async def test_read_response_step_populates_result(self):
        reader = _CannedReader(
            b'HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nOK')
        c, _, _ = _client_with_fakes(reader=reader)
        scenario = Scenario(steps=(ReadResponse(timeout=1.0),))
        result = await c.execute_scenario(scenario)
        assert result.response is not None
        assert result.response.status == 200
        assert result.response.body == b'OK'

    @pytest.mark.asyncio
    async def test_read_response_timeout_recorded_not_raised(self):
        c, _, _ = _client_with_fakes(reader=_NeverReader())
        scenario = Scenario(steps=(ReadResponse(timeout=0.05),))
        result = await c.execute_scenario(scenario)
        assert result.timed_out is True
        assert result.response is None
        assert result.exception is not None  # repr of TimeoutError

    @pytest.mark.asyncio
    async def test_multiple_steps_dispatched_in_order(self):
        reader = _CannedReader(
            b'HTTP/1.1 200 OK\r\ncontent-length: 0\r\n\r\n')
        c, w, _ = _client_with_fakes(reader=reader)
        scenario = Scenario(steps=(
            SendRawBytes(data=b'GET / HTTP/1.1\r\nHost: x\r\n\r\n'),
            Sleep(duration=0.01),
            ReadResponse(timeout=1.0),
        ))
        result = await c.execute_scenario(scenario)
        assert result.steps_completed == 3
        assert result.response is not None
        assert result.response.status == 200

    @pytest.mark.asyncio
    async def test_elapsed_s_is_populated(self):
        c, _, _ = _client_with_fakes()
        scenario = Scenario(steps=(Sleep(duration=0.02),))
        result = await c.execute_scenario(scenario)
        assert result.elapsed_s >= 0.015

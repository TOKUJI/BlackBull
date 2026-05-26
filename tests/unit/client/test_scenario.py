"""Sprint 17 Phase 5 — unit tests for the Scenario abstraction.

Covers:
  * ``Scenario.to_json`` / ``from_json`` round-trip for every step type
  * ``Scenario.from_bytes`` totality (every byte string yields a valid
    scenario)
  * ``Scenario.well_formed`` convenience builder
  * ``HTTP1Client.execute_scenario`` dispatch — each step type invokes
    the expected primitive, ``Abort`` short-circuits, ``ReadResponse``
    timeout folds into ``ScenarioResult`` without raising.
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest

from blackbull.client import (
    Abort,
    ReadResponse,
    Scenario,
    ScenarioResult,
    SendBytes,
    Sleep,
)
from blackbull.client.http1 import HTTP1Client
from blackbull.server.recipient import AbstractReader, IncompleteReadError
from blackbull.server.sender import AbstractWriter


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


class _CannedReader(AbstractReader):
    def __init__(self, payload: bytes) -> None:
        self._buf = payload
        self._pos = 0

    async def read(self, n: int = -1) -> bytes:
        if n < 0:
            out = self._buf[self._pos:]
            self._pos = len(self._buf)
            return out
        out = self._buf[self._pos:self._pos + n]
        self._pos += len(out)
        return out

    async def readuntil(self, separator: bytes) -> bytes:
        idx = self._buf.find(separator, self._pos)
        if idx == -1:
            partial = self._buf[self._pos:]
            self._pos = len(self._buf)
            raise IncompleteReadError(partial)
        end = idx + len(separator)
        out = self._buf[self._pos:end]
        self._pos = end
        return out

    async def readexactly(self, n: int) -> bytes:
        out = self._buf[self._pos:self._pos + n]
        if len(out) < n:
            self._pos = len(self._buf)
            raise IncompleteReadError(out)
        self._pos += n
        return out


class _NeverReader(AbstractReader):
    async def read(self, n: int = -1) -> bytes:  # pragma: no cover
        await asyncio.Event().wait()
        return b''

    async def readuntil(self, separator: bytes) -> bytes:  # pragma: no cover
        await asyncio.Event().wait()
        return b''

    async def readexactly(self, n: int) -> bytes:  # pragma: no cover
        await asyncio.Event().wait()
        return b''


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


class TestScenarioJSON:
    def test_round_trip_all_step_types(self):
        s = Scenario(steps=(
            SendBytes(data=b'GET / HTTP/1.1\r\nHost: x\r\n\r\n',
                      byte_interval=0.2),
            Sleep(duration=1.5),
            ReadResponse(timeout=3.0),
            Abort(),
        ))
        s2 = Scenario.from_json(s.to_json())
        assert s == s2

    def test_round_trip_empty_scenario(self):
        s = Scenario(steps=())
        assert Scenario.from_json(s.to_json()) == s

    def test_round_trip_preserves_bytes_with_high_bit_and_nul(self):
        # Base64 should round-trip arbitrary bytes.
        payload = bytes(range(256))
        s = Scenario(steps=(SendBytes(data=payload),))
        s2 = Scenario.from_json(s.to_json())
        assert s2.steps[0].data == payload

    def test_blank_lines_are_ignored(self):
        src = '\n{"op": "SLEEP", "duration": 0.5}\n\n{"op": "ABORT"}\n\n'
        s = Scenario.from_json(src)
        assert s == Scenario(steps=(Sleep(duration=0.5), Abort()))

    def test_unknown_op_raises(self):
        with pytest.raises(ValueError):
            Scenario.from_json('{"op": "PARTY"}')


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
        assert isinstance(step, SendBytes)
        assert step.data == b'abc'

    def test_abort_terminates_decoding(self):
        # 0x03 → ABORT regardless of remaining bytes
        raw = bytes([3]) + b'lots-of-junk-after'
        scenario = Scenario.from_bytes(raw)
        assert len(scenario.steps) == 1
        assert isinstance(scenario.steps[0], Abort)

    def test_sleep_opcode_consumes_one_param_byte(self):
        # 0x01 = SLEEP; next byte indexes the duration table modulo len.
        raw = bytes([1, 0])
        scenario = Scenario.from_bytes(raw)
        assert len(scenario.steps) == 1
        assert isinstance(scenario.steps[0], Sleep)
        assert scenario.steps[0].duration > 0

    def test_read_opcode_consumes_one_param_byte(self):
        raw = bytes([2, 0])
        scenario = Scenario.from_bytes(raw)
        assert len(scenario.steps) == 1
        assert isinstance(scenario.steps[0], ReadResponse)
        assert scenario.steps[0].timeout > 0

    def test_multiple_steps_decoded_in_order(self):
        # SEND(b'X'), SLEEP, READ — encoded by hand
        raw = (
            bytes([0]) + b'\x00\x01X\x00'   # SEND length=1 data=b'X' bi-index=0
            + bytes([1, 1])                  # SLEEP duration-index=1
            + bytes([2, 2])                  # READ timeout-index=2
        )
        scenario = Scenario.from_bytes(raw)
        kinds = [type(s).__name__ for s in scenario.steps]
        assert kinds == ['SendBytes', 'Sleep', 'ReadResponse']


# ---------------------------------------------------------------------------
# Scenario.well_formed convenience
# ---------------------------------------------------------------------------


class TestWellFormed:
    def test_two_step_send_then_read(self):
        s = Scenario.well_formed(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n')
        assert len(s.steps) == 2
        assert isinstance(s.steps[0], SendBytes)
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
    async def test_send_bytes_step_writes_to_socket(self):
        c, w, _ = _client_with_fakes()
        scenario = Scenario(steps=(SendBytes(data=b'PING'),))
        result = await c.execute_scenario(scenario)
        assert bytes(w.data) == b'PING'
        assert result.steps_completed == 1
        assert result.exception is None
        assert not result.timed_out
        assert not result.aborted

    @pytest.mark.asyncio
    async def test_send_bytes_with_byte_interval_splits_writes(self):
        c, w, _ = _client_with_fakes()
        scenario = Scenario(steps=(SendBytes(data=b'ab', byte_interval=0.01),))
        await c.execute_scenario(scenario)
        assert w.writes == [b'a', b'b']

    @pytest.mark.asyncio
    async def test_sleep_step_actually_sleeps(self):
        c, _, _ = _client_with_fakes()
        scenario = Scenario(steps=(Sleep(duration=0.05),))
        t0 = time.monotonic()
        await c.execute_scenario(scenario)
        assert time.monotonic() - t0 >= 0.04

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
    async def test_abort_step_short_circuits_and_calls_transport_abort(self):
        c, w, raw = _client_with_fakes(attach_raw_writer=True)
        scenario = Scenario(steps=(
            SendBytes(data=b'first'),
            Abort(),
            SendBytes(data=b'never'),  # must NOT be written
        ))
        result = await c.execute_scenario(scenario)
        assert result.aborted is True
        assert raw is not None and raw.transport.aborted is True
        assert bytes(w.data) == b'first'

    @pytest.mark.asyncio
    async def test_multiple_steps_dispatched_in_order(self):
        reader = _CannedReader(
            b'HTTP/1.1 200 OK\r\ncontent-length: 0\r\n\r\n')
        c, w, _ = _client_with_fakes(reader=reader)
        scenario = Scenario(steps=(
            SendBytes(data=b'GET / HTTP/1.1\r\nHost: x\r\n\r\n'),
            Sleep(duration=0.01),
            ReadResponse(timeout=1.0),
        ))
        result = await c.execute_scenario(scenario)
        assert result.steps_completed == 3
        assert result.response is not None
        assert result.response.status == 200

    @pytest.mark.asyncio
    async def test_empty_scenario_returns_empty_result(self):
        c, w, _ = _client_with_fakes()
        result = await c.execute_scenario(Scenario(steps=()))
        assert isinstance(result, ScenarioResult)
        assert result.steps_completed == 0
        assert result.response is None
        assert bytes(w.data) == b''

    @pytest.mark.asyncio
    async def test_elapsed_s_is_populated(self):
        c, _, _ = _client_with_fakes()
        scenario = Scenario(steps=(Sleep(duration=0.02),))
        result = await c.execute_scenario(scenario)
        assert result.elapsed_s >= 0.015

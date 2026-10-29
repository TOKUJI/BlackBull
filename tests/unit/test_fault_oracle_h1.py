"""The differential oracle accepts only outcomes both sides confirmed."""
from __future__ import annotations

import asyncio
import itertools
import socket
import struct

import pytest

from blackbull.fault_injection import oracle_h1, scenario_h1 as h1c
from blackbull.fault_injection.oracle_h1 import (
    ACCEPTED_CATEGORIES,
    Category,
    Observed,
    SideOutcome,
    categorize,
    run_scenario,
)


def _response(status: int) -> dict:
    return {'status': status, 'headers': [], 'body': b''}


_OUTCOMES = {
    'ok200': SideOutcome(Observed.RESPONSE, response=_response(200)),
    'ok404': SideOutcome(Observed.RESPONSE, response=_response(404)),
    'closed': SideOutcome(Observed.CLOSED),
    'broken': SideOutcome(Observed.BROKEN, exception="ProtocolError('x')"),
    'timeout': SideOutcome(Observed.TIMEOUT, exception='TimeoutError()'),
    'unmet': SideOutcome(Observed.UNMET, response=_response(200)),
    'no_read': SideOutcome(Observed.NO_READ),
    'error': SideOutcome(Observed.ERROR, exception="LookupError('x')"),
}
_CONFIRMED_EQUAL = {('ok200', 'ok200'), ('ok404', 'ok404'), ('closed', 'closed')}
_PAIRS = list(itertools.product(_OUTCOMES, repeat=2))


@pytest.mark.parametrize('ng,bb', _PAIRS, ids=[f'{a}-{b}' for a, b in _PAIRS])
def test_only_an_outcome_both_sides_confirmed_alike_is_accepted(ng, bb):
    category = categorize(_OUTCOMES[ng], _OUTCOMES[bb])
    assert (category in ACCEPTED_CATEGORIES) == ((ng, bb) in _CONFIRMED_EQUAL)


@pytest.mark.parametrize('other', list(_OUTCOMES))
def test_a_harness_error_on_either_side_is_reported_as_such(other):
    error, peer = _OUTCOMES['error'], _OUTCOMES[other]
    assert categorize(error, peer) is Category.HARNESS_ERROR
    assert categorize(peer, error) is Category.HARNESS_ERROR


@pytest.mark.parametrize('ng,bb', [
    ('timeout', 'timeout'),
    *[(u, o) for u in ('unmet', 'no_read') for o in _OUTCOMES if o != 'error'],
    *[(o, u) for u in ('unmet', 'no_read') for o in _OUTCOMES if o != 'error'],
])
def test_an_outcome_without_a_verdict_is_inconclusive(ng, bb):
    assert categorize(_OUTCOMES[ng], _OUTCOMES[bb]) is Category.INCONCLUSIVE


@pytest.mark.parametrize('answer', ['ok200', 'closed', 'broken'])
def test_silence_against_an_answer_blames_the_silent_side(answer):
    timeout, other = _OUTCOMES['timeout'], _OUTCOMES[answer]
    assert categorize(other, timeout) is Category.BB_TIMEOUT
    assert categorize(timeout, other) is Category.NG_TIMEOUT


# ---------------------------------------------------------------------------
# run_scenario observes each outcome over a real socket
# ---------------------------------------------------------------------------

_GET = h1c.SendRawBytes(b'GET / HTTP/1.1\r\nHost: a\r\n\r\n')


def _reply(status: int, body: bytes = b'') -> bytes:
    return (f'HTTP/1.1 {status} X\r\nContent-Length: {len(body)}\r\n\r\n'
            .encode() + body)


async def _observe(behaviour, steps):
    """Run *steps* against a raw server that does *behaviour* to the request."""
    async def serve(reader, writer):
        try:
            await reader.readuntil(b'\r\n\r\n')
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return
        await behaviour(writer)

    server = await asyncio.start_server(serve, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    try:
        outcome, _ = await asyncio.wait_for(
            run_scenario('127.0.0.1', port, h1c.Scenario(steps=steps)), 10.0)
    finally:
        server.close()
        await server.wait_closed()
    return outcome


def _answers(data: bytes, *, then_close: bool = True):
    async def behaviour(writer):
        writer.write(data)
        await writer.drain()
        if then_close:
            writer.close()
        else:
            await asyncio.sleep(1.0)
    return behaviour


async def _closes(writer):
    writer.close()


async def _resets(writer):
    writer.get_extra_info('socket').setsockopt(
        socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
    writer.transport.abort()


async def _stays_silent(writer):
    await asyncio.sleep(1.0)


_READ = h1c.ReadResponse(timeout=2.0)
_OBSERVATIONS = {
    'response': (_answers(_reply(200, b'hi')), (_GET, _READ), Observed.RESPONSE),
    'rejection': (_answers(_reply(400)), (_GET, _READ), Observed.RESPONSE),
    'eof': (_closes, (_GET, _READ), Observed.CLOSED),
    'reset': (_resets, (_GET, _READ), Observed.CLOSED),
    'truncated': (_answers(b'HTTP/1.1 200 X\r\nContent-Length: 9\r\n\r\nhi'),
                  (_GET, _READ), Observed.BROKEN),
    'step_timeout': (_stays_silent, (_GET, h1c.ReadResponse(timeout=0.2)),
                     Observed.TIMEOUT),
    'no_read': (_answers(_reply(200)), (_GET,), Observed.NO_READ),
    'expect_unmet': (_answers(_reply(200)),
                     (_GET, h1c.ExpectResponse(match={'status': 201}, timeout=2.0)),
                     Observed.UNMET),
    'wait_timeout': (_answers(_reply(200), then_close=False),
                     (_GET, h1c.WaitForResponse(match={'status': 201}, timeout=0.3)),
                     Observed.UNMET),
}


@pytest.mark.asyncio
@pytest.mark.parametrize('name', list(_OBSERVATIONS))
async def test_each_outcome_is_observed_as_what_it_is(name):
    behaviour, steps, observed = _OBSERVATIONS[name]
    outcome = await _observe(behaviour, steps)
    assert outcome.observed is observed, outcome
    assert outcome.result is not None
    assert outcome.elapsed_s > 0


@pytest.mark.asyncio
async def test_a_rejection_keeps_its_status():
    outcome = await _observe(_answers(_reply(400)), (_GET, _READ))
    assert outcome.response['status'] == 400


@pytest.mark.asyncio
async def test_a_runner_timeout_is_silence_not_rejection(monkeypatch):
    monkeypatch.setattr(oracle_h1, 'PER_REQUEST_TIMEOUT_S', 0.2)
    outcome = await _observe(_stays_silent, (_GET, h1c.ReadResponse(timeout=5.0)))
    assert outcome.observed is Observed.TIMEOUT


@pytest.mark.asyncio
async def test_a_refused_connection_is_a_harness_error():
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    outcome, _ = await run_scenario('127.0.0.1', port, h1c.Scenario(steps=(_GET, _READ)))
    assert outcome.observed is Observed.ERROR
    assert outcome.exception


@pytest.mark.asyncio
async def test_a_validator_fault_is_a_harness_error(monkeypatch):
    def broken(self):
        raise LookupError('harness bug')
    monkeypatch.setattr(h1c.SendRawBytes, 'wire', broken)
    outcome = await _observe(_closes, (_GET, _READ))
    assert outcome.observed is Observed.ERROR
    assert 'harness bug' in outcome.exception

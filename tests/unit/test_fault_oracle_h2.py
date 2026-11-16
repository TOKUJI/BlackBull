"""The HTTP/2 differential oracle: what it accepts, and what it observes."""
from __future__ import annotations

import asyncio

import pytest
from hpack import Encoder

from blackbull.fault_injection.oracle_h2 import (
    ACCEPTED_CATEGORIES,
    Category,
    H2Outcome,
    categorize,
    run_scenario,
)
from blackbull.fault_injection.scenario_h2_client import (
    ScenarioH2Client, SendFrame, SendHeaders, SendPreface, WaitForServerFrame,
)
from blackbull.fault_injection.scenario_h1 import Abort, HalfClose, SendRawBytes, Sleep
from blackbull.protocol.frame_types import FrameTypes


def _ok(sid=1, status=200, body=b'ok'):
    return (sid, 'response', (status, body))


_OUTCOMES = {
    'ok': H2Outcome(streams=(_ok(),)),
    'ok_other_body': H2Outcome(streams=(_ok(body=b'no'),)),
    'ok_404': H2Outcome(streams=(_ok(status=404),)),
    'rejected': H2Outcome(streams=((1, 'rejected', None),)),
    'reset_refused': H2Outcome(streams=((1, 'reset', 'REFUSED_STREAM'),)),
    'goaway_protocol': H2Outcome(goaway='PROTOCOL_ERROR', teardown='closed',
                                 streams=((1, 'connection_error', None),)),
    'goaway_compression': H2Outcome(goaway='COMPRESSION_ERROR', teardown='closed',
                                    streams=((1, 'connection_error', None),)),
    'ok_then_closed': H2Outcome(streams=(_ok(),), teardown='closed'),
    'timeout': H2Outcome(streams=((1, 'timeout', None),)),
    'error': H2Outcome(error="OSError('refused')"),
}


@pytest.mark.parametrize('a,b,expected', [
    ('ok', 'ok', Category.OK),
    ('rejected', 'rejected', Category.BOTH_REJECTED),
    ('goaway_protocol', 'goaway_protocol', Category.BOTH_REJECTED),
    ('ok', 'ok_other_body', Category.BODY_DIFFER),
    ('ok', 'ok_404', Category.STATUS_DIFFER),
    ('rejected', 'ok_404', Category.REJECT_DIFFER),
    ('ok', 'rejected', Category.REJECT_DIFFER),
    ('rejected', 'reset_refused', Category.ERROR_DIFFER),
    ('rejected', 'goaway_protocol', Category.ERROR_DIFFER),
    ('goaway_protocol', 'goaway_compression', Category.ERROR_DIFFER),
    ('ok', 'ok_then_closed', Category.TEARDOWN_DIFFER),
    ('ok', 'timeout', Category.TIMEOUT_DIFFER),
    ('error', 'ok', Category.HARNESS_ERROR),
    ('ok', 'error', Category.HARNESS_ERROR),
])
def test_each_difference_has_its_own_category(a, b, expected):
    assert categorize(_OUTCOMES[a], _OUTCOMES[b]) is expected
    assert categorize(_OUTCOMES[b], _OUTCOMES[a]) is expected


def test_only_agreement_is_accepted():
    accepted = {(a, b) for a in _OUTCOMES for b in _OUTCOMES
                if categorize(_OUTCOMES[a], _OUTCOMES[b]) in ACCEPTED_CATEGORIES}
    alike = {(a, b) for a in _OUTCOMES for b in _OUTCOMES
             if _OUTCOMES[a] == _OUTCOMES[b] and a not in ('error', 'timeout')}
    assert accepted == alike


def test_a_400_response_and_a_protocol_reset_are_one_rejection():
    """RFC 9113 §8.1.1 lets a server answer a malformed request before it resets."""
    answered = H2Outcome(streams=(_ok(status=400, body=b'<html>bad</html>'),))
    assert answered.streams == ((1, 'rejected', None),)
    assert categorize(answered, _OUTCOMES['rejected']) is Category.BOTH_REJECTED


def test_another_4xx_is_an_answer_not_a_rejection():
    """A 404 to a valid request must not hide a reset on the other side."""
    assert H2Outcome(streams=(_ok(status=404),)).streams == (_ok(status=404),)


# ---------------------------------------------------------------------------
# The observer: its own frame reader, never BlackBull's parser
# ---------------------------------------------------------------------------

def _frame(t, flags, sid, payload=b''):
    return len(payload).to_bytes(3, 'big') + bytes([t, flags]) + sid.to_bytes(4, 'big') + payload


_GET = ((':method', 'GET'), (':scheme', 'http'), (':authority', 'localhost'), (':path', '/'))


class _Peer:
    """A scripted server: answers each connection with *reply(data_so_far)*."""

    def __init__(self, reply, settings_first=True):
        self.reply = reply
        self.settings_first = settings_first
        self.received = b''

    async def handle(self, reader, writer):
        try:
            if self.settings_first:
                writer.write(_frame(4, 0, 0))
            while chunk := await reader.read(65536):
                self.received += chunk
                out = self.reply(self.received)
                if out is not None:
                    if out == 'abort':
                        writer.transport.abort()
                        return
                    out, close = out if isinstance(out, tuple) else (out, False)
                    writer.write(out)
                    if close:
                        writer.write_eof()   # FIN; keep reading so no unread data forces a reset
                    self.reply = lambda _: None
        except ConnectionError:
            pass
        finally:
            writer.close()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()


def _answer(status, body):
    block = Encoder().encode([(':status', str(status))])
    return _frame(1, 0x4, 1, block) + _frame(0, 0x1, 1, body)


def _after_request(out):
    return lambda data: out if len(data) > 24 + 9 + 9 else None


_REQUEST = ScenarioH2Client(steps=(
    SendPreface(), SendFrame(FrameTypes.SETTINGS), SendHeaders(pseudo=_GET, end_stream=True)))


@pytest.mark.asyncio
@pytest.mark.parametrize('reply,expected', [
    pytest.param(_answer(200, b'ok'), H2Outcome(streams=(_ok(),)), id='response'),
    pytest.param(_frame(3, 0, 1, (1).to_bytes(4, 'big')),
                 H2Outcome(streams=((1, 'rejected', None),)), id='protocol-reset'),
    pytest.param(_frame(3, 0, 1, (7).to_bytes(4, 'big')),
                 H2Outcome(streams=((1, 'reset', 'REFUSED_STREAM'),)), id='refused'),
    pytest.param((_frame(7, 0, 0, bytes(4) + (9).to_bytes(4, 'big')), True),
                 H2Outcome(streams=((1, 'connection_error', None),),
                           goaway='COMPRESSION_ERROR', teardown='closed'), id='goaway'),
    pytest.param('abort', H2Outcome(streams=((1, 'open', None),), teardown='closed'),
                 id='reset-connection'),
])
async def test_the_observer_reads_what_the_peer_sent(reply, expected):
    async with _Peer(_after_request(reply)) as peer:
        outcome = await run_scenario('127.0.0.1', peer.port, _REQUEST, quiet=0.3)
    assert outcome == expected


@pytest.mark.asyncio
async def test_the_observer_acknowledges_settings_and_ping():
    """A conforming endpoint for everything the scenario does not fault."""
    ping = _frame(6, 0, 0, b'12345678')
    async with _Peer(_after_request(ping + _answer(200, b'ok'))) as peer:
        await run_scenario('127.0.0.1', peer.port, _REQUEST, quiet=0.3)
    assert _frame(4, 1, 0) in peer.received
    assert _frame(6, 1, 0, b'12345678') in peer.received


@pytest.mark.asyncio
async def test_a_connection_that_cannot_open_is_a_harness_error():
    async with _Peer(lambda _: None) as peer:
        port = peer.port
    outcome = await run_scenario('127.0.0.1', port, _REQUEST, quiet=0.3)
    assert outcome.error is not None
    assert categorize(outcome, outcome) is Category.HARNESS_ERROR


@pytest.mark.asyncio
async def test_transport_steps_run_and_a_read_step_is_refused():
    steps = (SendPreface(), Sleep(0.01), HalfClose(), Abort())
    async with _Peer(lambda _: None) as peer:
        outcome = await run_scenario('127.0.0.1', peer.port,
                                     ScenarioH2Client(steps=steps), quiet=0.3)
        with pytest.raises(TypeError, match='WaitForServerFrame'):
            await run_scenario('127.0.0.1', peer.port, ScenarioH2Client(steps=(
                SendPreface(), WaitForServerFrame(match={'type': 'SETTINGS'}))))
    assert outcome.error is None


@pytest.mark.asyncio
async def test_an_acknowledgement_never_lands_inside_a_split_frame():
    """The scenario owns frame boundaries: a split frame stays contiguous."""
    request = SendHeaders(pseudo=_GET, end_stream=True).wire()
    scenario = ScenarioH2Client(steps=(
        SendPreface(), SendFrame(FrameTypes.SETTINGS),
        SendRawBytes(request[:9]), Sleep(0.3), SendRawBytes(request[9:])))
    async with _Peer(lambda data: None) as peer:
        await run_scenario('127.0.0.1', peer.port, scenario, quiet=0.3)
    assert request in peer.received


@pytest.mark.asyncio
async def test_after_a_half_close_the_observer_still_reads():
    """SETTINGS that arrive after our FIN cannot be acknowledged, but are read."""
    scenario = ScenarioH2Client(steps=(
        SendPreface(), SendFrame(FrameTypes.SETTINGS),
        SendHeaders(pseudo=_GET, end_stream=True), HalfClose()))
    answer = _frame(4, 0, 0) + _answer(200, b'ok')
    async with _Peer(_after_request(answer), settings_first=False) as peer:
        outcome = await run_scenario('127.0.0.1', peer.port, scenario, quiet=0.3)
    # The peer closes once it reads our FIN; the answer before it was still read.
    assert outcome == H2Outcome(streams=(_ok(),), teardown='closed')


def test_under_the_same_connection_error_an_answered_stream_is_not_a_divergence():
    """RFC 9113 §5.4.1: whether an earlier stream completes before the GOAWAY is timing."""
    dropped = H2Outcome(goaway='PROTOCOL_ERROR', teardown='closed',
                        streams=((1, 'connection_error', None), (3, 'connection_error', None)))
    answered = H2Outcome(goaway='PROTOCOL_ERROR', teardown='closed',
                         streams=(_ok(sid=1), (3, 'connection_error', None)))
    assert categorize(dropped, answered) is Category.BOTH_REJECTED
    other_code = H2Outcome(goaway='COMPRESSION_ERROR', teardown='closed', streams=answered.streams)
    assert categorize(dropped, other_code) not in ACCEPTED_CATEGORIES
    no_goaway = H2Outcome(streams=(_ok(sid=1), (3, 'open', None)))
    assert categorize(answered, no_goaway) is not Category.BOTH_REJECTED


@pytest.mark.asyncio
async def test_the_observer_returns_flow_control_credit():
    """Consumed DATA is credited on the connection, and on a stream still open."""
    body = (_frame(1, 0x4, 1, Encoder().encode([(':status', '200')]))
            + _frame(0, 0, 1, b'x' * 100) + _frame(0, 0x1, 1, b'y' * 7))
    async with _Peer(_after_request(body)) as peer:
        outcome = await run_scenario('127.0.0.1', peer.port, _REQUEST, quiet=0.3)
    assert outcome.streams == (_ok(body=b'x' * 100 + b'y' * 7),)
    assert _frame(8, 0, 1, (100).to_bytes(4, 'big')) in peer.received
    assert _frame(8, 0, 0, (100).to_bytes(4, 'big')) in peer.received
    assert _frame(8, 0, 0, (7).to_bytes(4, 'big')) in peer.received
    assert _frame(8, 0, 1, (7).to_bytes(4, 'big')) not in peer.received


@pytest.mark.asyncio
async def test_an_interleaved_field_block_is_recorded():
    """A server that splits a field block with another frame is malformed (§6.10)."""
    block = Encoder().encode([(':status', '200')])
    reply = _frame(1, 0, 1, block[:1]) + _frame(6, 0, 0, bytes(8)) + _frame(9, 0x4, 1, block[1:])
    async with _Peer(_after_request(reply)) as peer:
        outcome = await run_scenario('127.0.0.1', peer.port, _REQUEST, quiet=0.3)
    assert outcome.streams == ((1, 'reset', 'INTERLEAVED_FIELD_BLOCK'),)


@pytest.mark.asyncio
async def test_only_streams_the_scenario_opened_are_tracked():
    """Raw bytes are not re-read as frames: a stray HEADERS type byte opens nothing."""
    scenario = ScenarioH2Client(steps=(
        SendPreface(), SendFrame(FrameTypes.SETTINGS),
        SendRawBytes(b'\x00\x00\x00\x01\x05\x00\x00\x00\x07'),
        SendHeaders(pseudo=_GET, stream_id=3, end_stream=True)))
    async with _Peer(lambda data: None) as peer:
        outcome = await run_scenario('127.0.0.1', peer.port, scenario, quiet=0.3)
    assert outcome.streams == ((3, 'open', None),)

"""One scenario model under four vocabularies.

The steps a fault scenario takes — send, wait, expect, sleep, close — mean
the same thing in every cell, so the transport steps are one class each, and
every vocabulary writes and reads one JSON Lines format.  What differs by
protocol and role is only how a step goes onto the wire.
"""
from __future__ import annotations

import typing
from pathlib import Path

import pytest

from blackbull.fault_injection import (
    scenario_h1 as h1c,
    scenario_h1_server as h1s,
    scenario_h2 as h2s,
    scenario_h2_client as h2c,
)
from blackbull.protocol.frame_types import FrameTypes, GoAway, Ping

VOCABULARIES = (h1c, h1s, h2s, h2c)


def test_the_transport_steps_are_one_class_in_every_vocabulary():
    for name in ('SendRawBytes', 'Sleep', 'Abort', 'HalfClose'):
        assert len({getattr(m, name) for m in VOCABULARIES}) == 1, name
    assert h1c.ReadResponse is h2c.ReadResponse


# ---------------------------------------------------------------------------
# JSON Lines: every step of every vocabulary, one format
# ---------------------------------------------------------------------------

_PING = Ping(length=8, type_=FrameTypes.PING, flags=1, stream_id=0,
             data=b'12345678')
_GOAWAY = GoAway(length=10, type_=FrameTypes.GOAWAY, flags=0, stream_id=0,
                 data=(1).to_bytes(4, 'big') + (2).to_bytes(4, 'big') + b'zz')

#: (to_json, from_json, a scenario using every step its vocabulary has)
EVERY_STEP = {
    'h1c': (lambda s: s.to_json(), h1c.Scenario.from_json, h1c.Scenario(
        name='h1c', steps=(
            h1c.SendRawBytes(b'GET / HTTP/1.1\r\n\r\n', byte_interval=0.05),
            h1c.Sleep(0.25), h1c.ReadResponse(timeout=2.0), h1c.HalfClose(),
            h1c.WaitForResponse(match={'status': 200, 'header': (b'x', b'y')},
                                timeout=1.5),
            h1c.ExpectResponse(match={'body_contains': b'\xffok'}, timeout=1.0),
            h1c.Abort()))),
    'h1s': (h1s.scenario_to_json, h1s.scenario_from_json, h1s.ScenarioH1Server(
        name='h1s', steps=(
            h1s.WaitForRequest(match={'method': 'POST',
                                      'header': ('expect', None)}, timeout=2.0),
            h1s.ExpectRequest(match={'target': '/'}, timeout=1.0),
            h1s.SendStatusLine(code=404, reason='', omit_reason=True),
            h1s.SendHeader('x-a', 'b'), h1s.SendHeader('', 'more', fold=True),
            h1s.EndHeaders(),
            h1s.SendChunk(b'abc', declared_size=9, extension='e=1',
                          terminator=b'\n'),
            h1s.EndChunkedBody(trailers=(('x-t', '1'),)),
            h1s.SendRawBytes(b'\x00\xff', byte_interval=0.2),
            h1s.Sleep(0.5), h1s.HalfClose(), h1s.CloseGracefully(),
            h1s.Abort()))),
    'h2s': (h2s.scenario_to_json, h2s.scenario_from_json, h2s.ScenarioH2(
        name='h2s', send_preface=False, initial_settings=((4, 0), (5, 16383)),
        steps=(
            h2s.SendFrame(_PING), h2s.SendFrame(_GOAWAY, declared_length=99),
            h2s.SendRawBytes(b'\x00\x01', byte_interval=0.1),
            h2s.WaitForClientFrame(match={'type': 'HEADERS', 'stream_id': 1,
                                          'flags_set': ['END_STREAM']},
                                   timeout=3.0),
            h2s.ExpectClientFrame(match={'type': 'SETTINGS'}, timeout=1.0),
            h2s.Sleep(0.5), h2s.HalfClose(),
            h2s.CloseGracefully(error_code=1, last_stream_id=3), h2s.Abort()))),
    'h2c': (h2c.scenario_to_json, h2c.scenario_from_json, h2c.ScenarioH2Client(
        name='h2c', steps=(
            h2c.SendPreface(),
            h2c.SendFrame(FrameTypes.SETTINGS, flags=1, stream_id=0,
                          data=b'\x00' * 6, declared_length=7),
            h2c.SendFrame(0xfa),
            h2c.SendHeaders(pseudo=((':method', 'GET'),),
                            headers=(('x-a', '1'),), stream_id=3,
                            end_stream=True, end_headers=False,
                            raw_block=b'\x82', declared_length=5),
            h2c.SendRawBytes(b'\xff', byte_interval=0.05), h2c.Sleep(0.25),
            h2c.ReadResponse(timeout=2.0), h2c.HalfClose(),
            h2c.WaitForServerFrame(match={'type': 'GOAWAY', 'error_code': 6},
                                   timeout=5.0),
            h2c.ExpectServerFrame(match={'type': 'SETTINGS'}, timeout=1.0),
            h2c.Abort()))),
}

_MODULES = {'h1c': h1c, 'h1s': h1s, 'h2s': h2s, 'h2c': h2c}


@pytest.mark.parametrize('cell', sorted(EVERY_STEP))
def test_the_sample_uses_every_step_its_vocabulary_has(cell):
    _, _, scenario = EVERY_STEP[cell]
    declared = set(typing.get_args(_MODULES[cell].Step))
    assert {type(s) for s in scenario.steps} == declared


@pytest.mark.parametrize('cell', sorted(EVERY_STEP))
def test_every_step_survives_a_json_round_trip(cell):
    # Frames compare by identity, so the record is what must survive.
    to_json, from_json, scenario = EVERY_STEP[cell]
    assert to_json(from_json(to_json(scenario))) == to_json(scenario)


@pytest.mark.parametrize('cell, role', [
    ('h1c', 'h1_client'), ('h1s', 'h1_server'),
    ('h2s', 'h2_server'), ('h2c', 'h2_client')])
def test_every_catalogue_case_survives_a_json_round_trip(cell, role):
    from blackbull.fault_injection.catalogue import CATALOGUES
    to_json, from_json, _ = EVERY_STEP[cell]
    for name, build in CATALOGUES[role].items():
        text = to_json(build())
        assert to_json(from_json(text)) == text, name


def test_a_match_keeps_its_bytes_through_json():
    # A response is matched over bytes, so a saved match must keep them.
    scenario = h1c.Scenario(steps=(h1c.ExpectResponse(
        match={'body_contains': b'\xff ok', 'header': (b'x-a', b'1'),
               'header_absent': b'x-b'}),))
    assert h1c.Scenario.from_json(scenario.to_json()) == scenario


def test_the_format_is_one_shape_for_every_vocabulary():
    """Named by class, bytes as hex, the version on the header line."""
    scenario = h1s.ScenarioH1Server(name='n', steps=(
        h1s.SendRawBytes(b'\x00\xff'), h1s.WaitForRequest(timeout=1.0)))
    assert h1s.scenario_to_json(scenario).splitlines() == [
        '{"op": "HEADER", "format": 2, "name": "n"}',
        '{"op": "SendRawBytes", "data": "00ff", "byte_interval": 0.0}',
        '{"op": "WaitForRequest", "match": {}, "timeout": 1.0}',
    ]
    client = h1c.Scenario(name='n', steps=(h1c.SendRawBytes(b'\x00\xff'),))
    assert client.to_json().splitlines()[1] == \
        '{"op": "SendRawBytes", "data": "00ff", "byte_interval": 0.0}'


@pytest.mark.parametrize('cell', sorted(EVERY_STEP))
def test_a_step_from_another_vocabulary_is_refused(cell):
    _, from_json, _ = EVERY_STEP[cell]
    alien = 'SendPreface' if cell != 'h2c' else 'SendStatusLine'
    with pytest.raises(ValueError, match=alien):
        from_json('{"op": "HEADER", "format": 2, "name": ""}\n'
                  f'{{"op": "{alien}"}}')


def test_an_unknown_format_version_is_refused():
    with pytest.raises(ValueError, match='format 3'):
        h1s.scenario_from_json('{"op": "HEADER", "format": 3, "name": ""}')


# ---------------------------------------------------------------------------
# The v1 format still reads, with a warning, until it is removed
# ---------------------------------------------------------------------------

#: What each vocabulary wrote before the formats were unified, verbatim.
V1 = {
    'h1c': '''{"op": "HEADER", "name": "h1c"}
{"op": "SEND", "data": "R0VUIC8gSFRUUC8xLjENCg0K", "byte_interval": 0.05}
{"op": "SLEEP", "duration": 0.25}
{"op": "READ", "timeout": 2.0}
{"op": "HALF_CLOSE"}
{"op": "WAIT_FOR_RESPONSE", "timeout": 1.5, "match": {"status": 200, "header": ["x", "y"]}}
{"op": "EXPECT_RESPONSE", "timeout": 1.0, "match": {"body_contains": "ok"}}
{"op": "ABORT"}''',
    'h1s': '''{"op": "HEADER", "name": "h1s"}
{"op": "WAIT_FOR_REQUEST", "timeout": 2.0, "match": {"method": "POST", "header": ["expect", null]}}
{"op": "EXPECT_REQUEST", "timeout": 1.0, "match": {"target": "/"}}
{"op": "SEND_RAW", "data": "485454502f312e3120323030204f4b0d0a", "byte_interval": 0.2}
{"op": "SLEEP", "duration": 0.5}
{"op": "HALF_CLOSE"}
{"op": "CLOSE_GRACEFULLY"}
{"op": "ABORT"}''',
    'h2s': '''{"op": "HEADER", "name": "h2s", "send_preface": false, "initial_settings": [[4, 0], [5, 16383]]}
{"op": "SEND_FRAME", "frame": {"class": "Ping", "stream_id": 0, "flags": 1, "payload": "MTIzNDU2Nzg="}, "declared_length": null}
{"op": "SEND_FRAME", "frame": {"class": "GoAway", "stream_id": 0, "flags": 0, "last_stream_id": 1, "error_code": 2, "append_data": "eno="}, "declared_length": 99}
{"op": "SEND_RAW", "data": "AAE=", "byte_interval": 0.1}
{"op": "WAIT", "match": {"type": "HEADERS", "stream_id": 1, "flags_set": ["END_STREAM"]}, "timeout": 3.0}
{"op": "EXPECT", "match": {"type": "SETTINGS"}, "timeout": 1.0}
{"op": "SLEEP", "duration": 0.5}
{"op": "HALF_CLOSE"}
{"op": "GOAWAY_CLOSE", "error_code": 1, "last_stream_id": 3}
{"op": "ABORT"}''',
    'h2c': '''{"op": "HEADER", "name": "h2c"}
{"op": "PREFACE"}
{"op": "SEND_FRAME", "frame_type": 4, "flags": 1, "stream_id": 0, "data": "000000000000", "declared_length": 7}
{"op": "SEND_FRAME", "frame_type": 250, "flags": 0, "stream_id": 0, "data": "", "declared_length": null}
{"op": "SEND", "data": "ff", "byte_interval": 0.05}
{"op": "SLEEP", "duration": 0.25}
{"op": "READ", "timeout": 2.0}
{"op": "HALF_CLOSE"}
{"op": "WAIT_FOR_SERVER_FRAME", "timeout": 5.0, "match": {"type": "GOAWAY", "error_code": 6}}
{"op": "EXPECT_SERVER_FRAME", "timeout": 1.0, "match": {"type": "SETTINGS"}}
{"op": "ABORT"}''',
}

#: The v1 scenarios, as they were built: v1 could not carry the typed
#: HTTP/1.1 server steps nor SendHeaders, and its body match was a str.
V1_SCENARIOS = {
    'h1c': h1c.Scenario(name='h1c', steps=(
        h1c.SendRawBytes(b'GET / HTTP/1.1\r\n\r\n', byte_interval=0.05),
        h1c.Sleep(0.25), h1c.ReadResponse(timeout=2.0), h1c.HalfClose(),
        h1c.WaitForResponse(match={'status': 200, 'header': ('x', 'y')},
                            timeout=1.5),
        h1c.ExpectResponse(match={'body_contains': 'ok'}, timeout=1.0),
        h1c.Abort())),
    'h1s': h1s.ScenarioH1Server(name='h1s', steps=(
        h1s.WaitForRequest(match={'method': 'POST', 'header': ('expect', None)},
                           timeout=2.0),
        h1s.ExpectRequest(match={'target': '/'}, timeout=1.0),
        h1s.SendRawBytes(b'HTTP/1.1 200 OK\r\n', byte_interval=0.2),
        h1s.Sleep(0.5), h1s.HalfClose(), h1s.CloseGracefully(), h1s.Abort())),
    'h2s': h2s.ScenarioH2(
        name='h2s', send_preface=False, initial_settings=((4, 0), (5, 16383)),
        steps=(
            h2s.SendFrame(_PING), h2s.SendFrame(_GOAWAY, declared_length=99),
            h2s.SendRawBytes(b'\x00\x01', byte_interval=0.1),
            h2s.WaitForClientFrame(match={'type': 'HEADERS', 'stream_id': 1,
                                          'flags_set': ['END_STREAM']},
                                   timeout=3.0),
            h2s.ExpectClientFrame(match={'type': 'SETTINGS'}, timeout=1.0),
            h2s.Sleep(0.5), h2s.HalfClose(),
            h2s.CloseGracefully(error_code=1, last_stream_id=3), h2s.Abort())),
    'h2c': h2c.ScenarioH2Client(name='h2c', steps=(
        h2c.SendPreface(),
        h2c.SendFrame(FrameTypes.SETTINGS, flags=1, stream_id=0,
                      data=b'\x00' * 6, declared_length=7),
        h2c.SendFrame(0xfa),
        h2c.SendRawBytes(b'\xff', byte_interval=0.05), h2c.Sleep(0.25),
        h2c.ReadResponse(timeout=2.0), h2c.HalfClose(),
        h2c.WaitForServerFrame(match={'type': 'GOAWAY', 'error_code': 6},
                               timeout=5.0),
        h2c.ExpectServerFrame(match={'type': 'SETTINGS'}, timeout=1.0),
        h2c.Abort())),
}


@pytest.mark.parametrize('cell', sorted(V1))
def test_a_v1_file_still_reads_and_says_how_to_rewrite_it(cell):
    to_json, from_json, _ = EVERY_STEP[cell]
    with pytest.warns(DeprecationWarning, match='to_json'):
        scenario = from_json(V1[cell])
    assert to_json(scenario) == to_json(V1_SCENARIOS[cell])


def test_a_v1_file_without_a_header_line_or_with_blank_lines_still_reads():
    with pytest.warns(DeprecationWarning):
        scenario = h1c.Scenario.from_json('\n{"op": "ABORT"}\n\n')
    assert scenario == h1c.Scenario(steps=(h1c.Abort(),))


def test_a_v1_file_reads_as_its_own_writer_could_have_written_it():
    """Whole-number floats, a null match, a HEADER line anywhere."""
    with pytest.warns(DeprecationWarning):
        scenario = h1c.Scenario.from_json(
            '{"op": "SLEEP", "duration": 1}\n'
            '{"op": "WAIT_FOR_RESPONSE", "timeout": 2, "match": null}\n'
            '{"op": "HEADER", "name": "late"}')
    assert scenario.name == 'late'
    assert scenario.steps == (h1c.Sleep(1.0), h1c.WaitForResponse(timeout=2.0))
    assert isinstance(scenario.steps[0].duration, float)
    assert isinstance(scenario.steps[1].timeout, float)


def test_the_saved_scenarios_are_in_the_current_format(recwarn):
    corpus = Path(__file__).parents[1] / 'conformance/http1/fuzz/user-corpus'
    files = sorted(corpus.glob('*.jsonl'))
    assert files
    for path in files:
        scenario = h1c.Scenario.from_json(path.read_text())
        assert scenario.steps, path.name
    assert not [w for w in recwarn if w.category is DeprecationWarning]


# ---------------------------------------------------------------------------
# One step semantics, four cells: every row runs in every cell
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402
import socket  # noqa: E402
import struct  # noqa: E402

from blackbull.client.http1 import HTTP1Client  # noqa: E402
from blackbull.client.http2 import HTTP2Client  # noqa: E402
from blackbull.fault_injection import H1FaultServer, H2FaultServer  # noqa: E402
from blackbull.fault_injection.scenario_h2_client import CLIENT_PREFACE  # noqa: E402

_SETTINGS = b'\x00\x00\x00\x04\x00\x00\x00\x00\x00'
_PING_FRAME = b'\x00\x00\x08\x06\x00\x00\x00\x00\x00' + b'\x00' * 8


async def _read_to_eof(reader) -> bytes:
    """What the peer sent before it closed — and it must close."""
    try:
        return await asyncio.wait_for(reader.read(-1), timeout=5.0)
    except ConnectionError:
        return b''


def _end(writer, ending) -> None:
    """Close as the peer: ``'eof'`` sends FIN, ``'reset'`` sends RST."""
    if ending == 'reset':
        sock = writer.get_extra_info('socket')
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                        struct.pack('ii', 1, 0))
        writer.transport.abort()
    else:
        writer.close()


async def _against_a_fault_server(server_cls, scenario, prelude, peer, ending=None):
    """A raw client sends *peer*; returns the server's result and what it got.

    With *ending*, the client closes right after sending instead of waiting.
    """
    async with server_cls(scenario) as srv:
        reader, writer = await asyncio.open_connection('127.0.0.1', srv.port)
        writer.write(prelude + b''.join(peer))
        await writer.drain()
        if ending:
            _end(writer, ending)
            await srv.wait_for_connection_done(timeout=5.0)
            return srv.last_result, b''
        got = await _read_to_eof(reader)
        writer.close()
        await srv.wait_for_connection_done(timeout=5.0)
        return srv.last_result, got


async def _against_a_raw_server(run, peer, ending=None):
    """A raw server sends *peer*; returns the client's result and what it got.

    With *ending*, the server closes right after sending instead of waiting.
    """
    received = bytearray()
    closed = asyncio.Event()

    async def serve(reader, writer):
        writer.write(b''.join(peer))
        await writer.drain()
        if ending:
            _end(writer, ending)
            closed.set()
            return
        received.extend(await _read_to_eof(reader))
        closed.set()
        writer.close()

    server = await asyncio.start_server(serve, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    try:
        result = await asyncio.wait_for(run(port, closed), timeout=10.0)
        # Before 3.12, Server.wait_closed() does not wait for the handler.
        await asyncio.wait_for(closed.wait(), timeout=5.0)
    finally:
        server.close()
        await server.wait_closed()
    return result, bytes(received)


async def _h1c(steps, peer, ending=None):
    async def run(port, closed):
        async with HTTP1Client('127.0.0.1', port) as client:
            result = await client.execute_scenario(h1c.Scenario(steps=steps))
            if result.aborted:
                # An abort closes the connection itself, not the client's exit.
                await asyncio.wait_for(closed.wait(), timeout=2.0)
            return result
    return await _against_a_raw_server(run, peer, ending)


async def _h2c(steps, peer, ending=None):
    async def run(port, closed):
        async with HTTP2Client('127.0.0.1', port, scenario_mode=True) as client:
            result = await client.execute_scenario(h2c.ScenarioH2Client(steps=steps))
            if result.aborted:
                # An abort closes the connection itself, not the client's exit.
                await asyncio.wait_for(closed.wait(), timeout=2.0)
            return result
    return await _against_a_raw_server(run, peer, ending)


async def _h1s(steps, peer, ending=None):
    return await _against_a_fault_server(
        H1FaultServer, h1s.ScenarioH1Server(steps=steps), b'', peer, ending)


async def _h2s(steps, peer, ending=None):
    return await _against_a_fault_server(
        H2FaultServer, h2s.ScenarioH2(steps=steps, send_preface=False),
        CLIENT_PREFACE, peer, ending)


#: cell -> (driver, its wait and expect steps, a message that matches
#: ``match``, one that does not, ``match``)
CELLS = {
    'h1c': (_h1c, h1c.WaitForResponse, h1c.ExpectResponse,
            b'HTTP/1.1 201 Created\r\nContent-Length: 0\r\n\r\n',
            b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n', {'status': 201}),
    'h2c': (_h2c, h2c.WaitForServerFrame, h2c.ExpectServerFrame,
            _PING_FRAME, _SETTINGS, {'type': 'PING'}),
    'h1s': (_h1s, h1s.WaitForRequest, h1s.ExpectRequest,
            b'POST /c HTTP/1.1\r\nHost: a\r\n\r\n',
            b'GET /a HTTP/1.1\r\nHost: a\r\n\r\n', {'method': 'POST'}),
    'h2s': (_h2s, h2s.WaitForClientFrame, h2s.ExpectClientFrame,
            _PING_FRAME, _SETTINGS, {'type': 'PING'}),
}


def each_cell(test):
    return pytest.mark.asyncio(pytest.mark.parametrize('cell', sorted(CELLS))(test))


def _completed(result) -> int:
    done = result.steps_completed
    return len(done) if isinstance(done, list) else done


def _ended(result) -> bool:
    return getattr(result, 'aborted', False) or getattr(result, 'terminated', False)


@each_cell
async def test_a_wait_skips_until_a_match_and_counts_the_skips(cell):
    run, wait, _, hit, miss, match = CELLS[cell]
    result, _ = await run((wait(match=match, timeout=3.0),), [miss, hit])
    assert (result.wait_skipped, result.wait_timed_out) == (1, False)
    assert _completed(result) == 1


@each_cell
async def test_a_wait_that_never_matches_times_out_and_the_next_step_runs(cell):
    run, wait, _, _, miss, match = CELLS[cell]
    result, _ = await run((wait(match=match, timeout=0.3), Sleep0()), [miss])
    assert (result.wait_skipped, result.wait_timed_out) == (1, True)
    assert _completed(result) == 2


@each_cell
async def test_an_expect_records_a_held_premise(cell):
    run, _, expect, hit, _, match = CELLS[cell]
    result, _ = await run((expect(match=match, timeout=3.0),), [hit])
    assert result.expectations == [(match, True)]


@each_cell
async def test_an_expect_records_a_broken_premise_and_skips_nothing(cell):
    run, _, expect, hit, miss, match = CELLS[cell]
    result, _ = await run((expect(match=match, timeout=3.0),), [miss, hit])
    assert result.expectations == [(match, False)]
    assert result.wait_skipped == 0
    assert result.exception is None


@each_cell
async def test_an_expect_that_hears_nothing_records_the_miss(cell):
    run, _, expect, _, _, match = CELLS[cell]
    result, _ = await run((expect(match=match, timeout=0.3), Sleep0()), [])
    assert result.expectations == [(match, False)]
    assert result.wait_timed_out is True
    assert _completed(result) == 2


@each_cell
async def test_a_half_close_is_not_terminal_and_the_peer_is_still_heard(cell):
    run, _, expect, hit, _, match = CELLS[cell]
    result, _ = await run((h1c.HalfClose(), expect(match=match, timeout=3.0)),
                          [hit])
    assert result.half_closed is True
    assert result.expectations == [(match, True)]
    assert not _ended(result)


@each_cell
async def test_an_abort_is_terminal(cell):
    run = CELLS[cell][0]
    result, got = await run((h1c.Abort(), h1c.SendRawBytes(b'never')), [])
    assert _ended(result)
    assert b'never' not in got


@each_cell
async def test_raw_bytes_reach_the_peer_paced(cell):
    run = CELLS[cell][0]
    result, got = await run((h1c.SendRawBytes(b'xyz', byte_interval=0.01),), [])
    assert b'xyz' in got
    assert result.exception is None


@each_cell
@pytest.mark.parametrize('ending', ['eof', 'reset'])
@pytest.mark.parametrize('kind', ['wait', 'expect'])
async def test_a_peer_that_closes_ends_the_walk_closed_not_timed_out(cell, kind, ending):
    run, wait, expect, _, _, match = CELLS[cell]
    read = (wait if kind == 'wait' else expect)(match=match, timeout=3.0)
    result, _ = await run((read, Sleep0()), [], ending=ending)
    assert (result.peer_closed, result.wait_timed_out) == (True, False)
    assert result.exception is None
    assert result.expectations == []
    assert _completed(result) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['eof', 'reset'])
@pytest.mark.parametrize('cell', ['h1c', 'h2c'])
async def test_a_read_that_meets_a_closed_peer_records_the_close(cell, ending):
    result, _ = await CELLS[cell][0]((h1c.ReadResponse(timeout=3.0), Sleep0()),
                                     [], ending=ending)
    assert (result.peer_closed, result.timed_out) == (True, False)
    assert result.exception is None
    assert _completed(result) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('cell', ['h1c', 'h2c'])
async def test_a_harness_fault_is_raised_not_blamed_on_the_peer(cell, monkeypatch):
    def broken(self):
        raise LookupError('harness bug')
    monkeypatch.setattr(h1c.SendRawBytes, 'wire', broken)
    client_cls, scenario_cls, kwargs = {
        'h1c': (HTTP1Client, h1c.Scenario, {}),
        'h2c': (HTTP2Client, h2c.ScenarioH2Client, {'scenario_mode': True})}[cell]

    async def run(port, closed):
        async with client_cls('127.0.0.1', port, **kwargs) as client:
            with pytest.raises(LookupError, match='harness bug'):
                await client.execute_scenario(
                    scenario_cls(steps=(h1c.SendRawBytes(b'x'),)))

    await _against_a_raw_server(run, [])


def Sleep0():
    return h1c.Sleep(0.0)


# ---------------------------------------------------------------------------
# Both fault servers carry the same two safety locks
# ---------------------------------------------------------------------------

_SERVERS = [pytest.param(H1FaultServer, h1s.ScenarioH1Server, id='h1'),
            pytest.param(H2FaultServer, h2s.ScenarioH2, id='h2')]


@pytest.mark.parametrize('server_cls,scenario_cls', _SERVERS)
@pytest.mark.parametrize('env,refused', [
    ({'BLACKBULL_ENV': 'production'}, True),
    ({'BLACKBULL_ENV': 'development', 'BB_PRODUCTION': '1'}, True),
    ({'BLACKBULL_ENV': 'development'}, False),
])
def test_a_fault_server_refuses_a_production_context(
        monkeypatch, server_cls, scenario_cls, env, refused):
    from blackbull.env import reset_settings_cache
    monkeypatch.delenv('BB_PRODUCTION', raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    reset_settings_cache()
    try:
        if refused:
            with pytest.raises(server_cls.Error, match='production'):
                server_cls(scenario_cls(steps=()))
        else:
            server_cls(scenario_cls(steps=()))
    finally:
        reset_settings_cache()


@pytest.mark.parametrize('server_cls,scenario_cls', _SERVERS)
def test_a_fault_server_binds_off_loopback_only_when_told_to(server_cls, scenario_cls):
    with pytest.raises(server_cls.Error, match='allow_remote'):
        server_cls(scenario_cls(steps=()), host='0.0.0.0')
    assert server_cls(scenario_cls(steps=()), host='0.0.0.0',
                      allow_remote=True).host == '0.0.0.0'


@pytest.mark.asyncio
async def test_a_graceful_close_after_the_client_reset_still_ends_the_scenario():
    import socket
    import struct

    scenario = h1s.ScenarioH1Server(steps=(
        h1s.WaitForRequest(timeout=2.0), h1s.Sleep(0.3), h1s.CloseGracefully()))
    async with H1FaultServer(scenario) as srv:
        sock = socket.create_connection(('127.0.0.1', srv.port))
        sock.sendall(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n')
        await asyncio.sleep(0.1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
        sock.close()  # RST
        await srv.wait_for_connection_done(timeout=5.0)
        result = srv.last_result
    assert result.terminated is True
    assert result.steps_completed[-1] == 'CloseGracefully'


@pytest.mark.parametrize('module, name, step', [
    (h1s, 'encode_status_line', h1s.SendStatusLine(code=200)),
    (h1s, 'encode_header', h1s.SendHeader('x-a', 'b')),
    (h1s, 'encode_chunk', h1s.SendChunk(b'abc')),
    (h1s, 'encode_chunked_terminator', h1s.EndChunkedBody()),
    (h2c, 'encode_frame', h2c.SendFrame(FrameTypes.PING, data=b'\x00' * 8)),
    (h2c, 'encode_headers', h2c.SendHeaders(raw_block=b'\x82')),
])
def test_the_encode_helpers_are_deprecated_spellings_of_wire(module, name, step):
    with pytest.warns(DeprecationWarning, match=r'wire\(\)') as caught:
        encode = getattr(module, name)
    assert caught[0].filename == __file__
    assert encode(step) == step.wire()


def _smuggle(scenario, step):
    # Past the constructor, which beartype guards, to reach the executor's own.
    object.__setattr__(scenario, 'steps', scenario.steps + (step,))
    return scenario


@pytest.mark.asyncio
@pytest.mark.parametrize('cell', ['h1c', 'h2c'])
async def test_a_client_refuses_a_foreign_step_before_any_byte(cell):
    vocabulary = {'h1c': (h1c.Scenario, HTTP1Client, {}),
                  'h2c': (h2c.ScenarioH2Client, HTTP2Client, {'scenario_mode': True})}
    scenario_cls, client_cls, kwargs = vocabulary[cell]
    scenario = _smuggle(scenario_cls(steps=(h1c.SendRawBytes(b'first'),)),
                        h1s.SendStatusLine(code=200))

    async def run(port, closed):
        async with client_cls('127.0.0.1', port, **kwargs) as client:
            with pytest.raises(TypeError, match='SendStatusLine'):
                await client.execute_scenario(scenario)

    _, got = await _against_a_raw_server(run, [])
    assert got == b''


@pytest.mark.asyncio
@pytest.mark.parametrize('cell', ['h1c', 'h2c'])
async def test_an_exited_client_refuses_a_scenario(cell):
    from blackbull.client import ConnectionError as ClientConnectionError
    vocabulary = {'h1c': (h1c.Scenario, HTTP1Client, {}),
                  'h2c': (h2c.ScenarioH2Client, HTTP2Client, {'scenario_mode': True})}
    scenario_cls, client_cls, kwargs = vocabulary[cell]

    async def run(port, closed):
        async with client_cls('127.0.0.1', port, **kwargs) as client:
            pass
        with pytest.raises(ClientConnectionError, match='context is closed'):
            await client.execute_scenario(
                scenario_cls(steps=(h1c.SendRawBytes(b'late'),)))

    await _against_a_raw_server(run, [])


@pytest.mark.parametrize('server_cls,scenario_cls', _SERVERS)
def test_a_fault_server_refuses_a_foreign_step_when_built(server_cls, scenario_cls):
    scenario = _smuggle(scenario_cls(steps=()), h2c.SendPreface())
    with pytest.raises(TypeError, match='SendPreface'):
        server_cls(scenario)


@pytest.mark.asyncio
async def test_every_headers_step_reaches_the_peer_as_one_decodable_frame():
    import hpack
    fields = ((':method', 'GET'), (':path', '/'), (':scheme', 'http'),
              (':authority', 'a'))
    steps = (
        h2c.SendPreface(),
        h2c.SendHeaders(pseudo=fields, headers=(('x-a', '1'),), stream_id=1,
                        end_stream=True),
        h2c.SendHeaders(pseudo=fields, headers=(('x-a', '1'),), stream_id=3,
                        end_stream=True),
        h2c.SendHeaders(stream_id=5),
        h2c.SendHeaders(raw_block=b'\x82', stream_id=7, end_headers=False),
        h2c.SendHeaders(raw_block=b'\x82\x86', stream_id=9, declared_length=99),
    )
    result, got = await _h2c(steps, [])
    assert result.exception is None and result.steps_completed == len(steps)

    rest, frames = got[len(CLIENT_PREFACE):], []
    for _ in steps[1:-1]:
        length = int.from_bytes(rest[:3], 'big')
        frames.append((rest[3], rest[4], int.from_bytes(rest[5:9], 'big'),
                       rest[9:9 + length]))
        rest = rest[9 + length:]
    assert [f[:3] for f in frames] == [(1, 0x05, 1), (1, 0x05, 3), (1, 0x04, 5),
                                       (1, 0x00, 7)]
    peer = hpack.Decoder()  # one per connection, as on any real peer
    expected = [(n.encode(), v.encode()) for n, v in fields + (('x-a', '1'),)]
    assert peer.decode(frames[0][3], raw=True) == expected
    assert peer.decode(frames[1][3], raw=True) == expected
    assert peer.decode(frames[2][3]) == []
    assert frames[3][3] == b'\x82'
    assert rest[:9] == (99).to_bytes(3, 'big') + b'\x01\x04' + (9).to_bytes(4, 'big')
    assert rest[9:] == b'\x82\x86'


# ---------------------------------------------------------------------------
# A stopped fault server leaves nothing behind
# ---------------------------------------------------------------------------

_H2_HELLO = CLIENT_PREFACE + _SETTINGS

#: id -> (server, its scenario, what the client sends): the connection is
#: parked in that state when the server stops.
_PARKED = {
    'h1-reading-the-head': (H1FaultServer, h1s.ScenarioH1Server(
        steps=(h1s.WaitForRequest(timeout=30.0),)), b''),
    'h1-sleeping': (H1FaultServer, h1s.ScenarioH1Server(
        steps=(h1s.Sleep(30.0),)), b''),
    'h1-mid-paced-write': (H1FaultServer, h1s.ScenarioH1Server(
        steps=(h1s.SendRawBytes(b'x' * 100, byte_interval=0.5),)), b''),
    'h1-finished': (H1FaultServer, h1s.ScenarioH1Server(steps=()), b''),
    'h2-awaiting-the-preface': (H2FaultServer, h2s.ScenarioH2(
        steps=(h2s.Sleep(30.0),)), b''),
    'h2-waiting-for-a-frame': (H2FaultServer, h2s.ScenarioH2(
        steps=(h2s.WaitForClientFrame(match={'type': 'PING'}, timeout=30.0),)),
        _H2_HELLO),
    'h2-sleeping': (H2FaultServer, h2s.ScenarioH2(steps=(h2s.Sleep(30.0),)),
                    _H2_HELLO),
    'h2-failed': (H2FaultServer, h2s.ScenarioH2(steps=(h2s.Sleep(30.0),)),
                  b'not a preface' + b'.' * 11),
}


async def _connect(srv, hello: bytes, n: int = 1):
    peers = [await asyncio.open_connection('127.0.0.1', srv.port) for _ in range(n)]
    for _, writer in peers:
        writer.write(hello)
        await writer.drain()
    await asyncio.sleep(0.2)  # each handler reaches the parked state
    return peers


async def _stop_and_check(srv, peers, before):
    t0 = asyncio.get_running_loop().time()
    await asyncio.wait_for(srv.stop(), timeout=5.0)
    assert asyncio.get_running_loop().time() - t0 < 2.0
    assert asyncio.all_tasks() - before == set(), 'a task outlived stop()'
    for reader, writer in peers:
        await _read_to_eof(reader)  # the peer is told the connection is over
        writer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('parked', sorted(_PARKED))
async def test_stopping_a_fault_server_leaves_no_task_and_ends_the_connection(parked):
    server_cls, scenario, hello = _PARKED[parked]
    before = asyncio.all_tasks()
    srv = server_cls(scenario)
    await srv.start()
    peers = await _connect(srv, hello)
    await _stop_and_check(srv, peers, before)
    assert srv.last_result is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('server_cls,scenario_cls', _SERVERS)
async def test_stopping_ends_every_connection_at_once(server_cls, scenario_cls):
    before = asyncio.all_tasks()
    srv = server_cls(scenario_cls(steps=(h1c.Sleep(30.0),)))
    await srv.start()
    hello = _H2_HELLO if server_cls is H2FaultServer else b''
    await _stop_and_check(srv, await _connect(srv, hello, n=3), before)


@pytest.mark.asyncio
@pytest.mark.parametrize('server_cls,scenario_cls', _SERVERS)
async def test_a_restarted_fault_server_reports_only_its_own_connections(
        server_cls, scenario_cls):
    hello = _H2_HELLO if server_cls is H2FaultServer else b''
    srv = server_cls(scenario_cls(steps=()))
    await srv.start()
    peers = await _connect(srv, hello)
    await srv.wait_for_connection_done(timeout=2.0)
    first = srv.last_result
    await srv.stop()

    await srv.start()
    assert srv.last_result is None
    with pytest.raises(TimeoutError):
        await srv.wait_for_connection_done(timeout=0.2)
    peers += await _connect(srv, hello)
    await srv.wait_for_connection_done(timeout=2.0)
    assert srv.last_result is not None and srv.last_result is not first
    await srv.stop()
    for _, writer in peers:
        writer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('server_cls,scenario_cls', _SERVERS)
async def test_no_accepted_connection_outlives_stop(server_cls, scenario_cls):
    import gc
    for yields in range(8):
        srv = server_cls(scenario_cls(steps=(h1c.Sleep(30.0),)))
        await srv.start()
        port = srv.port
        connects = [asyncio.ensure_future(asyncio.open_connection('127.0.0.1', port))
                    for _ in range(10)]
        for _ in range(yields):
            await asyncio.sleep(0)
        await srv.stop()
        peers = [c for c in await asyncio.gather(*connects, return_exceptions=True)
                 if not isinstance(c, OSError)]  # refused once the listener closed
        await asyncio.sleep(0.1)
        open_server_side = [
            t for t in gc.get_objects()
            if isinstance(t, asyncio.Transport) and not t.is_closing()
            and (t.get_extra_info('sockname') or ('', 0))[1] == port]
        for _, writer in peers:
            writer.close()
        assert open_server_side == [], f'after {yields} yields'


def test_a_headers_block_needs_nothing_from_the_peers_table():
    import hpack
    step = h2c.SendHeaders(pseudo=((':method', 'GET'), (':scheme', 'http'),
                                   (':path', '/'), (':authority', 'a.example'),
                                   (':authority', 'a.example')))
    block = step.wire()[9:]
    for size in (0, 64, 4096):
        peer = hpack.Decoder()
        peer.header_table_size = size
        assert [n for n, _ in peer.decode(block)].count(':authority') == 2
        assert len(peer.header_table.dynamic_entries) == 0


def test_a_match_keeps_its_tuples_and_lists_through_json():
    scenario = h2s.ScenarioH2(steps=(
        h2s.WaitForClientFrame(match={'flags_set': ('ACK',)}),
        h2s.ExpectClientFrame(match={'flags_unset': ['END_STREAM']})))
    assert h2s.scenario_from_json(h2s.scenario_to_json(scenario)) == scenario


@pytest.mark.parametrize('match', ['{"x": {"hex": "zz"}}', '[1, 2]', '{"x": {"hex": 5}}'])
def test_a_malformed_field_is_refused_by_name(match):
    with pytest.raises(ValueError, match='ExpectResponse.match'):
        h1c.Scenario.from_json('{"op": "HEADER", "format": 2, "name": ""}\n'
                               f'{{"op": "ExpectResponse", "match": {match}}}')


@pytest.mark.asyncio
@pytest.mark.parametrize('server_cls,scenario_cls', _SERVERS)
async def test_a_fault_server_refuses_a_foreign_step_when_started(server_cls, scenario_cls):
    srv = server_cls(scenario_cls(steps=()))
    srv.scenario = _smuggle(scenario_cls(steps=()), h2c.SendPreface())
    with pytest.raises(TypeError, match='SendPreface'):
        await srv.start()

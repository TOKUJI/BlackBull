"""`match` on the HTTP/1.1 server side — a filter and a guard.

The consistency sweep found `WaitForRequest` had no `match` grammar where
`WaitForClientFrame` does.  Porting it literally would have changed what
the word means: on HTTP/2 the executor skips non-matching frames and keeps
waiting, which is harmless because streams are independent.  On HTTP/1.1
requests are positional (RFC 9112 §9.3), so a skipped head is one the
scenario can never answer — everything after it is off by one.

So there are two steps, because there are two questions:

* `WaitForRequest(match=)` — *which* request do I want to break?  Filters a
  pipeline, skips what it passes over, and records the skips because they
  desync the connection.
* `ExpectRequest(match=)` — *is the client behaving as I assumed?*  Reads
  one head, skips nothing, records the verdict.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.fault_injection import (
    ExpectRequest, H1FaultServer, H1SEndHeaders, H1SSendStatusLine,
    ScenarioH1Server, WaitForRequest, request_matches,
)

pytestmark = pytest.mark.asyncio

_HEAD = (b'POST /submit HTTP/1.1\r\nHost: a\r\n'
         b'Expect: 100-continue\r\n\r\n')


async def _send(host, port, *requests: bytes) -> bytes:
    reader, writer = await asyncio.open_connection(host, port)
    for r in requests:
        writer.write(r)
    await writer.drain()
    try:
        return await asyncio.wait_for(reader.read(4096), timeout=2.0)
    except (asyncio.TimeoutError, ConnectionError):
        return b''
    finally:
        writer.close()


class TestTheGrammar:
    async def test_the_recognised_keys(self):
        assert request_matches(_HEAD, {'method': 'POST'})
        assert request_matches(_HEAD, {'target': '/submit'})
        assert request_matches(_HEAD, {'version': 'HTTP/1.1'})
        assert request_matches(_HEAD, {'header': ('expect', '100-continue')})
        assert request_matches(_HEAD, {'header': ('host', None)})
        assert request_matches(_HEAD, {'header_absent': 'x-nope'})

    async def test_an_empty_match_matches_anything(self):
        """The pre-existing `WaitForRequest()` keeps its meaning."""
        assert request_matches(_HEAD, {})
        assert request_matches(b'nonsense\r\n\r\n', {})

    async def test_an_unknown_key_fails_closed(self):
        """A typo must not silently match — the rule `frame_matches` set."""
        assert not request_matches(_HEAD, {'methd': 'POST'})
        assert not request_matches(_HEAD, {'method': 'POST', 'oops': 1})

    async def test_a_malformed_request_line_does_not_raise(self):
        """A scenario may well be waiting for exactly that."""
        assert not request_matches(b'\r\n\r\n', {'method': 'GET'})


class TestWaitForRequestFilters:
    async def test_it_skips_until_a_head_matches(self):
        scenario = ScenarioH1Server(name='break_on_post', steps=(
            WaitForRequest(match={'method': 'POST'}, timeout=2.0),
            H1SSendStatusLine(code=500), H1SEndHeaders(),
        ))
        async with H1FaultServer(scenario) as srv:
            body = await _send(
                srv.host, srv.port,
                b'GET /a HTTP/1.1\r\nHost: a\r\n\r\n',
                b'GET /b HTTP/1.1\r\nHost: a\r\n\r\n',
                b'POST /c HTTP/1.1\r\nHost: a\r\n\r\n')
            await srv.wait_for_connection_done(timeout=5.0)
            result = srv.last_result

        assert body.startswith(b'HTTP/1.1 500')
        assert result.wait_skipped == 2, (
            'the two GETs before the POST were not counted as skipped')
        assert result.request_head.startswith(b'POST /c')

    async def test_skipping_is_reported_because_it_desyncs(self):
        """Non-zero `requests_skipped` is the connection being off by one.

        On HTTP/2 the same skip is harmless; here it is a fault in its own
        right, and the whole reason this count exists.
        """
        scenario = ScenarioH1Server(steps=(
            WaitForRequest(match={'method': 'DELETE'}, timeout=0.4),
        ))
        async with H1FaultServer(scenario) as srv:
            await _send(srv.host, srv.port,
                        b'GET /a HTTP/1.1\r\nHost: a\r\n\r\n')
            await srv.wait_for_connection_done(timeout=5.0)
            result = srv.last_result

        assert result.wait_skipped == 1
        assert result.wait_timed_out is True, (
            'the match never arrived, so the step must report the miss')

    async def test_no_match_behaves_as_before(self):
        scenario = ScenarioH1Server(steps=(
            WaitForRequest(timeout=2.0),
            H1SSendStatusLine(code=204), H1SEndHeaders(),
        ))
        async with H1FaultServer(scenario) as srv:
            body = await _send(srv.host, srv.port,
                               b'GET /a HTTP/1.1\r\nHost: a\r\n\r\n')
            await srv.wait_for_connection_done(timeout=5.0)
            result = srv.last_result

        assert body.startswith(b'HTTP/1.1 204')
        assert result.wait_skipped == 0


class TestExpectRequestGuards:
    async def test_it_records_a_held_expectation(self):
        scenario = ScenarioH1Server(steps=(
            ExpectRequest(match={'header': ('expect', '100-continue')},
                          timeout=2.0),
            H1SSendStatusLine(code=100), H1SEndHeaders(),
        ))
        async with H1FaultServer(scenario) as srv:
            await _send(srv.host, srv.port, _HEAD)
            await srv.wait_for_connection_done(timeout=5.0)
            result = srv.last_result

        assert result.expectations == [
            ({'header': ('expect', '100-continue')}, True)]
        assert result.wait_skipped == 0

    async def test_a_broken_expectation_is_recorded_not_raised(self):
        """The run would otherwise look like a pass while testing nothing."""
        scenario = ScenarioH1Server(steps=(
            ExpectRequest(match={'header': ('expect', '100-continue')},
                          timeout=2.0),
            H1SSendStatusLine(code=100), H1SEndHeaders(),
        ))
        async with H1FaultServer(scenario) as srv:
            await _send(srv.host, srv.port,
                        b'GET /a HTTP/1.1\r\nHost: a\r\n\r\n')
            await srv.wait_for_connection_done(timeout=5.0)
            result = srv.last_result

        assert result.expectations[0][1] is False
        assert result.exception is None, 'a broken expectation must not raise'

    async def test_it_skips_nothing(self):
        """The difference from WaitForRequest, asserted rather than described."""
        scenario = ScenarioH1Server(steps=(
            ExpectRequest(match={'method': 'DELETE'}, timeout=2.0),
        ))
        async with H1FaultServer(scenario) as srv:
            await _send(srv.host, srv.port,
                        b'GET /a HTTP/1.1\r\nHost: a\r\n\r\n')
            await srv.wait_for_connection_done(timeout=5.0)
            result = srv.last_result

        assert result.wait_skipped == 0
        assert result.expectations == [({'method': 'DELETE'}, False)]


class TestSerialisation:
    async def test_both_steps_round_trip(self):
        from blackbull.fault_injection import (
            scenario_h1_server_from_json, scenario_h1_server_to_json,
        )
        scenario = ScenarioH1Server(name='both', steps=(
            WaitForRequest(match={'method': 'POST'}, timeout=1.0),
            ExpectRequest(match={'header': ('x', 'y')}, timeout=2.0),
        ))
        assert scenario_h1_server_from_json(
            scenario_h1_server_to_json(scenario)) == scenario

"""The breaking side must be able to assert what the peer did.

Sprint 108 drove all four fault-injection cells against counterparts that
are not BlackBull and found the grid filled — and the *observation* half
asymmetric:

    broken server (B, C):  WaitForRequest / WaitForClientFrame
                           ExpectRequest  / ExpectClientFrame
                           + expectations, client_bytes_received, wait_skipped

    broken client (A, D):  one `response` slot.  Nothing else.

So a cell-D scenario could send an illegal frame and could not observe the
GOAWAY it drew, because `ReadResponse` reads *one* frame and the first
frame any correct server sends is its handshake SETTINGS.  Cell D could
answer "did the server survive?" and not "what did the server decide?".

This file pins the role-axis twins.  The naming is deliberate: the four
cells now run one scheme (`WaitFor…` filters and skips, `Expect…` guards
and never skips), so a reader comparing any two files is not told they
differ where they do not.
"""
from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.asyncio


def _echo_app():
    from blackbull import BlackBull, read_body

    app = BlackBull()

    @app.route(path='/')
    async def _root(scope, receive, send):
        await read_body(receive)
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'text/plain')]})
        await send({'type': 'http.response.body', 'body': b'ok'})

    return app


class TestTheFourCellsRunOneScheme:
    """The consistency rule, as a test rather than a habit."""

    def test_each_role_has_a_wait_step_and_a_guard_step(self):
        from blackbull.fault_injection import (
            scenario_h1, scenario_h1_server, scenario_h2, scenario_h2_client,
        )

        expected = {
            scenario_h1:        ('WaitForResponse', 'ExpectResponse'),
            scenario_h2_client: ('WaitForServerFrame', 'ExpectServerFrame'),
            scenario_h1_server: ('WaitForRequest', 'ExpectRequest'),
            scenario_h2:        ('WaitForClientFrame', 'ExpectClientFrame'),
        }
        missing = [f'{m.__name__.split(".")[-1]}.{n}'
                   for m, names in expected.items() for n in names
                   if not hasattr(m, n)]
        assert missing == [], f'missing: {missing}'

    def test_the_client_side_results_gained_the_server_sides_fields(self):
        """Same names, both roles — `expectations` is `expectations`."""
        from blackbull.fault_injection.scenario_h1 import ScenarioResult
        from blackbull.fault_injection.scenario_h1_server import (
            ScenarioH1ServerResult)
        from blackbull.fault_injection.scenario_h2 import ScenarioH2Result
        from blackbull.fault_injection.scenario_h2_client import (
            ScenarioH2ClientResult)

        shared = {'expectations', 'wait_skipped', 'wait_timed_out',
                  'half_closed'}
        for cls in (ScenarioResult, ScenarioH2ClientResult,
                    ScenarioH1ServerResult, ScenarioH2Result):
            assert shared <= set(cls.__dataclass_fields__), (
                f'{cls.__name__} lacks {shared - set(cls.__dataclass_fields__)}')

        # The peer-traffic counter is named for whoever the peer *is*.
        assert 'server_bytes_received' in ScenarioResult.__dataclass_fields__
        assert 'server_bytes_received' in \
            ScenarioH2ClientResult.__dataclass_fields__
        assert 'client_bytes_received' in \
            ScenarioH1ServerResult.__dataclass_fields__


class TestCellDCanObserveAVerdict:
    """The finding that motivated the sprint, closed."""

    async def test_an_illegal_frame_draws_an_observable_goaway(self):
        """Without knowing how many frames precede it.

        This is the exact scenario that reported success for a sprint
        while measuring nothing.
        """
        from blackbull.client.http2 import HTTP2Client
        from blackbull.fault_injection.scenario_h2_client import (
            ScenarioH2Client, SendFrame, SendPreface, WaitForServerFrame,
        )
        from blackbull.protocol.frame_types import FrameTypes
        from blackbull.testing.native import NativeTestServer

        async with NativeTestServer(_echo_app()) as server:
            async with HTTP2Client('127.0.0.1', server.port,
                                   scenario_mode=True) as client:
                result = await asyncio.wait_for(client.execute_scenario(
                    ScenarioH2Client(steps=(
                        SendPreface(),
                        # SETTINGS with ACK set *and* a payload — RFC 9113
                        # §6.5 makes this a FRAME_SIZE_ERROR.
                        SendFrame(FrameTypes.SETTINGS, flags=0x1, stream_id=0,
                                  data=b'\x00' * 6),
                        WaitForServerFrame(match={'type': 'GOAWAY'},
                                           timeout=5.0)))), timeout=10.0)

        assert result.exception is None, result.exception
        assert result.response is not None, (
            'no GOAWAY observed — the handshake SETTINGS is not a verdict')
        assert type(result.response).__name__ == 'GoAway'
        assert result.wait_skipped > 0, (
            'the SETTINGS ahead of the GOAWAY should have been skipped over')

    async def test_the_whole_frame_sequence_is_recoverable(self):
        """`received` keeps every frame, not only the last one read."""
        from blackbull.client.http2 import HTTP2Client
        from blackbull.fault_injection.scenario_h2_client import (
            ReadResponse, ScenarioH2Client, SendFrame, SendPreface,
        )
        from blackbull.protocol.frame_types import FrameTypes
        from blackbull.testing.native import NativeTestServer

        async with NativeTestServer(_echo_app()) as server:
            async with HTTP2Client('127.0.0.1', server.port,
                                   scenario_mode=True) as client:
                result = await asyncio.wait_for(client.execute_scenario(
                    ScenarioH2Client(steps=(
                        SendPreface(),
                        # An empty SETTINGS, so the peer owes us two frames:
                        # its own SETTINGS and the ACK for ours.
                        SendFrame(FrameTypes.SETTINGS, flags=0, stream_id=0,
                                  data=b''),
                        ReadResponse(timeout=3.0),
                        ReadResponse(timeout=3.0)))), timeout=10.0)

        assert len(result.received) == 2, (
            f'two reads, {len(result.received)} recorded — the second '
            f'overwrote the first')
        assert result.response is result.received[-1], (
            '`response` must stay the most recent read, for back-compat')
        assert result.server_bytes_received > 0

    async def test_expect_server_frame_records_a_failed_premise(self):
        """A guard, not a filter: nothing skipped, verdict recorded.

        A scenario whose premise silently failed would otherwise look
        like a pass — the reason `ExpectRequest` exists on the other role.
        """
        from blackbull.client.http2 import HTTP2Client
        from blackbull.fault_injection.scenario_h2_client import (
            ExpectServerFrame, ScenarioH2Client, SendPreface,
        )
        from blackbull.testing.native import NativeTestServer

        async with NativeTestServer(_echo_app()) as server:
            async with HTTP2Client('127.0.0.1', server.port,
                                   scenario_mode=True) as client:
                result = await asyncio.wait_for(client.execute_scenario(
                    ScenarioH2Client(steps=(
                        SendPreface(),
                        # The first frame is SETTINGS, never GOAWAY.
                        ExpectServerFrame(match={'type': 'GOAWAY'},
                                          timeout=3.0)))), timeout=10.0)

        assert result.expectations == [({'type': 'GOAWAY'}, False)]
        assert result.wait_skipped == 0, 'a guard must not skip'


class TestCellACanObserveAPair:
    """Pipelining: two responses, both checkable."""

    async def test_two_pipelined_responses_are_both_recorded(self):
        from blackbull.client.http1 import HTTP1Client
        from blackbull.fault_injection.scenario_h1 import (
            ReadResponse, Scenario, SendRawBytes,
        )
        from blackbull.testing.native import NativeTestServer

        async with NativeTestServer(_echo_app()) as server:
            async with HTTP1Client('127.0.0.1', server.port) as client:
                result = await asyncio.wait_for(client.execute_scenario(
                    Scenario(steps=(
                        SendRawBytes(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n'
                                     b'GET / HTTP/1.1\r\nHost: x\r\n\r\n'),
                        ReadResponse(timeout=5.0),
                        ReadResponse(timeout=5.0)))), timeout=10.0)

        assert result.exception is None, result.exception
        assert len(result.received) == 2, (
            f'{len(result.received)} of 2 responses survived')
        assert [r.status for r in result.received] == [200, 200]
        assert result.server_bytes_received > 0

    async def test_wait_for_response_skips_until_a_match(self):
        """The h1 twin of `WaitForServerFrame`, on a pipelined pair."""
        from blackbull import BlackBull, read_body
        from blackbull.client.http1 import HTTP1Client
        from blackbull.fault_injection.scenario_h1 import (
            Scenario, SendRawBytes, WaitForResponse,
        )
        from blackbull.testing.native import NativeTestServer

        app = BlackBull()

        @app.route(path='/ok')
        async def _ok(scope, receive, send):
            await read_body(receive)
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})

        async with NativeTestServer(app) as server:
            async with HTTP1Client('127.0.0.1', server.port) as client:
                result = await asyncio.wait_for(client.execute_scenario(
                    Scenario(steps=(
                        # A 404 first, then the 200 the scenario wants.
                        SendRawBytes(b'GET /nope HTTP/1.1\r\nHost: x\r\n\r\n'
                                     b'GET /ok HTTP/1.1\r\nHost: x\r\n\r\n'),
                        WaitForResponse(match={'status': 200},
                                        timeout=5.0)))), timeout=10.0)

        assert result.response is not None, 'never reached the 200'
        assert result.response.status == 200
        assert result.wait_skipped == 1, (
            'the 404 should have been skipped over and counted')

    async def test_expect_response_records_the_verdict_either_way(self):
        from blackbull.client.http1 import HTTP1Client
        from blackbull.fault_injection.scenario_h1 import (
            ExpectResponse, Scenario, SendRawBytes,
        )
        from blackbull.testing.native import NativeTestServer

        async with NativeTestServer(_echo_app()) as server:
            async with HTTP1Client('127.0.0.1', server.port) as client:
                good = await asyncio.wait_for(client.execute_scenario(
                    Scenario(steps=(
                        SendRawBytes(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n'),
                        ExpectResponse(match={'status': 200},
                                       timeout=5.0)))), timeout=10.0)
            async with HTTP1Client('127.0.0.1', server.port) as client:
                bad = await asyncio.wait_for(client.execute_scenario(
                    Scenario(steps=(
                        SendRawBytes(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n'),
                        ExpectResponse(match={'status': 500},
                                       timeout=5.0)))), timeout=10.0)

        assert good.expectations == [({'status': 200}, True)]
        assert bad.expectations == [({'status': 500}, False)]


class TestTheMatchersFailClosed:
    """An unrecognised key is a typo, and a typo must not silently match.

    Both existing matchers made this choice; the two new ones inherit it
    rather than re-deciding it.
    """

    def test_an_unknown_key_never_matches(self):
        from blackbull.fault_injection.scenario_h1 import response_matches
        from blackbull.fault_injection.scenario_h2 import frame_matches

        class _Resp:
            status = 200
            headers = ()
            reason = 'OK'
            version = 'HTTP/1.1'

        assert response_matches(_Resp(), {'status': 200}) is True
        assert response_matches(_Resp(), {'stauts': 200}) is False
        assert frame_matches(object(), {'nonsense': 1}) is False

    def test_the_h2_matcher_can_read_an_error_code(self):
        """Without it, cell D can see *a* GOAWAY but not *which* GOAWAY."""
        from blackbull.fault_injection.scenario_h2 import frame_matches
        from blackbull.protocol.frame_types import FrameTypes, GoAway

        frame = GoAway(8, FrameTypes.GOAWAY, 0, 0,
                       data=(0).to_bytes(4, 'big') + (6).to_bytes(4, 'big'))
        assert frame_matches(frame, {'type': 'GOAWAY', 'error_code': 6}) is True
        assert frame_matches(frame, {'type': 'GOAWAY', 'error_code': 1}) is False

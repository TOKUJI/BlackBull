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

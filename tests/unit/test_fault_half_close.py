"""`HalfClose` — the fault no cell could express.

Four vocabularies shipped `Abort` (RST) and, on the server side,
`CloseGracefully` (a full close).  Neither is a half-close: shutting down
*one* direction while still reading the other.

That is not a stylistic third option.  A peer that sends FIN and keeps
reading is a peer that has finished its request and is waiting for the
response — the ordinary end of a non-keep-alive exchange, and the shape of
several real client bugs.  A test that reaches for `Abort` to stand in for
it is testing reset handling, which is a different code path with a
different answer: RST discards buffered data, FIN does not.

The four-cell differential of Sprint 108 found this missing in **all four
cells at once**, which is what makes it a role-axis finding rather than a
protocol one — the gap is the same on both protocols and in both roles.
"""
from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.asyncio


class TestTheVocabulariesAgree:
    """One name, four vocabularies — the Sprint 108 consistency rule.

    A step added to three of the four recreates exactly the asymmetry that
    sprint spent a day finding, so the sweep is a test rather than a habit.
    """

    def test_every_scenario_vocabulary_has_half_close(self):
        from blackbull.fault_injection import (
            scenario_h1, scenario_h1_server, scenario_h2, scenario_h2_client,
        )

        missing = [m.__name__ for m in (scenario_h1, scenario_h1_server,
                                        scenario_h2, scenario_h2_client)
                   if not hasattr(m, 'HalfClose')]
        assert missing == [], f'HalfClose missing from: {missing}'

    def test_half_close_is_in_every_step_union(self):
        """Declared in `Step`, or the executors type-check without it."""
        from blackbull.fault_injection import (
            scenario_h1, scenario_h1_server, scenario_h2, scenario_h2_client,
        )

        for mod in (scenario_h1, scenario_h1_server, scenario_h2,
                    scenario_h2_client):
            assert 'HalfClose' in str(mod.Step), f'{mod.__name__}.Step'

    def test_the_package_exports_it_role_qualified(self):
        """Imported the way the docs tell a reader to, all four resolve."""
        from blackbull import fault_injection as fi

        for name in ('SendRawBytes', 'H1SSendRawBytes', 'H2CSendRawBytes'):
            assert hasattr(fi, name), name
        for name in ('HalfClose', 'H1SHalfClose', 'H2CHalfClose',
                     'H2SHalfClose'):
            assert hasattr(fi, name), name


class TestItIsAFinNotAReset:
    """The distinction the step exists for, asserted on the wire."""

    async def test_the_client_half_close_lets_the_response_still_arrive(self):
        """FIN on the write side; the peer's answer must still be read.

        This is the case `Abort` cannot express: after a reset there is no
        response to read, so a test written with `Abort` would pass for the
        wrong reason.
        """
        from blackbull import BlackBull, read_body
        from blackbull.client.http1 import HTTP1Client
        from blackbull.fault_injection.scenario_h1 import (
            HalfClose, ReadResponse, Scenario, SendRawBytes,
        )
        from blackbull.testing.native import NativeTestServer

        app = BlackBull()

        @app.route(path='/')
        async def _root(scope, receive, send):
            await read_body(receive)
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(b'content-type', b'text/plain')]})
            await send({'type': 'http.response.body', 'body': b'ok'})

        async with NativeTestServer(app) as server:
            async with HTTP1Client('127.0.0.1', server.port) as client:
                result = await asyncio.wait_for(client.execute_scenario(
                    Scenario(steps=(
                        SendRawBytes(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n'),
                        HalfClose(),
                        ReadResponse(timeout=5.0)))), timeout=10.0)

        assert result.exception is None, result.exception
        assert result.response is not None, (
            'the response never arrived — a half-close closed the read side too')
        assert result.response.status == 200

    async def test_half_close_is_not_terminal_but_abort_is(self):
        """`Abort` short-circuits; `HalfClose` must not.

        Half of the point is that steps keep running afterwards — the
        scenario is still reading.
        """
        from blackbull import BlackBull, read_body
        from blackbull.client.http1 import HTTP1Client
        from blackbull.fault_injection.scenario_h1 import (
            Abort, HalfClose, Scenario, SendRawBytes,
        )
        from blackbull.testing.native import NativeTestServer

        app = BlackBull()

        @app.route(path='/')
        async def _root(scope, receive, send):
            await read_body(receive)
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})

        req = SendRawBytes(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n')
        async with NativeTestServer(app) as server:
            async with HTTP1Client('127.0.0.1', server.port) as c:
                after_half = await asyncio.wait_for(c.execute_scenario(
                    Scenario(steps=(req, HalfClose(), Sleep0()))), timeout=10.0)
            async with HTTP1Client('127.0.0.1', server.port) as c:
                after_abort = await asyncio.wait_for(c.execute_scenario(
                    Scenario(steps=(req, Abort(), Sleep0()))), timeout=10.0)

        # A terminal step returns before it is counted, so `Abort` stops
        # the walk at the one step that preceded it.  `HalfClose` counts
        # itself and the step after it — that difference *is* the
        # behaviour under test.
        assert after_half.steps_completed == 3, 'HalfClose must not be terminal'
        assert after_half.aborted is False
        assert after_half.half_closed is True
        assert after_abort.steps_completed == 1, 'Abort must stay terminal'
        assert after_abort.aborted is True


def Sleep0():
    from blackbull.fault_injection.scenario_h1 import Sleep
    return Sleep(0.0)


class TestTheServerSideHalfCloses:
    """The same step, driven from the breaking *server* — cells B and C."""

    async def test_h1_fault_server_half_closes_after_a_partial_response(self):
        """FIN mid-body: the client sees a truncated body, not a reset.

        `CloseGracefully` closes both directions at once; this leaves the
        read side open, which is how a real server that has finished
        writing but not finished reading behaves.
        """
        import httpx

        from blackbull.fault_injection.h1_server import H1FaultServer
        from blackbull.fault_injection.scenario_h1_server import (
            EndHeaders, HalfClose, ScenarioH1Server, SendHeader,
            SendStatusLine, WaitForRequest,
        )

        scenario = ScenarioH1Server(steps=(
            WaitForRequest(timeout=5.0),
            SendStatusLine(code=200, reason='OK'),
            SendHeader('content-length', '100'),
            EndHeaders(),
            HalfClose(),
        ))

        async with H1FaultServer(scenario) as srv:
            async with httpx.AsyncClient(timeout=5.0) as c:
                with pytest.raises(httpx.HTTPError):
                    await c.get(f'http://127.0.0.1:{srv.port}/')

        assert srv.last_result is not None, srv.last_result
        assert srv.last_result.exception is None, srv.last_result.exception
        assert srv.last_result.half_closed is True, (
            'the transport refused the half-close')
        assert srv.last_result.terminated is True, (
            'a half-close ends the scenario for the server side')

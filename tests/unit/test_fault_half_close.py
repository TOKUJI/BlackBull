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
        """FIN mid-body: the bytes on the wire, asserted directly.

        A raw socket, because the claim here is about *our* output — the
        headers went out and FIN followed them — and `reader.read(-1)`
        returning at EOF proves both halves in one call.

        This does **not** replace driving a real client at the same fault.
        The broken-server cells are verified against implementations that
        are not ours, and that rule applies to this step like any other:
        `half_closed_after_headers` is in the HTTP/1.1 and HTTP/2 server
        catalogues, and
        `tests/conformance/fault_injection/test_four_cell_differential.py`
        drives httpx and curl at it.  This test is the wire-level
        complement, not a substitute — the earlier version of this file
        had only the client-driven half, and only the raw-socket half
        would have located the leak that made it hang.
        """
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
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection('127.0.0.1', srv.port), timeout=5.0)
            try:
                writer.write(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n')
                await writer.drain()
                # read() returns b'' at EOF, so this both proves the
                # headers arrived and that FIN followed them.
                data = await asyncio.wait_for(reader.read(-1), timeout=10.0)
            finally:
                writer.close()

        assert data.startswith(b'HTTP/1.1 200 OK\r\n'), data[:60]
        assert b'content-length: 100' in data.lower()
        assert b'\r\n\r\n' in data, 'headers never ended'
        assert data.split(b'\r\n\r\n', 1)[1] == b'', (
            'the server sent body bytes it declared it would not')

        assert srv.last_result is not None
        assert srv.last_result.exception is None, srv.last_result.exception
        assert srv.last_result.half_closed is True, (
            'the transport refused the half-close')


class TestHalfCloseIsNeverTerminal:
    """The role-axis rule, as a test.

    The first cut of this step made `HalfClose` terminal on the two server
    cells and non-terminal on the two client cells — the exact asymmetry
    this sprint exists to remove, shipped by the sprint removing it.  It
    also leaked: a terminal step returns from the connection handler
    without closing anything, so the half-open socket survived until
    teardown reaped it.

    CI caught it as a timeout on one Python version.  This catches it as a
    statement about all four.
    """

    async def test_a_step_after_half_close_still_runs_on_the_h1_server(self):
        """Asserted as behaviour: the step after it executed.

        `Abort` and `CloseGracefully` end a scenario; `HalfClose` must not,
        because the connection is still readable — that *is* the difference
        between this step and the other two.
        """
        from blackbull.fault_injection.h1_server import H1FaultServer
        from blackbull.fault_injection.scenario_h1_server import (
            EndHeaders, HalfClose, ScenarioH1Server, SendStatusLine, Sleep,
            WaitForRequest,
        )

        async with H1FaultServer(ScenarioH1Server(steps=(
                WaitForRequest(timeout=5.0),
                SendStatusLine(code=204, reason='No Content'),
                EndHeaders(),
                HalfClose(),
                Sleep(0.0),          # must run
        ))) as srv:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection('127.0.0.1', srv.port), timeout=5.0)
            try:
                writer.write(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n')
                await writer.drain()
                await asyncio.wait_for(reader.read(-1), timeout=10.0)
            finally:
                writer.close()
            result = srv.last_result

        assert result is not None
        assert result.steps_completed[-1] == 'Sleep', (
            f'the step after HalfClose never ran: {result.steps_completed}')
        assert result.terminated is False

    async def test_a_step_after_half_close_still_runs_on_the_h2_server(self):
        """The HTTP/2 twin — the asymmetry was in both server halves."""
        from blackbull.fault_injection.h2_server import H2FaultServer
        from blackbull.fault_injection.scenario_h2 import (
            HalfClose, ScenarioH2, Sleep,
        )

        async with H2FaultServer(ScenarioH2(steps=(
                HalfClose(),
                Sleep(0.0),          # must run
        ))) as srv:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection('127.0.0.1', srv.port), timeout=5.0)
            try:
                writer.write(b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n')
                await writer.drain()
                await asyncio.wait_for(reader.read(-1), timeout=10.0)
            finally:
                writer.close()
            result = srv.last_result

        assert result is not None, 'the scenario never ran'
        assert result.half_closed is True
        assert result.terminated is False, (
            'HalfClose ended the HTTP/2 scenario instead of continuing it')

    async def test_the_server_releases_the_socket_when_the_scenario_ends(self):
        """A half-closed scenario must still finish and close.

        The leak the terminal-step bug caused: FIN went out, the handler
        returned, and nothing closed the read side.
        """
        from blackbull.fault_injection.h1_server import H1FaultServer
        from blackbull.fault_injection.scenario_h1_server import (
            EndHeaders, HalfClose, ScenarioH1Server, SendStatusLine,
            WaitForRequest,
        )

        scenario = ScenarioH1Server(steps=(
            WaitForRequest(timeout=5.0),
            SendStatusLine(code=204, reason='No Content'),
            EndHeaders(),
            HalfClose(),
        ))

        async with H1FaultServer(scenario) as srv:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection('127.0.0.1', srv.port), timeout=5.0)
            try:
                writer.write(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n')
                await writer.drain()
                await asyncio.wait_for(reader.read(-1), timeout=10.0)
            finally:
                writer.close()

            result = srv.last_result

        assert result is not None
        assert result.half_closed is True
        assert result.terminated is False, (
            'HalfClose ended the scenario instead of continuing it')
        assert 'HalfClose' in result.steps_completed

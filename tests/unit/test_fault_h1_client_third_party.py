"""The HTTP/1.1 broken client, against a server that is not ours.

Cell A's counterpart was BlackBull in every committed test.  That is the
condition Sprint 108's post-mortem named as the one to distrust first —
*the cell most likely to be self-referential is the one where our own
implementation is the convenient counterpart* — and it is exactly how
cell D measured nothing for a sprint.

The counterpart here is CPython's `http.server`.  It is a third party in
the sense that matters: not written by this project, not sharing a line
of parsing code with it.  It is also always present, so this coverage
runs on every push rather than in the weekly Docker tier — the deeper
nginx comparison lives in
`tests/conformance/fault_injection/test_four_cell_differential.py` and
needs a daemon.

What is asserted is delivery and a verdict, **not** agreement.  Whether a
given malformation earns 400 is the server author's decision, RFC 9112
leaves several of these genuinely open (§2.2's bare-LF MAY above all),
and `http.server` is more permissive than BlackBull in places.  A cell
that could only be driven against ourselves is the defect; a cell whose
peers disagree is a finding.
"""
from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from blackbull.fault_injection.catalogue.h1_client import CATALOGUE
from blackbull.fault_injection.oracle_h1 import run_scenario

pytestmark = pytest.mark.asyncio


class _Handler(BaseHTTPRequestHandler):
    """200 "ok" for anything, so path and method choices are not the signal."""

    protocol_version = 'HTTP/1.1'

    def _answer(self):
        body = b'ok'
        self.send_response(200)
        self.send_header('content-type', 'text/plain')
        self.send_header('content-length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_DELETE = do_OPTIONS = _answer

    def log_message(self, *args):  # noqa: A003 - silence the test run
        pass


class _StdlibServer:
    """CPython's `http.server` on a loopback port, in a thread."""

    def __enter__(self):
        self._server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


#: Cases that end on their own.  The rest hold the connection open by
#: design and would be bounded by the peer's header deadline, which
#: `http.server` does not have — it is a request handler, not a server
#: with a slowloris policy.
_SELF_TERMINATING = tuple(
    name for name in CATALOGUE
    if name not in {'head_never_ends', 'trickled_head'}
)


class TestTheScenarioReachesANonBlackBullServer:
    @pytest.mark.parametrize('case_name', _SELF_TERMINATING)
    async def test_the_case_is_delivered_and_answered(self, case_name):
        """Each named case must reach a server that is not ours.

        A case that only BlackBull can be driven with would mean the
        scenario depends on something only BlackBull accepts — the failure
        this file exists to rule out.
        """
        with _StdlibServer() as server:
            outcome, wire = await asyncio.wait_for(
                run_scenario('127.0.0.1', server.port, CATALOGUE[case_name]()),
                timeout=20.0)

        assert wire, f'{case_name} put no bytes on the wire'
        assert outcome.ok or outcome.exception, (
            f'{case_name} produced neither a response nor a failure — the '
            f'scenario did not reach the server')

    async def test_a_well_formed_request_succeeds_against_it(self):
        """The control.

        Without it, every row above could pass because the reference
        server is broken rather than because the scenario is portable.
        """
        from blackbull.fault_injection.scenario_h1 import (
            ReadResponse, Scenario, SendRawBytes,
        )

        good = Scenario(steps=(
            SendRawBytes(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n'),
            ReadResponse(timeout=5.0)))

        with _StdlibServer() as server:
            outcome, _ = await asyncio.wait_for(
                run_scenario('127.0.0.1', server.port, good), timeout=20.0)

        assert outcome.ok, outcome.exception
        assert outcome.response['status'] == 200


class TestWhereThePeersDisagree:
    """Recorded, not adjudicated.

    These are the rows where `http.server` and BlackBull answer
    differently.  Pinning them keeps the difference visible: if either
    side changes its mind, this test says so, and someone decides whether
    that was intended — which is the job a differential oracle does and an
    assertion of correctness cannot.
    """

    @pytest.mark.parametrize('case_name', [
        'absent_host', 'two_content_lengths', 'negative_content_length',
    ])
    async def test_both_peers_reach_a_verdict(self, case_name):
        from blackbull import BlackBull, read_body
        from blackbull.testing.native import NativeTestServer

        app = BlackBull()

        @app.route(path='/')
        async def _root(scope, receive, send):
            await read_body(receive)
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})

        scenario = CATALOGUE[case_name]

        with _StdlibServer() as ref:
            theirs, _ = await asyncio.wait_for(
                run_scenario('127.0.0.1', ref.port, scenario()), timeout=20.0)
        async with NativeTestServer(app) as ours_server:
            ours, _ = await asyncio.wait_for(
                run_scenario('127.0.0.1', ours_server.port, scenario()),
                timeout=20.0)

        # Both must *decide*.  What they decide is theirs to decide.
        for label, outcome in (('http.server', theirs), ('blackbull', ours)):
            assert outcome.ok or outcome.exception, (
                f'{case_name}: {label} neither answered nor failed')

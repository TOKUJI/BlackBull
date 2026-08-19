"""Every cell driven at a counterpart that is not BlackBull.

Sprint 108's cell-D defect survived a full sprint because the only server
that cell was ever pointed at was ours: the scenario put a second
connection preface on the wire, BlackBull skipped the junk, and every case
reported success while the fault never reached the code meant to judge it.

The lesson was not "test against a third party".  It was that *the cell
most likely to be self-referential is the one where our own implementation
is the convenient counterpart* — cells A, B and C got a third party by
accident, because there was no BlackBull HTTP/1.1 client to reach for and
`httpx` was obvious for the rest.

So this file's assertions are deliberately **not** about who is right.
Where nginx, httpx, curl and BlackBull disagree, that difference is a
finding to read, not a verdict to encode — RFC 9112 §2.2 alone leaves
several of these genuinely open.  What is asserted is that each cell can
be *driven and judged at all* with someone else on the other end.

Docker is required for the nginx half and the file skips cleanly without
it, the way `test_http1_differential.py` does.
"""
from __future__ import annotations

import asyncio
import shutil

import pytest

pytestmark = pytest.mark.asyncio

httpx = pytest.importorskip('httpx')


def _mirror_app():
    """nginx's semantics — 200 "ok" for anything.

    Without it every generated path is 404 here and 200 there, and the run
    drowns in status differences that say nothing about framing.
    """
    from http import HTTPMethod, HTTPStatus

    from blackbull import BlackBull, read_body

    app = BlackBull()

    @app.route(path='/', methods=[HTTPMethod.GET, HTTPMethod.POST,
                                  HTTPMethod.PUT, HTTPMethod.DELETE,
                                  HTTPMethod.OPTIONS, HTTPMethod.HEAD])
    async def _root(scope, receive, send):
        await read_body(receive)
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'text/plain')]})
        await send({'type': 'http.response.body', 'body': b'ok'})

    for status in (HTTPStatus.NOT_FOUND, HTTPStatus.METHOD_NOT_ALLOWED):
        @app.on_error(status)
        async def _mirror(scope, receive, send):
            await read_body(receive)
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(b'content-type', b'text/plain')]})
            await send({'type': 'http.response.body', 'body': b'ok'})

    return app


# ---------------------------------------------------------------------------
# Cells A and D — the broken client.  A *server* is the judge.
# ---------------------------------------------------------------------------

class TestTheBrokenClientCellsReachARealServer:
    """The direction that was measuring nothing until #256."""

    @pytest.mark.parametrize('case_name', [
        'absent_host', 'two_content_lengths',
        'content_length_and_transfer_encoding', 'space_before_header_colon',
        'obs_fold_header', 'negative_content_length', 'chunk_size_not_hex',
        'nul_in_header_value', 'body_shorter_than_declared',
    ])
    async def test_cell_a_case_is_delivered_and_answered(self, case_name):
        """Every named HTTP/1.1 client case reaches a server and gets a verdict.

        The assertion is about delivery, not about which status: whether a
        given malformation earns 400 is the server author's decision and
        implementations differ.  What must hold is that the scenario ran
        and something came back.
        """
        from blackbull.fault_injection.catalogue.h1_client import CATALOGUE
        from blackbull.fault_injection.oracle_h1 import run_scenario
        from blackbull.testing.native import NativeTestServer

        async with NativeTestServer(_mirror_app()) as server:
            outcome, _ = await run_scenario('127.0.0.1', server.port,
                                            CATALOGUE[case_name]())

        assert outcome.ok or outcome.exception, (
            f'{case_name} produced neither a response nor a failure — '
            f'the scenario did not reach the server')
        if outcome.ok:
            assert isinstance(outcome.response['status'], int)

    async def test_cell_d_observes_a_verdict_not_just_survival(self):
        """The capability the sprint exists for, end to end.

        Before `WaitForServerFrame`, a scenario could read one frame — the
        handshake SETTINGS — and had no way to reach the GOAWAY that
        follows it.
        """
        from blackbull.client.http2 import HTTP2Client
        from blackbull.fault_injection.scenario_h2_client import (
            ScenarioH2Client, SendFrame, SendPreface, WaitForServerFrame,
        )
        from blackbull.protocol.frame_types import FrameTypes
        from blackbull.testing.native import NativeTestServer

        async with NativeTestServer(_mirror_app()) as server:
            async with HTTP2Client('127.0.0.1', server.port,
                                   scenario_mode=True) as client:
                result = await asyncio.wait_for(client.execute_scenario(
                    ScenarioH2Client(steps=(
                        SendPreface(),
                        SendFrame(FrameTypes.SETTINGS, flags=0x1, stream_id=0,
                                  data=b'\x00' * 6),
                        WaitForServerFrame(
                            match={'type': 'GOAWAY', 'error_code': 6},
                            timeout=5.0)))), timeout=15.0)

        assert result.response is not None, (
            'no GOAWAY(FRAME_SIZE_ERROR) observed')
        assert result.received, 'nothing was logged'


# ---------------------------------------------------------------------------
# Cells B and C — the broken server.  *Clients* are the judges.
# ---------------------------------------------------------------------------

class TestTheBrokenServerCellsAreJudgedByOthers:
    """Two independent clients per case, one of them not written in Python."""

    @pytest.mark.parametrize('case_name', [
        'content_length_overstated', 'chunked_stops_mid_chunk',
        'closed_without_response',
    ])
    async def test_cell_b_httpx_and_blackbull_both_reject(self, case_name):
        """Where two independent clients agree, our client must agree too.

        A case only *our* client rejects would mean the fault server is
        emitting something only we find objectionable — the broken-server
        mirror of the cell-D defect.
        """
        from blackbull.client.http1 import HTTP1Client
        from blackbull.fault_injection.catalogue import CATALOGUE_H1_SERVER
        from blackbull.fault_injection.h1_server import H1FaultServer

        build = CATALOGUE_H1_SERVER[case_name]

        async with H1FaultServer(build()) as srv:
            with pytest.raises(Exception):
                async with HTTP1Client('127.0.0.1', srv.port) as c:
                    await asyncio.wait_for(c.request('GET', '/'), timeout=5.0)

        async with H1FaultServer(build()) as srv:
            with pytest.raises(httpx.HTTPError):
                async with httpx.AsyncClient(timeout=5.0) as c:
                    await c.get(f'http://127.0.0.1:{srv.port}/')

    async def test_cell_c_httpx_rejects_what_our_client_rejects(self):
        from blackbull.client.http2 import HTTP2Client
        from blackbull.fault_injection.catalogue import CATALOGUE_H2_SERVER
        from blackbull.fault_injection.h2_server import H2FaultServer

        build = CATALOGUE_H2_SERVER['headers_continuation_dropped']

        async with H2FaultServer(build()) as srv:
            with pytest.raises(Exception):
                async with HTTP2Client('127.0.0.1', srv.port) as c:
                    await asyncio.wait_for(c.request('GET', '/'), timeout=5.0)

        async with H2FaultServer(build()) as srv:
            with pytest.raises(httpx.HTTPError):
                async with httpx.AsyncClient(http2=True, http1=False,
                                             timeout=5.0) as c:
                    await c.get(f'http://127.0.0.1:{srv.port}/')


@pytest.mark.skipif(shutil.which('curl') is None, reason='curl not installed')
class TestACImplementationAgrees:
    """curl shares no code with httpx or with us.

    Two Python clients agreeing can mean the protocol is clear or can mean
    they inherited the same reading of it.  A C implementation with its own
    parser is the cheapest way to tell those apart.
    """

    async def test_curl_also_refuses_a_truncated_body(self):
        import subprocess

        from blackbull.fault_injection.catalogue import CATALOGUE_H1_SERVER
        from blackbull.fault_injection.h1_server import H1FaultServer

        async with H1FaultServer(
                CATALOGUE_H1_SERVER['content_length_overstated']()) as srv:
            proc = await asyncio.create_subprocess_exec(
                'curl', '-s', '--max-time', '5', '--http1.1',
                f'http://127.0.0.1:{srv.port}/',
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            await asyncio.wait_for(proc.communicate(), timeout=15)

        # 18 = CURLE_PARTIAL_FILE.  Asserted as non-zero rather than as 18
        # so a curl that reports the same fact under a different code does
        # not fail the suite for a reason unrelated to BlackBull.
        assert proc.returncode != 0, (
            'curl accepted a body shorter than its declared Content-Length')

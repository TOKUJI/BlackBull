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

The two broken-**client** cells need a third-party *server*, and that is
nginx, in a container built from `nginx_h2c/` (see `_reference_peers.py`).  One listener
speaks HTTP/1.1 and h2c, so both cells point at the same peer.  Docker is
required for that half only; it skips cleanly without one, the way
`test_http1_differential.py` does — except that a daemon which answers but
cannot build the peer fails, because that image is missing rather than absent
by design.  The broken-**server** cells (which need third-party *clients*, not
servers) run either way.
"""
from __future__ import annotations

import asyncio
import shutil

import pytest

from blackbull.fault_injection.oracle_h1 import Observed

from ._reference_peers import PEER_BUDGET

#: Every outcome of a case that ran against its server.
_REACHED = frozenset(Observed) - {Observed.ERROR, Observed.NO_READ}

#: One worker runs the peers under `-n auto`, so a run starts one nginx, not
#: one per worker racing the Docker Desktop port relay.
pytestmark = [pytest.mark.asyncio, pytest.mark.xdist_group('reference_peers')]

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
    async def test_cell_a_case_is_delivered(self, case_name):
        """Every named HTTP/1.1 client case runs against a real server.

        The assertion is about delivery, not about which status: whether a
        given malformation earns 400 is the server author's decision and
        implementations differ.  Silence is an outcome too — a server may
        wait out a body that never arrives.
        """
        from blackbull.fault_injection.catalogue.h1_client import CATALOGUE
        from blackbull.fault_injection.oracle_h1 import run_scenario
        from blackbull.testing.native import NativeTestServer

        async with NativeTestServer(_mirror_app()) as server:
            outcome, _ = await run_scenario('127.0.0.1', server.port,
                                            CATALOGUE[case_name]())

        assert outcome.observed in _REACHED, (
            f'{case_name} did not run against the server: {outcome}')
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


    @pytest.mark.parametrize('case_name', [
        'absent_host', 'two_content_lengths',
        'content_length_and_transfer_encoding', 'space_before_header_colon',
        'obs_fold_header', 'negative_content_length', 'chunk_size_not_hex',
        'nul_in_header_value', 'body_shorter_than_declared',
        'duplicate_transfer_encoding', 'oversized_method_token',
    ])
    @pytest.mark.timeout(PEER_BUDGET)
    async def test_cell_a_case_reaches_nginx_too(self, nginx_peer, case_name):
        """The same named case, delivered to a server that is not ours.

        Cell A's counterpart had been BlackBull in every committed test —
        the condition that let cell D measure nothing for a sprint.  The
        assertion is delivery, not agreement: whether a given malformation
        earns 400 is the server author's call, and nginx and BlackBull
        genuinely differ on some of these.
        """
        from blackbull.fault_injection.catalogue.h1_client import CATALOGUE
        from blackbull.fault_injection.oracle_h1 import run_scenario

        host, port = nginx_peer
        outcome, wire = await run_scenario(host, port, CATALOGUE[case_name]())

        assert wire, f'{case_name} put no bytes on the wire'
        assert outcome.observed in _REACHED, (
            f'{case_name} did not run against nginx: {outcome}')

    @pytest.mark.timeout(PEER_BUDGET)
    async def test_cell_d_observes_a_verdict_from_nginx(self, nginx_peer):
        """The verdict step, against a server with no BlackBull in it.

        nginx answering GOAWAY(FRAME_SIZE_ERROR) is what makes
        `WaitForServerFrame` a fact about HTTP/2 rather than about our own
        server's frame ordering.
        """
        from blackbull.client.http2 import HTTP2Client
        from blackbull.fault_injection.scenario_h2_client import (
            ScenarioH2Client, SendFrame, SendPreface, WaitForServerFrame,
        )
        from blackbull.protocol.frame_types import FrameTypes

        host, port = nginx_peer
        async with HTTP2Client(host, port, scenario_mode=True) as client:
            result = await asyncio.wait_for(client.execute_scenario(
                ScenarioH2Client(steps=(
                    SendPreface(),
                    SendFrame(FrameTypes.SETTINGS, flags=0x1, stream_id=0,
                              data=b'\x00' * 6),
                    WaitForServerFrame(
                        match={'type': 'GOAWAY', 'error_code': 6},
                        timeout=8.0)))), timeout=25.0)

        assert result.response is not None, (
            'nginx sent no GOAWAY(FRAME_SIZE_ERROR); RFC 9113 §6.5 says a '
            'SETTINGS ACK carrying a payload is a FRAME_SIZE_ERROR')
        assert type(result.response).__name__ == 'GoAway'

    @pytest.mark.parametrize('case_name', [
        'rapid_reset_burst', 'ping_flood', 'settings_flood',
        'unknown_frame_type', 'settings_ack_with_payload',
    ])
    @pytest.mark.timeout(PEER_BUDGET)
    async def test_cell_d_case_reaches_nginx(self, nginx_peer, case_name):
        """Every self-terminating cell-D case, delivered to nginx."""
        from blackbull.client.http2 import HTTP2Client
        from blackbull.fault_injection.catalogue.h2_client import CATALOGUE

        host, port = nginx_peer
        async with HTTP2Client(host, port, scenario_mode=True) as client:
            result = await asyncio.wait_for(
                client.execute_scenario(CATALOGUE[case_name]()), timeout=25.0)

        assert result.steps_completed > 0, (
            f'{case_name} delivered no steps to nginx')

# ---------------------------------------------------------------------------
# Cells B and C — the broken server.  *Clients* are the judges.
# ---------------------------------------------------------------------------

class TestTheBrokenServerCellsAreJudgedByOthers:
    """Two independent clients per case, one of them not written in Python."""

    @pytest.mark.parametrize('case_name', [
        'content_length_overstated', 'chunked_stops_mid_chunk',
        'closed_without_response', 'half_closed_after_headers',
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

    @pytest.mark.parametrize('case_name', [
        'headers_continuation_dropped', 'half_closed_after_headers',
    ])
    async def test_cell_c_httpx_rejects_what_our_client_rejects(self, case_name):
        from blackbull.client.http2 import HTTP2Client
        from blackbull.fault_injection.catalogue import CATALOGUE_H2_SERVER
        from blackbull.fault_injection.h2_server import H2FaultServer

        build = CATALOGUE_H2_SERVER[case_name]

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

    @pytest.mark.parametrize('case_name', [
        'content_length_overstated',
        # A half-close is the case most likely to be handled differently
        # by a client that maps FIN onto "connection reset": curl's
        # answer here is independent evidence that the FIN carries the
        # meaning the scenario intends.
        'half_closed_after_headers',
    ])
    async def test_curl_also_refuses_a_truncated_body(self, case_name):
        import subprocess

        from blackbull.fault_injection.catalogue import CATALOGUE_H1_SERVER
        from blackbull.fault_injection.h1_server import H1FaultServer

        async with H1FaultServer(CATALOGUE_H1_SERVER[case_name]()) as srv:
            proc = await asyncio.create_subprocess_exec(
                'curl', '-s', '--max-time', '5', '--http1.1',
                f'http://127.0.0.1:{srv.port}/',
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            await asyncio.wait_for(proc.communicate(), timeout=15)

        # 18 = CURLE_PARTIAL_FILE.  Asserted as non-zero rather than as 18
        # so a curl that reports the same fact under a different code does
        # not fail the suite for a reason unrelated to BlackBull.
        assert proc.returncode != 0, (
            f'{case_name}: curl accepted a body shorter than its declared '
            f'Content-Length')

    async def test_curl_refuses_a_broken_http2_server_too(self):
        """Cell C's second implementation.

        httpx drives `h2`, which is Python; curl drives nghttp2, which is
        not.  Without this, cell C's only judges are two Python stacks and
        an agreement between them could be a shared reading rather than a
        property of the protocol.
        """
        import subprocess

        from blackbull.fault_injection.catalogue import CATALOGUE_H2_SERVER
        from blackbull.fault_injection.h2_server import H2FaultServer

        async with H2FaultServer(
                CATALOGUE_H2_SERVER['headers_continuation_dropped']()) as srv:
            proc = await asyncio.create_subprocess_exec(
                'curl', '-s', '--max-time', '5', '--http2-prior-knowledge',
                f'http://127.0.0.1:{srv.port}/',
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            await asyncio.wait_for(proc.communicate(), timeout=15)

        assert proc.returncode != 0, (
            'curl accepted a HEADERS block that never ended')

    async def test_curl_and_httpx_agree_a_half_close_is_not_a_reset(self):
        """The distinction the step exists for, checked by two implementations.

        `HalfClose` and `Abort` are only worth having as separate steps if
        a peer can tell them apart.  Two independent clients reporting the
        *same* difference is what makes that a property of the wire rather
        than of one library's error mapping.
        """
        import subprocess

        from blackbull.fault_injection.catalogue import CATALOGUE_H1_SERVER
        from blackbull.fault_injection.h1_server import H1FaultServer

        async def curl_exit(build) -> int:
            async with H1FaultServer(build()) as srv:
                proc = await asyncio.create_subprocess_exec(
                    'curl', '-sS', '--max-time', '5', '--http1.1',
                    f'http://127.0.0.1:{srv.port}/',
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                await asyncio.wait_for(proc.communicate(), timeout=15)
            return proc.returncode

        async def httpx_error(build) -> str:
            async with H1FaultServer(build()) as srv:
                async with httpx.AsyncClient(timeout=5.0) as c:
                    try:
                        await c.get(f'http://127.0.0.1:{srv.port}/')
                    except httpx.HTTPError as exc:
                        return f'{type(exc).__name__}: {exc}'.lower()
            return ''

        half = CATALOGUE_H1_SERVER['half_closed_after_headers']
        reset = CATALOGUE_H1_SERVER['closed_without_response']

        # A half-close after headers: the client got a complete head and an
        # incomplete body, so both should say so in those terms.
        assert 'body' in await httpx_error(half), (
            'httpx did not describe the half-close as a truncated body')
        # curl's exit codes, not its wording, which changes between releases.
        assert await curl_exit(half) == 18, (       # CURLE_PARTIAL_FILE
            'curl did not report the half-close as a truncated transfer')

        # A reset before any response is a different report on both.
        assert 'body' not in await httpx_error(reset), (
            'httpx reported a pre-response reset the same way as a '
            'half-close — the two steps are indistinguishable on the wire')
        assert await curl_exit(reset) == 52, (      # CURLE_GOT_NOTHING
            'curl did not report the pre-response reset as an empty reply')

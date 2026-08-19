"""The HTTP/1.1 broken server — the direction HTTP/1.1 did not have.

`docs/guide/fault_injection.md` says "two directions are supported" and
it is telling the truth, but each protocol had only **one** and they were
opposite ones: HTTP/1.1 had the broken *client*
(`HTTP1Client.execute_scenario`), HTTP/2 the broken *server*
(`H2FaultServer`).  A reader who took the sentence as "both directions on
both protocols" was wrong and nothing corrected them.

This is the missing cell: a programmable misbehaving HTTP/1.1 server for
people testing their own clients.

**The load-bearing rule under test here** is that the breaking side
assembles its own bytes.  `h2_server.py` already works this way — it
carries its own frame encoder rather than calling the production
`FrameBase.save()` — and the reason is that a fault server sharing the
production serialiser cannot catch a bug the production serialiser has.
`TestTheBreakerIsIndependent` pins it.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.client.exceptions import (
    ConnectionError as ClientConnectionError, ProtocolError,
)
from blackbull.client.http1 import HTTP1Client
from blackbull.server.recipient import IncompleteReadError
from blackbull.fault_injection.catalogue.h1 import CATALOGUE
from blackbull.fault_injection.h1_server import H1FaultServer, H1FaultServerError
from blackbull.fault_injection.scenario_h1_server import (
    Abort, CloseGracefully, ScenarioH1Server, SendRawBytes, Sleep, WaitForRequest,
)

pytestmark = pytest.mark.asyncio


#: What a client is allowed to do when the server misbehaves: refuse, in any
#: of the shapes this stack expresses a refusal.  The assertion that matters
#: is the negative one — it must not hand back a truncated body as a complete
#: response — so the tuple is deliberately wide.  It does **not** include the
#: builtin ``ConnectionError``: the client raises its own, and a tuple that
#: quietly catches neither would pass for the wrong reason.
_REFUSED = (
    IncompleteReadError,          # the reader ran out mid-message
    asyncio.IncompleteReadError,  # ... or asyncio's, depending on the path
    ProtocolError,                # the response was well-formed nonsense
    ClientConnectionError,        # the peer went away before it finished
    asyncio.TimeoutError,         # nothing arrived at all
)


async def _get(url_host: str, port: int, *, timeout: float = 2.0):
    async with HTTP1Client(url_host, port) as client:
        return await asyncio.wait_for(client.request('GET', '/'), timeout=timeout)


# ===========================================================================
# The server misbehaves on command
# ===========================================================================

class TestDeliberateMisbehaviour:
    async def test_a_status_line_delivered_in_pieces(self):
        """A trickled status line: every byte legal, the pause is the fault."""
        scenario = ScenarioH1Server(steps=(
            WaitForRequest(),
            SendRawBytes(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi',
                         byte_interval=0.001),
        ))
        async with H1FaultServer(scenario) as srv:
            resp = await _get(srv.host, srv.port)
        assert resp.status == 200
        assert resp.body == b'hi'

    async def test_a_content_length_that_overstates_the_body(self):
        """The client must not return a short body as if it were complete."""
        scenario = ScenarioH1Server(steps=(
            WaitForRequest(),
            SendRawBytes(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort'),
            CloseGracefully(),
        ))
        async with H1FaultServer(scenario) as srv:
            with pytest.raises(_REFUSED):
                await _get(srv.host, srv.port)

    async def test_a_chunked_body_that_stops_mid_chunk(self):
        scenario = ScenarioH1Server(steps=(
            WaitForRequest(),
            SendRawBytes(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n'
                         b'\r\n5\r\nab'),
            CloseGracefully(),
        ))
        async with H1FaultServer(scenario) as srv:
            with pytest.raises(_REFUSED):
                await _get(srv.host, srv.port)

    async def test_a_connection_closed_without_any_response(self):
        scenario = ScenarioH1Server(steps=(WaitForRequest(), Abort()))
        async with H1FaultServer(scenario) as srv:
            with pytest.raises(_REFUSED):
                await _get(srv.host, srv.port)

    async def test_a_server_that_never_answers(self):
        """The client's own deadline is what must end this, not the server."""
        scenario = ScenarioH1Server(steps=(WaitForRequest(), Sleep(5.0)))
        async with H1FaultServer(scenario) as srv:
            with pytest.raises(asyncio.TimeoutError):
                await _get(srv.host, srv.port, timeout=0.3)


# ===========================================================================
# The rule that makes a fault server worth having
# ===========================================================================

class TestTheBreakerIsIndependent:
    async def test_the_h1_fault_server_does_not_use_the_production_sender(self):
        """A breaker that shares the production serialiser cannot catch a bug
        the production serialiser has.

        `h2_server.py` established this: it carries its own frame encoder and
        imports only constants from the protocol package.  The same rule has
        to hold here, and it is worth a test rather than a comment because it
        is invisible until the day it matters.
        """
        import pathlib
        import re

        src = (pathlib.Path(__file__).resolve().parents[2]
               / 'blackbull' / 'fault_injection' / 'h1_server.py').read_text()
        offenders = [line.strip() for line in src.splitlines()
                     if re.match(r'\s*(from|import)\s', line)
                     and ('server.sender' in line or 'server.response' in line)]
        assert offenders == [], (
            f'the fault server imports the production send path: {offenders}')


# ===========================================================================
# Safety locks — the same two H2FaultServer carries
# ===========================================================================

class TestSafetyLocks:
    async def test_it_refuses_a_non_loopback_bind(self):
        scenario = ScenarioH1Server(steps=())
        with pytest.raises(H1FaultServerError):
            H1FaultServer(scenario, host='0.0.0.0')

    async def test_allow_remote_is_the_explicit_opt_in(self):
        scenario = ScenarioH1Server(steps=())
        srv = H1FaultServer(scenario, host='0.0.0.0', allow_remote=True)
        assert srv.host == '0.0.0.0'

    async def test_it_refuses_to_run_in_production(self, monkeypatch):
        monkeypatch.setenv('BB_PRODUCTION', '1')
        with pytest.raises(H1FaultServerError):
            H1FaultServer(ScenarioH1Server(steps=()))


# ===========================================================================
# The catalogue, driven from both sides of the independence question
# ===========================================================================

class TestCatalogue:
    """Every named case, against our client and against a third party.

    Driving with `HTTP1Client` exercises the client Sprint 106 has just
    repaired.  Driving the same cases with `httpx` is the cross-check
    borrowed from how `httpx` itself works: if our client and our fault
    server ever agree on something wrong, an independent implementation is
    what notices.  `trickled_status_line` is the case that must *succeed*
    on both — a correct response delivered slowly is not a fault, and a
    client that fails it has a bug of its own.
    """

    #: Cases where a **200 is the correct outcome**, with the reason.  Not
    #: every entry in a fault catalogue is a fault the client should refuse:
    #:
    #: * ``trickled_status_line`` — the response is correct, only slow.  A
    #:   client that fails it has a bug of its own.
    #: * ``content_length_understated`` — RFC 9112 says read exactly
    #:   ``Content-Length`` octets, so returning the 2-byte body *is* the
    #:   conformant answer.  The hazard is not this exchange but the next
    #:   one: the surplus is a whole second response left in the buffer, and
    #:   a keep-alive client that reuses the connection parses it as the
    #:   reply to a request it has not sent.  That is a desync test, not a
    #:   refusal test, and it needs a second request to express — which is
    #:   why the case is catalogued here and asserted only this far.
    _SUCCEEDS = {'trickled_status_line', 'content_length_understated'}

    @pytest.mark.parametrize('case_name', sorted(CATALOGUE))
    async def test_our_client_survives_every_case(self, case_name):
        scenario = CATALOGUE[case_name]()
        async with H1FaultServer(scenario) as srv:
            if case_name in self._SUCCEEDS:
                resp = await _get(srv.host, srv.port, timeout=3.0)
                assert resp.status == 200
                if case_name == 'content_length_understated':
                    assert resp.body == b'hi', (
                        'the client read past the declared Content-Length')
                return
            with pytest.raises(_REFUSED):
                await _get(srv.host, srv.port, timeout=0.25)

    @pytest.mark.parametrize('case_name', sorted(CATALOGUE))
    async def test_an_independent_client_agrees(self, case_name):
        """The cross-check: if our client and our fault server ever agree on
        something wrong, a third implementation is what notices."""
        httpx = pytest.importorskip('httpx')
        scenario = CATALOGUE[case_name]()
        async with H1FaultServer(scenario) as srv:
            url = f'http://{srv.host}:{srv.port}/'
            async with httpx.AsyncClient(timeout=0.25) as client:
                if case_name in self._SUCCEEDS:
                    resp = await client.get(url)
                    assert resp.status_code == 200
                    return
                with pytest.raises(Exception) as exc:
                    await client.get(url)
                assert not isinstance(exc.value, AssertionError)


# ===========================================================================
# The package export surface
# ===========================================================================

class TestExportsDoNotCollide:
    """Three vocabularies share step names; the package must keep them apart.

    This is not hypothetical.  The documented quick-start originally imported
    a bare ``SendRawBytes`` from the package, which resolved to **HTTP/2's** —
    so the example in the docs raised ``unknown scenario step`` when run.  The
    unit tests missed it because they import from the submodule, which is not
    what the docs tell a reader to do.
    """

    async def test_the_documented_import_builds_a_runnable_scenario(self):
        """Import exactly the way the guide does, and run it."""
        from blackbull.fault_injection import (
            H1FaultServer as Srv, H1SCloseGracefully, H1SSendRawBytes,
            ScenarioH1Server as Scn, WaitForRequest as Wait,
        )

        scenario = Scn(steps=(
            Wait(),
            H1SSendRawBytes(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi'),
            H1SCloseGracefully(),
        ))
        async with Srv(scenario) as srv:
            resp = await _get(srv.host, srv.port)
        assert resp.status == 200 and resp.body == b'hi'

    async def test_each_vocabulary_is_reachable_and_distinct(self):
        import blackbull.fault_injection as fi
        from blackbull.fault_injection import scenario_h1, scenario_h1_server, scenario_h2

        assert fi.H1SSendRawBytes is scenario_h1_server.SendRawBytes
        assert fi.SendRawBytes is scenario_h2.SendRawBytes
        assert fi.Abort is scenario_h1.Abort
        assert fi.H1SAbort is scenario_h1_server.Abort
        assert fi.H2Abort is scenario_h2.Abort

    async def test_both_servers_expose_host_and_port(self):
        """A caller driving either with a raw socket needs the pair."""
        from blackbull.fault_injection import (
            H2FaultServer, ScenarioH1Server, ScenarioH2,
        )
        h1 = H1FaultServer(ScenarioH1Server(steps=()))
        h2 = H2FaultServer(ScenarioH2(steps=()))
        for srv in (h1, h2):
            assert isinstance(srv.host, str)
            assert isinstance(srv.port, int)

    async def test_both_catalogues_are_reachable_the_same_way(self):
        from blackbull.fault_injection.catalogue import CATALOGUE_H1, CATALOGUE_H2
        assert len(CATALOGUE_H1) == 9
        assert len(CATALOGUE_H2) == 4

    async def test_a_scenario_survives_a_json_round_trip(self):
        """H2 scenarios serialise; H1 server scenarios now do too."""
        from blackbull.fault_injection import (
            scenario_h1_server_from_json, scenario_h1_server_to_json,
        )
        for name, build in CATALOGUE.items():
            scenario = build()
            assert scenario_h1_server_from_json(
                scenario_h1_server_to_json(scenario)) == scenario, name


# ===========================================================================
# The two halves report the same things by the same names
# ===========================================================================

class TestResultSymmetry:
    """A harness reporting on one half must not need a second spelling.

    The HTTP/1.1 server result originally invented `completed`,
    `bytes_sent` and `elapsed` where HTTP/2 already had `steps_completed`,
    `server_bytes_sent` and `elapsed_s`.  Nothing was wrong with either
    set; having both was the defect.
    """

    async def test_the_protocol_neutral_fields_match(self):
        import dataclasses as dc
        from blackbull.fault_injection import (
            ScenarioH1Server, ScenarioH1ServerResult, ScenarioH2,
        )
        from blackbull.fault_injection.scenario_h2 import ScenarioH2Result

        h1 = {f.name for f in dc.fields(ScenarioH1ServerResult)}
        h2 = {f.name for f in dc.fields(ScenarioH2Result)}
        #: The one field that is genuinely HTTP/1.1-only: HTTP/2 has no
        #: single "head" to capture, it has frames.  Everything else must
        #: exist on both — this test caught `wait_skipped` and
        #: `expectations` being added to one half only, an hour after they
        #: were written.
        assert h1 - h2 == {'request_head'}
        assert h2 - h1 == set(), (
            f'HTTP/2 reports fields HTTP/1.1 does not: {sorted(h2 - h1)}')

        #: `name` is protocol-neutral and both scenarios carry it.
        assert 'name' in {f.name for f in dc.fields(ScenarioH1Server)}
        assert 'name' in {f.name for f in dc.fields(ScenarioH2)}

    async def test_a_run_populates_the_shared_fields(self):
        from blackbull.fault_injection import H1SCloseGracefully, H1SSendRawBytes

        scenario = ScenarioH1Server(steps=(
            WaitForRequest(),
            H1SSendRawBytes(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi'),
            H1SCloseGracefully(),
        ), name='symmetry_probe')
        async with H1FaultServer(scenario) as srv:
            await _get(srv.host, srv.port)
            await srv.wait_for_connection_done(timeout=5.0)
            result = srv.last_result

        assert result.steps_completed == [
            'WaitForRequest', 'SendRawBytes', 'CloseGracefully']
        assert result.server_bytes_sent > 0
        assert result.client_bytes_received > 0
        assert result.terminated is True
        assert result.wait_timed_out is False
        assert result.elapsed_s > 0


# =========================================================================
# The shipped example
# ===========================================================================

class TestTheExampleStillRuns:
    """`examples/fault_injection.py` is the toolkit's front door.

    An example that stopped working is worse than no example: it is the
    first thing a reader runs, and it fails in *their* terminal rather than
    in CI.  This sprint has already broken one — the guide's quick-start
    imported a step from the wrong half — so the example gets a test rather
    than a promise.
    """

    async def test_the_example_imports_resolve(self):
        """Every name the example imports must exist, with the right half."""
        import ast
        import pathlib

        src = (pathlib.Path(__file__).resolve().parents[2]
               / 'examples' / 'fault_injection.py')
        tree = ast.parse(src.read_text())
        imported = [
            (node.module, alias.name)
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
            and node.module.startswith('blackbull')
            for alias in node.names
        ]
        assert imported, 'the example imports nothing from blackbull'
        import importlib
        for module, name in imported:
            mod = importlib.import_module(module)
            assert hasattr(mod, name), f'{module} has no {name}'

    async def test_cell_b_and_e_run(self):
        """The two cells this sprint added, executed for real.

        Cells A and C are left to the example itself: A starts a threaded
        stdlib server and C needs TLS, and neither is what this sprint
        changed.
        """
        import importlib.util
        import pathlib

        path = (pathlib.Path(__file__).resolve().parents[2]
                / 'examples' / 'fault_injection.py')
        spec = importlib.util.spec_from_file_location('_fi_example', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        await asyncio.wait_for(module.cell_b_broken_server(), timeout=60.0)
        await asyncio.wait_for(module.cell_e_scenarios_as_data(), timeout=20.0)

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
    Abort, CloseConnection, ScenarioH1Server, SendRawBytes, Sleep, WaitForRequest,
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
        scenario = ScenarioH1Server(steps=[
            WaitForRequest(),
            SendRawBytes(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi',
                         byte_interval=0.001),
        ])
        async with H1FaultServer(scenario) as srv:
            resp = await _get(srv.host, srv.port)
        assert resp.status == 200
        assert resp.body == b'hi'

    async def test_a_content_length_that_overstates_the_body(self):
        """The client must not return a short body as if it were complete."""
        scenario = ScenarioH1Server(steps=[
            WaitForRequest(),
            SendRawBytes(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort'),
            CloseConnection(),
        ])
        async with H1FaultServer(scenario) as srv:
            with pytest.raises(_REFUSED):
                await _get(srv.host, srv.port)

    async def test_a_chunked_body_that_stops_mid_chunk(self):
        scenario = ScenarioH1Server(steps=[
            WaitForRequest(),
            SendRawBytes(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n'
                         b'\r\n5\r\nab'),
            CloseConnection(),
        ])
        async with H1FaultServer(scenario) as srv:
            with pytest.raises(_REFUSED):
                await _get(srv.host, srv.port)

    async def test_a_connection_closed_without_any_response(self):
        scenario = ScenarioH1Server(steps=[WaitForRequest(), Abort()])
        async with H1FaultServer(scenario) as srv:
            with pytest.raises(_REFUSED):
                await _get(srv.host, srv.port)

    async def test_a_server_that_never_answers(self):
        """The client's own deadline is what must end this, not the server."""
        scenario = ScenarioH1Server(steps=[WaitForRequest(), Sleep(5.0)])
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
        scenario = ScenarioH1Server(steps=[])
        with pytest.raises(H1FaultServerError):
            H1FaultServer(scenario, host='0.0.0.0')

    async def test_allow_remote_is_the_explicit_opt_in(self):
        scenario = ScenarioH1Server(steps=[])
        srv = H1FaultServer(scenario, host='0.0.0.0', allow_remote=True)
        assert srv.host == '0.0.0.0'

    async def test_it_refuses_to_run_in_production(self, monkeypatch):
        monkeypatch.setenv('BB_PRODUCTION', '1')
        with pytest.raises(H1FaultServerError):
            H1FaultServer(ScenarioH1Server(steps=[]))


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

"""The fault-injection toolkit, one cell of the grid at a time.

`blackbull.fault_injection` breaks HTTP on purpose so you can find out how
the *other* side reacts.  There are two roles and two protocols, so there
are four cells — and which one you want depends entirely on which side you
are testing::

              broken CLIENT -> your server     broken SERVER -> your client
    HTTP/1.1  A. yes                           B. yes
    HTTP/2    D. yes                           C. yes

This one file walks every implemented cell, then shows the same scenarios
as data rather than code.  It replaces the two per-protocol examples that
came before it: a file per cell would have meant four, and the next cell
would have meant five.

Run::

    python examples/fault_injection.py

Cells B and C need a real third-party client and TLS::

    pip install 'blackbull[fault-injection]'

Without it those cells report themselves skipped rather than failing —
cell A needs nothing but the standard library.  See
``docs/guide/fault_injection.md`` for the tutorial.
"""
from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from blackbull.client import HTTP1Client
from blackbull.fault_injection import (
    Abort,
    H1FaultServer,
    H1SCloseGracefully,
    H1SSendRawBytes,
    H2FaultServer,
    ReadResponse,
    Scenario,
    ScenarioH1Server,
    H1CSendRawBytes,
    Sleep,
    WaitForRequest,
    make_self_signed_h2_context,
    scenario_h1_server_from_json,
    scenario_h1_server_to_json,
)
from blackbull.fault_injection.catalogue import CATALOGUE_H1, CATALOGUE_H2

try:
    import httpx
except ImportError:  # pragma: no cover - the extra is optional
    httpx = None

PER_REQUEST_TIMEOUT_S = 4.0


def _heading(letter: str, title: str) -> None:
    print(f'\n{"=" * 72}\n{letter}. {title}\n{"=" * 72}')


# ===========================================================================
# A. HTTP/1.1 — broken client against a real server
# ===========================================================================
#
# The counterpart is the standard library's own HTTP server, running in a
# background thread.  Its reactions are worth reading against a hardened
# server's: stdlib waits patiently where nginx or BlackBull would enforce a
# header-read timeout, so "the request still completed" is a finding about
# stdlib, not a pass.

class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:            # noqa: N802 - stdlib's spelling
        body = b'ok\n'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args) -> None:
        pass                             # keep the example's output readable


class _QuietHTTPServer(HTTPServer):
    daemon_threads = True


def _start_stdlib_server() -> tuple[HTTPServer, int]:
    httpd = _QuietHTTPServer(('127.0.0.1', 0), _OkHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


#: A complete, well-formed request — the baseline the misbehaving scenarios
#: below are differentials against.
_WELL_FORMED = (
    b'GET / HTTP/1.1\r\n'
    b'Host: 127.0.0.1\r\n'
    b'Connection: close\r\n'
    b'\r\n'
)

BROKEN_CLIENT_SCENARIOS: dict[str, Scenario] = {
    'well_formed_request': Scenario(steps=(
        H1CSendRawBytes(_WELL_FORMED),
        ReadResponse(timeout=2.0),
    )),
    # Slowloris: one byte every 50 ms.  stdlib waits; a hardened server
    # closes on a request-header read timeout.
    'slowloris_trickle': Scenario(steps=(
        H1CSendRawBytes(_WELL_FORMED, byte_interval=0.05),
        ReadResponse(timeout=5.0),
    )),
    # Request line + Host, no blank line, then silence.  stdlib blocks
    # reading more header lines until the kernel gives up (default: never),
    # so this records a client-side read timeout.
    'partial_headers_idle': Scenario(steps=(
        H1CSendRawBytes(b'GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n'),
        Sleep(duration=1.5),
        ReadResponse(timeout=1.0),
    )),
    # Hard RST after the request line.  Abort short-circuits the scenario;
    # no read happens.
    'abort_after_request_line': Scenario(steps=(
        H1CSendRawBytes(b'GET / HTTP/1.1\r\n'),
        Abort(),
    )),
}


async def cell_a_broken_client() -> None:
    _heading('A', 'HTTP/1.1 — a broken client against a real server')
    httpd, port = _start_stdlib_server()
    print(f'counterpart: stdlib http.server at http://127.0.0.1:{port}/')
    try:
        for name, scenario in BROKEN_CLIENT_SCENARIOS.items():
            print(f'\n  --- {name} ---')
            async with HTTP1Client('127.0.0.1', port) as client:
                result = await client.execute_scenario(scenario)
            print(f'    steps completed: {result.steps_completed}')
            if result.response is not None:
                print(f'    response status: '
                      f'{getattr(result.response, "status", "?")}')
            print(f'    timed out:       {result.timed_out}')
            print(f'    aborted:         {result.aborted}')
            if result.exception is not None:
                print(f'    exception:       {result.exception}')
            print(f'    elapsed:         {result.elapsed_s:.3f}s')
    finally:
        httpd.shutdown()
        httpd.server_close()


# ===========================================================================
# B. HTTP/1.1 — broken server against a real client
# ===========================================================================
#
# Driven twice on purpose: once with BlackBull's own client, once with
# httpx.  If our client and our fault server ever agree on something wrong,
# an independent implementation is what notices — which is the reason a
# fault server assembles its own bytes instead of calling the production
# send path.
#
# Two catalogue entries are *not* failures to catch, and the output says so:
#
#   trickled_status_line       correct HTTP, delivered slowly.  A client
#                              that rejects it has a bug of its own.
#   content_length_understated RFC 9112 says read exactly Content-Length
#                              octets, so a 200 is the conformant answer.
#                              The hazard is the *next* exchange: the
#                              surplus is a whole second response left in
#                              the buffer for a keep-alive client to
#                              mistake for its own reply.

_EXPECTED_OK = {'trickled_status_line', 'content_length_understated'}


async def _drive_with_blackbull(srv: H1FaultServer) -> str:
    try:
        async with HTTP1Client(srv.host, srv.port) as client:
            resp = await asyncio.wait_for(client.request('GET', '/'),
                                          timeout=1.0)
        return f'HTTP {resp.status} ({len(resp.body)} body bytes)'
    except Exception as exc:
        return f'{type(exc).__name__}: {str(exc) or "<no message>"}'


async def _drive_with_httpx(srv: H1FaultServer) -> str:
    if httpx is None:
        return 'skipped (httpx not installed)'
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            resp = await client.get(f'http://{srv.host}:{srv.port}/')
        return f'HTTP {resp.status_code} ({len(resp.content)} body bytes)'
    except Exception as exc:
        return f'{type(exc).__name__}: {str(exc) or "<no message>"}'


#: One scenario written out by hand, so the example shows the vocabulary and
#: not only the catalogue.  Cell A does the same for the client-side steps;
#: without this, a reader learns that named cases exist but not how to write
#: the case they actually need.
_HANDWRITTEN = ScenarioH1Server(name='truncated_header_line', steps=(
    WaitForRequest(),
    # A header line that stops before its CRLF, then nothing more.
    H1SSendRawBytes(b'HTTP/1.1 200 OK\r\nContent-Len'),
    H1SCloseGracefully(),
))


async def cell_b_broken_server() -> None:
    _heading('B', 'HTTP/1.1 — a broken server against real clients')
    print(f'{len(CATALOGUE_H1)} catalogue cases, each driven twice, '
          f'then one written by hand.')
    for name, build in CATALOGUE_H1.items():
        async with H1FaultServer(build()) as srv:
            ours = await _drive_with_blackbull(srv)
        async with H1FaultServer(build()) as srv:
            theirs = await _drive_with_httpx(srv)
        note = '  <- a 200 is correct here' if name in _EXPECTED_OK else ''
        print(f'\n  --- {name} ---{note}')
        print(f'    blackbull: {ours}')
        print(f'    httpx:     {theirs}')

    async with H1FaultServer(_HANDWRITTEN) as srv:
        ours = await _drive_with_blackbull(srv)
    print(f'\n  --- {_HANDWRITTEN.name} (hand-written) ---')
    print(f'    blackbull: {ours}')


# ===========================================================================
# C. HTTP/2 — broken server against a real client
# ===========================================================================
#
# httpx negotiates HTTP/2 only via ALPN over TLS, which is why this cell
# spins up a self-signed context: over plain http:// the client silently
# downgrades to HTTP/1.1 and never speaks the preface the fault server is
# waiting for.

async def cell_c_broken_h2_server() -> None:
    _heading('C', 'HTTP/2 — a broken server against a real client')
    if httpx is None:
        print("skipped: needs pip install 'blackbull[fault-injection]'")
        return
    print(f'{len(CATALOGUE_H2)} catalogue cases against httpx over TLS.')
    for name, build in CATALOGUE_H2.items():
        ssl_ctx = make_self_signed_h2_context()
        async with H2FaultServer(scenario=build(), ssl_context=ssl_ctx) as srv:
            print(f'\n  --- {name} ---')
            print(f'    bound at:        {srv.url}')
            async with httpx.AsyncClient(
                http2=True, verify=ssl_ctx.bb_ca_cert_path,
                timeout=PER_REQUEST_TIMEOUT_S,
            ) as client:
                try:
                    resp = await client.get(srv.url)
                    print(f'    client outcome:  HTTP {resp.status_code} '
                          f'(version {resp.http_version})')
                except httpx.HTTPError as exc:
                    print(f'    client outcome:  {type(exc).__name__}: '
                          f'{str(exc) or "<no message>"}')
            await srv.wait_for_connection_done(timeout=10.0)
            result = srv.last_result
            print(f'    server bytes:    {result.server_bytes_sent} sent / '
                  f'{result.client_bytes_received} received')
            print(f'    steps completed: {result.steps_completed}')
            print(f'    terminated:      {result.terminated}')


# ===========================================================================
# D. HTTP/2 — broken client against a real server
# ===========================================================================
#
# The counterpart is BlackBull's own HTTP/2 server, which is the honest
# choice for this cell: the toolkit is aimed at other people's
# implementations, and the one implementation always on hand in an example
# is ours.  Point it at yours by changing the host and port.
#
# Three catalogue cases close with a long Sleep because *holding* the
# connection is the fault they stage.  Those are left out here — what ends
# them is the server's own deadline, which is a different question at a
# different timescale.

_SELF_TERMINATING = (
    'rapid_reset_burst', 'ping_flood', 'settings_flood',
    'unknown_frame_type', 'settings_ack_with_payload',
    'abort_mid_header_block',
)


async def cell_d_broken_h2_client() -> None:
    _heading('D', 'HTTP/2 — a broken client against a real server')
    from blackbull import BlackBull
    from blackbull.client.http2 import HTTP2Client
    from blackbull.fault_injection.catalogue.h2_client import (
        CATALOGUE as CATALOGUE_H2C,
    )
    from blackbull.testing import NativeTestServer

    app = BlackBull()

    @app.route(path='/')
    async def _root(conn):
        return 'ok'

    print(f'{len(_SELF_TERMINATING)} of {len(CATALOGUE_H2C)} catalogue cases '
          f'(the rest hold the connection open on purpose).')
    async with NativeTestServer(app) as server:
        print(f'counterpart: BlackBull on 127.0.0.1:{server.port}')
        for name in _SELF_TERMINATING:
            async with HTTP2Client('127.0.0.1', server.port,
                                   scenario_mode=True) as client:
                result = await asyncio.wait_for(
                    client.execute_scenario(CATALOGUE_H2C[name]()),
                    timeout=10.0)
            print(f'\n  --- {name} ---')
            print(f'    steps completed: {result.steps_completed}')
            print(f'    timed out:       {result.timed_out}')
            print(f'    aborted:         {result.aborted}')
            if result.exception is not None:
                print(f'    exception:       {result.exception}')
            print(f'    elapsed:         {result.elapsed_s:.3f}s')


def cell_d_what_is_still_outside() -> None:
    print('\n  Outside the toolkit, stated so you can plan around it:')
    print('    gRPC      transport-layer misbehaviour only, via cells C/D —')
    print('              it rides HTTP/2, but an invalid grpc-status or a')
    print('              malformed length-prefixed message cannot be')
    print('              expressed.')
    print('    MQTT      none, and not planned.')
    print('    WebSocket none, in either direction.')


# ===========================================================================
# E. A scenario is data, not code
# ===========================================================================
#
# Every scenario above was written as Python, but it does not have to be.
# JSON Lines round-trips exactly, which is what makes a reproduction
# something you can attach to an issue or check into a fixture directory
# rather than describe in prose.
#
# Payloads are hex rather than base64 or an escaped string: a fault
# scenario's bytes are frequently not valid UTF-8, and hex is what you can
# hold next to a packet capture.

async def cell_e_scenarios_as_data() -> None:
    _heading('E', 'A scenario is data — save it, send it, replay it')

    original = CATALOGUE_H1['chunked_stops_mid_chunk']()
    as_json = scenario_h1_server_to_json(original)
    print('chunked_stops_mid_chunk, serialised:\n')
    for line in as_json.splitlines():
        print(f'  {line}')

    restored = scenario_h1_server_from_json(as_json)
    print(f'\n  round-trips exactly: {restored == original}')

    # Hand-written, never a Python object until now.
    # b'HTTP/1.1 20' — the status line stops mid-code.  Built with the
    # payload named rather than split across two adjacent literals: inside a
    # list, implicit concatenation and a missing comma look identical.
    half_a_status_line = '485454502f312e31203230'
    send_raw = (f'{{"op": "SEND_RAW", "data": "{half_a_status_line}", '
                f'"byte_interval": 0.0}}')
    handwritten = scenario_h1_server_from_json('\n'.join([
        '{"op": "HEADER", "name": "half_a_status_line"}',
        '{"op": "WAIT_FOR_REQUEST", "timeout": 5.0}',
        send_raw,
        '{"op": "CLOSE_GRACEFULLY"}',
    ]))
    print(f'\n  loaded from hand-written JSON: {handwritten.name}')
    async with H1FaultServer(handwritten) as srv:
        print(f'    blackbull: {await _drive_with_blackbull(srv)}')


async def main() -> None:
    print(__doc__.split('Run::')[0].strip())
    await cell_a_broken_client()
    await cell_b_broken_server()
    await cell_c_broken_h2_server()
    await cell_d_broken_h2_client()
    cell_d_what_is_still_outside()
    await cell_e_scenarios_as_data()
    print()


if __name__ == '__main__':
    asyncio.run(main())

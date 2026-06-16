"""Drive a real HTTP/1.1 server (stdlib :mod:`http.server`) through
deliberately misbehaving client scenarios.

This is the directional opposite of
``examples/scenario_h2_fault_injection.py``: that example pairs a
malformed server with a real client (httpx); this one pairs a
malformed client (``HTTP1Client.execute_scenario``) with a real
server (stdlib ``http.server.BaseHTTPRequestHandler`` running in a
background thread).  Both demonstrate how a real-world counterpart
reacts when one side of the wire deliberately breaks the protocol.

Run::

    python examples/scenario_h1_fault_injection.py

See ``docs/guide/fault_injection.md`` for a tutorial.
"""
from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from blackbull.client import HTTP1Client
from blackbull.fault_injection import (
    Abort,
    ReadResponse,
    Scenario,
    SendBytes,
    Sleep,
)


class _OkHandler(BaseHTTPRequestHandler):
    """Trivial handler — always returns 200 OK with a one-line body."""

    def do_GET(self) -> None:  # noqa: N802 — stdlib API name
        body = b'ok\n'
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # The Abort scenario RSTs mid-request; the response write
            # then hits a dead socket.  Swallow so the example output
            # stays clean.
            pass

    def log_message(self, format: str, *args) -> None:  # noqa: A002, ARG002
        # Silence the per-request stderr noise so the scenario output
        # is the only thing on the console.
        return


class _QuietHTTPServer(HTTPServer):
    """HTTPServer that suppresses the per-connection error tracebacks
    stdlib emits when a client RSTs mid-request."""

    def handle_error(self, request, client_address) -> None:  # noqa: ARG002
        return


def _start_stdlib_server() -> tuple[HTTPServer, int]:
    """Bind ``http.server`` on a random localhost port; serve in a thread."""
    httpd = _QuietHTTPServer(('127.0.0.1', 0), _OkHandler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, port


# A complete, well-formed request — the baseline against which the
# misbehaving scenarios below are differentials.
_WELL_FORMED = (
    b'GET / HTTP/1.1\r\n'
    b'Host: 127.0.0.1\r\n'
    b'Connection: close\r\n'
    b'\r\n'
)


SCENARIOS: dict[str, Scenario] = {
    # Baseline: send the bytes in one shot, read the response.
    'well_formed_request': Scenario(steps=(
        SendBytes(_WELL_FORMED),
        ReadResponse(timeout=2.0),
    )),
    # Slowloris: trickle the request one byte every 50 ms.  stdlib's
    # HTTPServer happily waits, so the request still completes — just
    # slowly.  A hardened production server (nginx, BlackBull) would
    # close on a request-header read timeout.
    'slowloris_trickle': Scenario(steps=(
        SendBytes(_WELL_FORMED, byte_interval=0.05),
        ReadResponse(timeout=5.0),
    )),
    # Partial headers + idle: send the request line + Host but not the
    # blank-line terminator, then sleep.  stdlib blocks reading more
    # header lines until the kernel-level recv times out (default:
    # never), so this scenario records a per-step read timeout on the
    # client side.
    'partial_headers_idle': Scenario(steps=(
        SendBytes(b'GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n'),
        Sleep(duration=1.5),
        ReadResponse(timeout=1.0),
    )),
    # Hard RST mid-request after writing only the request line.  An
    # Abort step short-circuits scenario execution; no read happens.
    'abort_after_request_line': Scenario(steps=(
        SendBytes(b'GET / HTTP/1.1\r\n'),
        Abort(),
    )),
}


async def drive_one(name: str, scenario: Scenario, port: int) -> None:
    print(f'--- {name} ---')
    async with HTTP1Client('127.0.0.1', port) as client:
        result = await client.execute_scenario(scenario)
    print(f'  steps completed: {result.steps_completed}')
    if result.response is not None:
        # response is a ClientResponse; we only print the status code
        # because the body is "ok\n" in every successful case.
        status = getattr(result.response, 'status', '?')
        print(f'  response status: {status}')
    print(f'  timed out:       {result.timed_out}')
    print(f'  aborted:         {result.aborted}')
    if result.exception is not None:
        print(f'  exception:       {result.exception}')
    print(f'  elapsed:         {result.elapsed_s:.3f}s')


async def main() -> None:
    httpd, port = _start_stdlib_server()
    print(f'stdlib http.server bound at http://127.0.0.1:{port}/\n')
    try:
        for name, scenario in SCENARIOS.items():
            await drive_one(name, scenario, port)
            print()
    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == '__main__':
    asyncio.run(main())

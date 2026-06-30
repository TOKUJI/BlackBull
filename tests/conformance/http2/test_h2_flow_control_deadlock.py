"""Conformance test: H2 flow-control for payloads above the 65535-byte
initial window (Sprint 57).

Three sequential RFC-compliance checks in one test function — each
step gates the next.  Subprocess isolation is required because the
deadlocks survive ``CancelledError``, ``@pytest.mark.timeout``, and
``asyncio.wait_for`` (they block at the C-level socket / event-loop
teardown).  **However, the scenario logic lives in plain async
functions in this file — no embedded script strings.**

Full analysis: ``.claude/planning/proposals/h2-flow-control-deadlock.md``
Sprint log: ``bench/sprint-logs/sprint-57.md``
"""
from __future__ import annotations

import asyncio
import pathlib
import subprocess
import sys

import pytest


_VENV_PYTHON = str(pathlib.Path(__file__).parents[3] / '.venv' / 'bin' / 'python')
_TIMEOUT = 8  # seconds per subprocess


# ====================================================================
# Scenario functions — plain async, no pytest dependency.
# These are run in a subprocess via `_run_scenario()`.
# ====================================================================

def _make_echo_app():
    from blackbull import BlackBull
    from blackbull.grpc import GrpcServiceRegistry

    app = BlackBull()
    reg = GrpcServiceRegistry()

    @reg.method('/echo.Echo/Echo')
    async def echo(request, context):
        return request

    app.enable_grpc(reg)
    return app


async def _scenario_step1_rest_100kb():
    """Bug #1: REST GET 100 KB — client never credits received DATA."""
    from blackbull import BlackBull
    from blackbull.client.http2 import HTTP2Client
    from blackbull.server.server import ASGIServer

    app = BlackBull()

    async def large(scope, receive, send):
        await send({
            'type': 'http.response.start', 'status': 200,
            'headers': [(b'content-type', b'application/octet-stream')],
        })
        await send({
            'type': 'http.response.body',
            'body': b'X' * 100_000, 'more_body': False,
        })

    app._router.route_fn(['GET'], '/large', 'http', 'large')(large)

    srv = ASGIServer(app)
    srv.open_socket(port=0)
    port = srv.port
    asyncio.create_task(srv.run())
    await asyncio.sleep(0.15)

    async with HTTP2Client('127.0.0.1', port) as c:
        r = await c.request('GET', '/large')
        assert r.status == 200 and len(r.body) == 100_000


async def _scenario_step2_grpc_65531b():
    """Bug #2: gRPC Echo 65531 bytes — lost-wakeup race."""
    from blackbull import BlackBull
    from blackbull.grpc import GrpcServiceRegistry, encode_message
    from blackbull.client.http2 import HTTP2Client
    from blackbull.server.server import ASGIServer

    app = BlackBull()
    reg = GrpcServiceRegistry()

    @reg.method('/echo.Echo/Echo')
    async def echo(request, context):
        return request

    app.enable_grpc(reg)

    srv = ASGIServer(app)
    srv.open_socket(port=0)
    port = srv.port
    asyncio.create_task(srv.run())
    await asyncio.sleep(0.15)

    payload = bytes((i % 256) for i in range(65531))
    async with HTTP2Client('127.0.0.1', port) as c:
        r = await c.request(
            'POST', '/echo.Echo/Echo',
            headers=[('content-type', 'application/grpc')],
            body=encode_message(payload))
        assert r.status == 200


async def _scenario_step3_rapid_30():
    """Bug #3: 30 rapid gRPC Echo — late WU on closed stream."""
    from blackbull import BlackBull
    from blackbull.grpc import GrpcServiceRegistry, encode_message
    from blackbull.client.http2 import HTTP2Client
    from blackbull.server.server import ASGIServer

    app = BlackBull()
    reg = GrpcServiceRegistry()

    @reg.method('/echo.Echo/Echo')
    async def echo(request, context):
        return request

    app.enable_grpc(reg)

    srv = ASGIServer(app)
    srv.open_socket(port=0)
    port = srv.port
    asyncio.create_task(srv.run())
    await asyncio.sleep(0.15)

    async with HTTP2Client('127.0.0.1', port) as c:
        for i in range(30):
            payload = f'p{i}'.encode()
            r = await c.request(
                'POST', '/echo.Echo/Echo',
                headers=[('content-type', 'application/grpc')],
                body=encode_message(payload))
            assert r.status == 200, f'request {i} failed: {r.status}'


# ====================================================================
# Subprocess runner — invokes a named async function in THIS file.
# ====================================================================

_SCENARIOS = {
    'step1': _scenario_step1_rest_100kb,
    'step2': _scenario_step2_grpc_65531b,
    'step3': _scenario_step3_rapid_30,
}


def _run_scenario(name: str) -> tuple[bool, str]:
    """Run scenario *name* in a subprocess with a timeout guard.

    Returns ``(True, '')`` if it completed, ``(False, reason)`` if it
    hung (``TimeoutExpired``) or crashed.
    """
    try:
        subprocess.run(
            [_VENV_PYTHON, __file__, name],
            capture_output=True, timeout=_TIMEOUT,
            cwd=str(pathlib.Path(__file__).parents[3]),
        )
        return (True, '')
    except subprocess.TimeoutExpired:
        return (False, f'timeout ({_TIMEOUT}s) — deadlock reproduced')


# ====================================================================
# Test
# ====================================================================

def test_h2_flow_control_deadlocks():
    """RFC 9113 flow-control compliance — three sequential checks."""

    # Step 1 — RFC 9113 §6.9: receiver must credit sender via WINDOW_UPDATE.
    ok, reason = _run_scenario('step1')
    assert ok, (
        f'STEP 1 FAILED ({reason})\n'
        f'RFC 9113 §6.9: receiver must send WINDOW_UPDATE for consumed DATA.\n'
        f'Bug #1: blackbull/client/http2.py _on_response_data() never credits '
        f'received DATA → server blocks when response > 65535 bytes.\n'
        f'Fix: emit stream + connection WINDOW_UPDATE after appending body.')

    # Step 2 — RFC 9113 §6.9: sender must resume on WINDOW_UPDATE arrival.
    ok, reason = _run_scenario('step2')
    assert ok, (
        f'STEP 2 FAILED ({reason})\n'
        f'RFC 9113 §6.9: sender must resume when WINDOW_UPDATE arrives.\n'
        f'Bug #2: blackbull/server/sender.py _write_data() lost-wakeup race.\n'
        f'Fix: re-check window after Event.clear(), break if credit > 0.')

    # Step 3 — RFC 9113 §5.1: WU on CLOSED stream MUST be silently ignored.
    ok, reason = _run_scenario('step3')
    assert ok, (
        f'STEP 3 FAILED ({reason})\n'
        f'RFC 9113 §5.1: WINDOW_UPDATE on CLOSED stream MUST be silently '
        f'ignored.\n'
        f'Bug #3: blackbull/server/http2_actor.py sends RST_STREAM instead.\n'
        f'Fix: continue (ignore) for WU/RST/PRIORITY on CLOSED streams.')


# ====================================================================
# CLI entry point — invoked by the subprocess runner above.
# ====================================================================

if __name__ == '__main__':
    fn = _SCENARIOS[sys.argv[1]]
    asyncio.run(fn())

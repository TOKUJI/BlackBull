"""Conformance test: H2 flow-control for payloads above the 65535-byte
initial window (Sprint 57).

Four sequential RFC-compliance checks in one test function — each
step gates the next.  Steps 1-3 pin the three fixed bugs at the window
boundary; step 4 adds a large *bidirectional* payload that forces
multiple WINDOW_UPDATE refills in both directions at once.  Subprocess
isolation is required because the
deadlocks survive ``CancelledError``, ``@pytest.mark.timeout``, and
``asyncio.wait_for`` (they block at the C-level socket / event-loop
teardown).  **However, the scenario logic lives in plain async
functions in this file — no embedded script strings.**

Full analysis: ``.claude/planning/archives/h2-flow-control-deadlock.md``
Sprint log: ``bench/sprint-logs/sprint-57.md``
"""
from __future__ import annotations

import asyncio
import pathlib
import subprocess
import sys

import pytest


# Reuse the interpreter running the test suite so the subprocess sees the same
# installed BlackBull.  ``sys.executable`` is the local ``.venv`` when run from
# there and the CI runner's system Python on GitHub Actions — no hardcoded
# ``.venv`` path (which doesn't exist on the runner's ``pip install -e``).
_SUBPROCESS_PYTHON = sys.executable
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


async def _scenario_step4_grpc_128kb_bidir():
    """Large *bidirectional* payload: gRPC Echo of 128 KiB.

    Steps 1-3 hit the deadlocks at the window boundary (step 2 is 65536
    bytes — a single WINDOW_UPDATE refill).  This step sends *and* receives
    128 KiB, forcing ~2 full window refills in **each** direction at once:
    the client must credit received DATA (bug #1) *while* the server's
    sender resumes on incoming WINDOW_UPDATEs (bug #2), with neither side
    able to drain the other in one window.  A regression in either
    direction's flow control wedges here even if the boundary cases pass.
    """
    from blackbull import BlackBull
    from blackbull.grpc import GrpcServiceRegistry, encode_message, decode_messages
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

    payload = bytes((i % 256) for i in range(131072))  # 128 KiB, ~2x the window
    async with HTTP2Client('127.0.0.1', port) as c:
        r = await c.request(
            'POST', '/echo.Echo/Echo',
            headers=[('content-type', 'application/grpc')],
            body=encode_message(payload))
        assert r.status == 200
        # The echoed body must survive intact — a partial/dropped refill
        # would truncate it even if the stream did not hang.
        assert decode_messages(r.body) == [(False, payload)]


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
    'step4': _scenario_step4_grpc_128kb_bidir,
}


def _run_scenario(name: str) -> tuple[bool, str]:
    """Run scenario *name* in a subprocess with a timeout guard.

    Returns ``(True, '')`` if it completed, ``(False, reason)`` if it
    hung (``TimeoutExpired``) or crashed.
    """
    try:
        subprocess.run(
            [_SUBPROCESS_PYTHON, __file__, name],
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

    # Step 4 — RFC 9113 §6.9: large *bidirectional* payload (128 KiB each
    # way) forces multiple WINDOW_UPDATE refills in both directions at once,
    # not just the single-refill boundary of steps 1-2.
    ok, reason = _run_scenario('step4')
    assert ok, (
        f'STEP 4 FAILED ({reason})\n'
        f'RFC 9113 §6.9: sustained bidirectional flow control — 128 KiB up '
        f'and 128 KiB down must both credit and resume repeatedly.\n'
        f'A regression in client crediting (bug #1) or sender resume '
        f'(bug #2) that survives the boundary cases deadlocks here.')


# ====================================================================
# CLI entry point — invoked by the subprocess runner above.
# ====================================================================

if __name__ == '__main__':
    fn = _SCENARIOS[sys.argv[1]]
    asyncio.run(fn())

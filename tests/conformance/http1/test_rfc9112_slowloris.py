"""Slowloris defence — RFC 9110 §15.5.9 (408 Request Timeout).

Slowloris is the canonical partial-data attack: open a connection, send
the headers byte by byte (or with long pauses), never send the
terminating CRLFCRLF.  Each held connection consumes one server slot.
With enough connections the server can't accept new ones.

BlackBull's defences are three timeouts in :mod:`blackbull.env`,
overridden short here so the suite stays fast:

  * ``BB_HEADER_TIMEOUT`` — deadline for the header block.
  * ``BB_BODY_TIMEOUT``   — deadline for the body (Sprint 17 Phase 3).
  * ``BB_KEEP_ALIVE_TIMEOUT`` — deadline for idle persistent connections.

Sprint 17 Phase 5 merged the previous ``test_http1_slowloris.py`` into
this file and rewrote every test on top of the
:class:`blackbull.client.Scenario` abstraction — the same shape the
Hypothesis-driven differential test and the atheris fuzz harness
consume, so wire-shaping logic lives in exactly one place.
"""
import asyncio
import os
import time
from multiprocessing import Process

import pytest
import pytest_asyncio

from blackbull.client import (
    HTTP1Client,
    ReadResponse,
    Scenario,
    ScenarioResult,
    SendBytes,
    Sleep,
)

from .conftest import _make_app


# ---------------------------------------------------------------------------
# Tight server-side timeouts so slowloris assertions complete inside the
# per-test pytest budget (--timeout=30).  Defaults are 10 / 30 / 60 s in
# `blackbull/env.py`; without overrides the test budget would not survive
# even one scenario.
# ---------------------------------------------------------------------------

_SHORT_HEADER_TIMEOUT = 1.0
_SHORT_BODY_TIMEOUT = 3.0
_SHORT_KEEP_ALIVE_TIMEOUT = 5.0


def _run_with_short_timeouts(server, env_overrides):
    """Subprocess entry point — apply env overrides before running."""
    for k, v in env_overrides.items():
        os.environ[k] = v
    # The parent may have already populated the get_settings() cache before
    # forking; the child inherits that stale snapshot.  Reset so the next
    # call re-reads the overridden env vars.
    from blackbull.env import reset_settings_cache
    reset_settings_cache()
    asyncio.run(server.run())


@pytest_asyncio.fixture
async def slow_app():
    """A BlackBull subprocess with all three timeouts shortened.

    Shared by every test in this module — single fixture instance so the
    process starts once, runs every scenario, then teardown.  Uses
    ASGIServer directly (not ``live_server``) because the worker needs
    custom env-var setup before invoking ``asyncio.run``.
    """
    from types import SimpleNamespace
    from blackbull.server import ASGIServer

    app = _make_app()
    server = ASGIServer(app)
    server.open_socket(0)
    p = Process(
        target=_run_with_short_timeouts,
        args=(server, {
            'BB_HEADER_TIMEOUT': str(_SHORT_HEADER_TIMEOUT),
            'BB_BODY_TIMEOUT': str(_SHORT_BODY_TIMEOUT),
            'BB_KEEP_ALIVE_TIMEOUT': str(_SHORT_KEEP_ALIVE_TIMEOUT),
        }),
    )
    p.start()
    try:
        server.wait_for_port(timeout=10.0)
        yield SimpleNamespace(app=app, port=server.port)
    finally:
        server.close()
        p.terminate()
        p.join(timeout=5)


# ---------------------------------------------------------------------------
# Scenario-runner helper — every test below builds a Scenario and pipes
# it through HTTP1Client.execute_scenario, the same code path the
# Hypothesis differential test and the atheris fuzz harness use.
# ---------------------------------------------------------------------------

async def _run(scenario: Scenario, port: int) -> ScenarioResult:
    async with HTTP1Client('127.0.0.1', port, connect_timeout=2.0) as c:
        return await c.execute_scenario(scenario)


def _server_closed(result: ScenarioResult) -> bool:
    """The server failed to return a complete response.

    Slowloris-defence assertions are tolerant of the exact wire-level
    signal — 408 then close, silent FIN, or RST all count as "server
    refused the partial input".  We treat the absence of a 2xx/3xx
    response as the success signal.
    """
    if result.response is None:
        return True
    # 4xx with closed connection also counts as "server refused".
    return 400 <= result.response.status < 500


# ---------------------------------------------------------------------------
# Header-timeout enforcement — the original RFC 9112 tests, rewritten
# on the Scenario abstraction.
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSlowlorisDefence:
    """Connections that never finish their header block must time out."""

    @pytest.mark.asyncio
    async def test_no_data_at_all_closed_within_deadline(self, slow_app):
        """Open a connection and send nothing.  Server must close (or 408)
        before our test timeout fires.

        Expressed as a one-step scenario: just ``ReadResponse`` against
        an empty pipe.  The server-initiated close manifests as the
        read raising ``ConnectionError`` (folded into ``result.exception``).
        """
        scenario = Scenario(steps=(
            ReadResponse(timeout=_SHORT_HEADER_TIMEOUT + 2.0),
        ))
        t0 = time.monotonic()
        result = await _run(scenario, slow_app.port)
        elapsed = time.monotonic() - t0

        assert _server_closed(result), (
            f'expected server-initiated close on idle connection; '
            f'got response={result.response!r}'
        )
        assert elapsed < _SHORT_HEADER_TIMEOUT + 1.5, (
            f'connection still alive after {elapsed:.2f}s — '
            f'slowloris defence either disabled or too slow '
            f'(BB_HEADER_TIMEOUT={_SHORT_HEADER_TIMEOUT})'
        )

    @pytest.mark.asyncio
    async def test_partial_request_line_closed_within_deadline(self, slow_app):
        """Send some bytes but never the terminating CRLFCRLF.  The
        header deadline runs from when the server starts the
        ``readuntil`` — total elapsed must be bounded."""
        scenario = Scenario(steps=(
            SendBytes(data=b'GET / HTTP'),          # partial request line
            Sleep(duration=0.3),
            SendBytes(data=b'/1.1\r\nHost: x\r\n'),  # partial header block
            ReadResponse(timeout=_SHORT_HEADER_TIMEOUT + 2.0),
        ))
        t0 = time.monotonic()
        result = await _run(scenario, slow_app.port)
        elapsed = time.monotonic() - t0

        assert _server_closed(result), (
            f'expected server close on unfinished headers; '
            f'got response={result.response!r}'
        )
        assert elapsed < _SHORT_HEADER_TIMEOUT + 2.0, (
            f'still alive after {elapsed:.2f}s — slowloris defence inactive'
        )

    @pytest.mark.asyncio
    async def test_complete_request_within_deadline_is_fine(self, slow_app):
        """The deadline is for *unfinished* requests.  A legitimate
        request sent in fragments — but completing within the budget —
        must succeed."""
        scenario = Scenario(steps=(
            SendBytes(data=b'GET / '),
            Sleep(duration=0.1),
            SendBytes(data=b'HTTP/1.1\r\n'),
            Sleep(duration=0.1),
            SendBytes(data=b'Host: localhost\r\n\r\n'),
            ReadResponse(timeout=2.0),
        ))
        result = await _run(scenario, slow_app.port)
        assert result.response is not None and result.response.status == 200, (
            f'fragmented-but-complete request must succeed; result={result!r}'
        )


@pytest.mark.integration
class TestIncrementalRequest:
    """Robustness under partial-data conditions (legitimate slow clients).

    Sending the request one byte at a time must produce a correct
    response — this exercises the actor's ``readuntil`` loop under
    maximally fragmented input.  Phase 5: now expressed via
    ``SendBytes(..., byte_interval=...)`` instead of a raw per-byte
    socket loop.
    """

    @pytest.mark.asyncio
    async def test_byte_by_byte_request_processed_correctly(self, slow_app):
        request = (
            b'POST /echo HTTP/1.1\r\n'
            b'Host: localhost\r\n'
            b'Content-Length: 5\r\n\r\n'
            b'hello'
        )
        # 1 ms between bytes — keeps the test wall-clock under
        # BB_HEADER_TIMEOUT while still forcing per-byte writes.
        scenario = Scenario(steps=(
            SendBytes(data=request, byte_interval=0.001),
            ReadResponse(timeout=2.0),
        ))
        result = await _run(scenario, slow_app.port)
        assert result.response is not None, (
            f'byte-by-byte request must complete; result={result!r}'
        )
        assert result.response.status == 200
        assert result.response.body == b'hello'


# ---------------------------------------------------------------------------
# Sprint 17 Phase 5 additions — body-timeout, keep-alive timeout, and
# CL.CL / CL+TE smuggling defences.
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestBodyTrickle:
    """Send the full header block at once, then trickle the body one
    byte every 500 ms.  The server should close after
    ``BB_BODY_TIMEOUT`` elapses even though Content-Length says more
    is coming.  Phase 3 added BB_BODY_TIMEOUT specifically for this."""

    @pytest.mark.asyncio
    async def test_body_trickle_triggers_server_close(self, slow_app):
        scenario = Scenario(steps=(
            SendBytes(
                data=b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Length: 100\r\n\r\n',
            ),
            SendBytes(data=b'x' * 100, byte_interval=0.5),
            ReadResponse(timeout=_SHORT_BODY_TIMEOUT + 3.0),
        ))
        t0 = time.monotonic()
        result = await _run(scenario, slow_app.port)
        elapsed = time.monotonic() - t0

        assert _server_closed(result), (
            f'expected body-timeout close, got response={result.response!r}'
        )
        # The trickle would take 50 s at full speed; assert we got out
        # well before that.  Upper bound is generous because the client
        # only observes the server-side close on its next write() after
        # TCP propagates RST — typically several bytes after the timeout.
        assert elapsed < _SHORT_BODY_TIMEOUT + 15.0, (
            f'server took {elapsed:.2f}s to close; expected well below '
            f'the full-speed trickle time of 50 s'
        )


@pytest.mark.integration
class TestKeepAliveIdle:
    """The server should close the connection if the client goes silent
    for longer than ``BB_KEEP_ALIVE_TIMEOUT`` after a full
    request/response cycle."""

    @pytest.mark.asyncio
    async def test_post_crlf_idle_triggers_server_close(self, slow_app):
        # Full request, no response read for keep_alive_timeout + 1.5 s.
        # After the idle the *next* request on the same connection must
        # fail because the server closed.
        scenario = Scenario(steps=(
            SendBytes(data=b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n'),
            Sleep(duration=_SHORT_KEEP_ALIVE_TIMEOUT + 1.5),
            # First read picks up the buffered first response (server
            # answered before closing).
            ReadResponse(timeout=2.0),
            # Second request on the (now-closed) connection.
            SendBytes(data=b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n'),
            ReadResponse(timeout=2.0),
        ))
        result = await _run(scenario, slow_app.port)
        # Either the scenario didn't run to completion (executor caught
        # a transport error mid-step) or the final ReadResponse came
        # back with no response.
        all_succeeded = (result.steps_completed == len(scenario.steps)
                         and result.response is not None
                         and result.exception is None)
        assert not all_succeeded, (
            f'expected server close after idle; result={result!r}'
        )

    @pytest.mark.asyncio
    async def test_mid_keepalive_idle_triggers_server_close(self, slow_app):
        # Same shape but the first response IS read before the idle —
        # tests that the keep-alive timer also fires when the connection
        # has been used (not just freshly accepted).
        scenario = Scenario(steps=(
            SendBytes(data=b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n'),
            ReadResponse(timeout=2.0),
            Sleep(duration=_SHORT_KEEP_ALIVE_TIMEOUT + 1.5),
            SendBytes(data=b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n'),
            ReadResponse(timeout=2.0),
        ))
        result = await _run(scenario, slow_app.port)
        all_succeeded = (result.steps_completed == len(scenario.steps)
                         and result.response is not None
                         and result.exception is None)
        assert not all_succeeded, (
            f'expected server close after keep-alive idle; result={result!r}'
        )


@pytest.mark.integration
class TestSmugglingRejected:
    """Duplicate or conflicting framing headers MUST be rejected with
    400 (or a connection close) — this is the CL.CL / CL+TE smuggling
    defence in ``http1_actor.py``.  Sending the headers via SendBytes
    bypasses the client's own dedup so we can verify the server's
    check directly."""

    @pytest.mark.asyncio
    async def test_duplicate_content_length_rejected(self, slow_app):
        scenario = Scenario(steps=(
            SendBytes(
                data=b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Length: 5\r\n'
                     b'Content-Length: 10\r\n'
                     b'\r\n'
                     b'hello',
            ),
            ReadResponse(timeout=2.0),
        ))
        result = await _run(scenario, slow_app.port)
        assert _server_closed(result), (
            f'duplicate Content-Length must be rejected; result={result!r}'
        )

    @pytest.mark.asyncio
    async def test_conflicting_cl_te_rejected(self, slow_app):
        # RFC 9112 §6.3 — when both Transfer-Encoding and Content-Length
        # are present, the server MUST close the connection (or reject)
        # to defend against request smuggling.
        scenario = Scenario(steps=(
            SendBytes(
                data=b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Length: 5\r\n'
                     b'Transfer-Encoding: chunked\r\n'
                     b'\r\n'
                     b'0\r\n\r\n',
            ),
            ReadResponse(timeout=2.0),
        ))
        result = await _run(scenario, slow_app.port)
        assert _server_closed(result), (
            f'CL+TE conflict must be rejected; result={result!r}'
        )

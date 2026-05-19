"""Slowloris defence (RFC 9110 §15.5.9 — 408 Request Timeout) tests.

Slowloris is the canonical partial-data attack: open a connection, send
the headers byte by byte (or with long pauses), never send the
terminating CRLFCRLF.  Each held connection consumes one server slot.
With enough connections the server can't accept new ones.

BlackBull's defence is a deadline on the header-read phase, configured
via ``BB_HEADER_TIMEOUT`` (default 10 s).  These tests use a short
override so the suite stays fast.
"""
import os
import socket
import time
from multiprocessing import Process

import asyncio
import pytest
import pytest_asyncio

from blackbull import BlackBull

from .conftest import _make_app, open_socket, parse_response


def _run_with_short_timeout(app, env_overrides):
    """Subprocess entry point — apply env overrides before running."""
    for k, v in env_overrides.items():
        os.environ[k] = v
    asyncio.run(app.run())


@pytest_asyncio.fixture
async def slow_app():
    """A short-header-timeout BlackBull so the slowloris tests run in seconds."""
    app = _make_app()
    app.create_server(port=0)
    p = Process(
        target=_run_with_short_timeout,
        args=(app, {'BB_HEADER_TIMEOUT': '1.0'}),
    )
    p.start()
    app.wait_for_port(timeout=10.0)
    yield app
    app.stop()
    p.terminate()
    p.join(timeout=5)


@pytest.mark.integration
class TestSlowlorisDefence:
    """Connections that never finish their header block must time out."""

    def test_no_data_at_all_closed_within_deadline(self, slow_app):
        """Open a connection and send nothing.  Server must close (or 408)
        before our test timeout fires."""
        s = open_socket('127.0.0.1', slow_app.port, timeout=5)
        try:
            start = time.monotonic()
            # Don't send anything; just wait for the server to give up.
            data = b''
            while time.monotonic() - start < 3.0:
                try:
                    s.settimeout(3.0 - (time.monotonic() - start))
                    chunk = s.recv(4096)
                except (socket.timeout, TimeoutError):
                    break
                if not chunk:
                    break
                data += chunk
            elapsed = time.monotonic() - start
        finally:
            s.close()

        assert elapsed < 2.5, (
            f'connection still alive after {elapsed:.2f}s — slowloris defence '
            f'either disabled or too slow (BB_HEADER_TIMEOUT was 1.0s)')
        # Either 408 sent then close, OR straight TCP close — both are acceptable.
        if data:
            r = parse_response(data, closed=True)
            assert r.status in (408, None), (
                f'expected 408 (or silent close); got {r.status}')

    def test_partial_request_line_closed_within_deadline(self, slow_app):
        """Send some bytes but never the terminating CRLFCRLF.  The
        deadline runs from the first byte (well: from when readuntil
        starts) — total elapsed must still be bounded."""
        s = open_socket('127.0.0.1', slow_app.port, timeout=5)
        try:
            start = time.monotonic()
            s.sendall(b'GET / HTTP')          # partial request line
            time.sleep(0.3)
            s.sendall(b'/1.1\r\nHost: x\r\n')  # one more partial header
            # Now stop; deadline should fire within ~1s of the call start.
            data = b''
            while time.monotonic() - start < 3.0:
                try:
                    s.settimeout(3.0 - (time.monotonic() - start))
                    chunk = s.recv(4096)
                except (socket.timeout, TimeoutError):
                    break
                if not chunk:
                    break
                data += chunk
            elapsed = time.monotonic() - start
        finally:
            s.close()

        assert elapsed < 2.5, (
            f'still alive after {elapsed:.2f}s — slowloris defence inactive')
        if data:
            r = parse_response(data, closed=True)
            assert r.status in (408, 400, None)

    def test_complete_request_within_deadline_is_fine(self, slow_app):
        """The deadline is for *unfinished* requests.  A legitimate request
        sent in fragments — but completing within the budget — must succeed."""
        s = open_socket('127.0.0.1', slow_app.port, timeout=5)
        try:
            # Send the request in three pieces with small pauses; total
            # well under the 1-second deadline.
            s.sendall(b'GET / ')
            time.sleep(0.1)
            s.sendall(b'HTTP/1.1\r\n')
            time.sleep(0.1)
            s.sendall(b'Host: localhost\r\n\r\n')
            data = b''
            while True:
                try:
                    s.settimeout(2.0)
                    chunk = s.recv(4096)
                except (socket.timeout, TimeoutError):
                    break
                if not chunk:
                    break
                data += chunk
        finally:
            s.close()

        r = parse_response(data, closed=True)
        assert r.status == 200, (
            f'fragmented-but-complete request must succeed; got {r.status}')


@pytest.mark.integration
class TestIncrementalRequest:
    """Robustness under partial-data conditions (legitimate slow clients).

    Sending the request one byte at a time must produce a correct response
    — this exercises the actor's ``readuntil`` loop under maximally
    fragmented input.
    """

    def test_byte_by_byte_request_processed_correctly(self, slow_app):
        request = (
            b'POST /echo HTTP/1.1\r\n'
            b'Host: localhost\r\n'
            b'Content-Length: 5\r\n\r\n'
            b'hello'
        )
        s = open_socket('127.0.0.1', slow_app.port, timeout=5)
        try:
            for byte in request:
                s.sendall(bytes([byte]))
                # No sleep — just maximally fragmented `send`s.  The OS may
                # coalesce, but the server side sees TCP segment fragments.
            data = b''
            while True:
                try:
                    s.settimeout(2.0)
                    chunk = s.recv(4096)
                except (socket.timeout, TimeoutError):
                    break
                if not chunk:
                    break
                data += chunk
        finally:
            s.close()

        r = parse_response(data, closed=True)
        assert r.status == 200
        assert r.body == b'hello'

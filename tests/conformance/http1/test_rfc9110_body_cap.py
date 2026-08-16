"""Request-body size cap — RFC 9110 §15.5.14 (413 Content Too Large).

Without a cap the peer chooses how much memory a request costs: the per-read
bound (``BB_BODY_CHUNK_MAX``) limits what one read materialises, never the sum
of them, and ``conn.body()`` accumulates whatever arrives.  ``BB_MAX_BODY_SIZE``
puts a ceiling on the total, enforced differently on each framing because the
framings tell us different things:

* ``Content-Length`` declares the whole body in the head → refused **before a
  body octet is read**.  That is the property worth testing: the answer must
  arrive without the client sending any of the body at all.
* ``chunked`` declares nothing → counted as it arrives, refused the moment the
  running total passes the cap.

Both refusals close the connection.  The octets we declined are still on their
way, so whatever follows them in the stream is the peer's to choose — serving
it as the next request is the request-smuggling shape, and one of the tests
below sends exactly that.

The cap is set to 1 KiB for this module so the assertions cost bytes, not
megabytes.
"""
from __future__ import annotations

import asyncio
import os
from multiprocessing import Process
from types import SimpleNamespace

import pytest

from .conftest import _make_app, open_socket, parse_response, read_until_eof

_CAP = 1024


def _run_with_body_cap(server, env_overrides):
    """Subprocess entry point — apply env overrides before running."""
    for k, v in env_overrides.items():
        os.environ[k] = v
    # The parent may have already populated the get_settings() cache before
    # forking; the child inherits that stale snapshot.
    from blackbull.env import reset_settings_cache
    reset_settings_cache()
    asyncio.run(server.run())


@pytest.fixture(scope='module')
def capped_app():
    """A live BlackBull server with ``BB_MAX_BODY_SIZE`` set to 1 KiB."""
    from blackbull.server import ASGIServer

    app = _make_app()
    server = ASGIServer(app)
    server.open_socket(0)
    p = Process(target=_run_with_body_cap,
                args=(server, {'BB_MAX_BODY_SIZE': str(_CAP)}))
    p.start()
    try:
        server.wait_for_port(timeout=10.0)
        yield SimpleNamespace(app=app, port=server.port)
    finally:
        server.close()
        p.terminate()
        p.join(timeout=5)


def _head(extra: bytes = b'', *, length: int | None = None,
          chunked: bool = False, path: bytes = b'/echo') -> bytes:
    framing = b'Transfer-Encoding: chunked\r\n' if chunked else \
        b'Content-Length: %d\r\n' % length
    return (b'POST ' + path + b' HTTP/1.1\r\nHost: x\r\n' + framing
            + extra + b'\r\n')


class TestDeclaredBodyOverCap:
    """``Content-Length`` says how big it will be, so nothing has to arrive."""

    def test_oversized_content_length_is_refused_before_the_body_is_sent(
            self, capped_app):
        # The whole point of a head-time refusal: the client sends *only* the
        # head — not one octet of the declared body — and still gets an answer.
        # A server that waited for the body would time out here instead.
        sock = open_socket('127.0.0.1', capped_app.port, timeout=5.0)
        try:
            sock.sendall(_head(length=_CAP * 100))
            raw = read_until_eof(sock)
        finally:
            sock.close()
        resp = parse_response(raw, closed=True)
        assert resp.status == 413, raw[:200]

    def test_the_connection_closes_after_the_refusal(self, capped_app):
        sock = open_socket('127.0.0.1', capped_app.port, timeout=5.0)
        try:
            sock.sendall(_head(length=_CAP * 100))
            raw = read_until_eof(sock)
            # EOF, not merely a response: read_until_eof stops on either, so
            # the discriminator is that a second recv sees the close.
            assert sock.recv(1) == b''
        finally:
            sock.close()
        assert parse_response(raw, closed=True).status == 413

    def test_a_declared_body_within_the_cap_is_served(self, capped_app):
        # The cap must not be a general body ban: the boundary case one byte
        # under it round-trips normally.
        body = b'z' * (_CAP - 1)
        sock = open_socket('127.0.0.1', capped_app.port, timeout=5.0)
        try:
            sock.sendall(_head(length=len(body)) + body)
            raw = read_until_eof(sock)
        finally:
            sock.close()
        resp = parse_response(raw, closed=True)
        assert resp.status == 200
        assert resp.body == body

    def test_expect_100_continue_gets_the_413_instead_of_the_interim(
            self, capped_app):
        """RFC 9110 §10.1.1 — answer the final status, not ``100 Continue``.

        Telling a peer to go ahead and send a body we have already decided to
        refuse is the one thing the Expect handshake exists to prevent.
        """
        sock = open_socket('127.0.0.1', capped_app.port, timeout=5.0)
        try:
            sock.sendall(_head(b'Expect: 100-continue\r\n',
                               length=_CAP * 100))
            raw = read_until_eof(sock)
        finally:
            sock.close()
        assert b'100 Continue' not in raw, raw[:200]
        assert parse_response(raw, closed=True).status == 413


class TestChunkedBodyOverCap:
    """``chunked`` declares nothing, so the cap is enforced on the octets."""

    @staticmethod
    def _chunk(payload: bytes) -> bytes:
        return b'%x\r\n%s\r\n' % (len(payload), payload)

    def test_running_total_over_the_cap_is_refused_mid_stream(self, capped_app):
        # Each chunk is legal on its own; it is their sum that is not.  A cap
        # that only looked at one read would let this through.
        sock = open_socket('127.0.0.1', capped_app.port, timeout=5.0)
        try:
            sock.sendall(_head(chunked=True)
                         + self._chunk(b'a' * 700)
                         + self._chunk(b'b' * 700)
                         + b'0\r\n\r\n')
            raw = read_until_eof(sock)
        finally:
            sock.close()
        assert parse_response(raw, closed=True).status == 413, raw[:200]

    def test_a_chunked_body_within_the_cap_is_served(self, capped_app):
        sock = open_socket('127.0.0.1', capped_app.port, timeout=5.0)
        try:
            sock.sendall(_head(chunked=True)
                         + self._chunk(b'a' * 500)
                         + self._chunk(b'b' * 400)
                         + b'0\r\n\r\n')
            raw = read_until_eof(sock)
        finally:
            sock.close()
        resp = parse_response(raw, closed=True)
        assert resp.status == 200
        assert resp.body == b'a' * 500 + b'b' * 400


class TestARefusalIsNotASmugglingWindow:
    """What follows a refused body is attacker-chosen, so nothing follows it."""

    def test_bytes_after_a_refused_declared_body_are_never_served(
            self, capped_app):
        # The classic shape: an over-cap request whose "body" is a second,
        # perfectly well-formed request.  If the server refuses the first and
        # then keeps the connection, it reads the smuggled one out of the
        # octets it declined — and a front end that framed the pair
        # differently disagrees about how many requests were sent.
        smuggled = b'GET / HTTP/1.1\r\nHost: x\r\n\r\n'
        sock = open_socket('127.0.0.1', capped_app.port, timeout=5.0)
        try:
            sock.sendall(_head(length=_CAP * 100) + smuggled)
            raw = read_until_eof(sock)
        finally:
            sock.close()
        assert parse_response(raw, closed=True).status == 413
        assert raw.count(b'HTTP/1.1 ') == 1, (
            f'the refused body was parsed as a second request: {raw[:400]!r}')
        assert b'ok' not in raw

    def test_bytes_after_a_refused_chunked_body_are_never_served(
            self, capped_app):
        smuggled = b'GET / HTTP/1.1\r\nHost: x\r\n\r\n'
        sock = open_socket('127.0.0.1', capped_app.port, timeout=5.0)
        try:
            sock.sendall(_head(chunked=True)
                         + TestChunkedBodyOverCap._chunk(b'a' * 700)
                         + TestChunkedBodyOverCap._chunk(b'b' * 700)
                         + b'0\r\n\r\n' + smuggled)
            raw = read_until_eof(sock)
        finally:
            sock.close()
        assert parse_response(raw, closed=True).status == 413
        assert raw.count(b'HTTP/1.1 ') == 1, (
            f'the refused body was parsed as a second request: {raw[:400]!r}')
        assert b'ok' not in raw


def test_a_body_free_request_is_unaffected(capped_app):
    """The cap must cost a GET nothing — it is a body-path guard only."""
    sock = open_socket('127.0.0.1', capped_app.port, timeout=5.0)
    try:
        sock.sendall(b'GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n')
        raw = read_until_eof(sock)
    finally:
        sock.close()
    resp = parse_response(raw, closed=True)
    assert resp.status == 200
    assert resp.body == b'ok'

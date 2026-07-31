"""RFC 9112 §9 — Connection Management conformance.

* §9.1 — keep-alive is the default for HTTP/1.1.  ``Connection: close``
  on either side terminates the connection after the response.
* §9.2 — connection close MUST come from the server with a final
  response (not mid-message).
* §9.3 — pipelining is allowed but rarely seen in practice; we MUST
  process pipelined requests in order.
* HTTP/1.0 clients default to non-persistent unless they send
  ``Connection: keep-alive``.

These tests open a single socket and exchange multiple requests on it
to validate the persistent-connection contract.
"""
import socket

import pytest

from .conftest import open_socket, parse_response


@pytest.mark.integration
class TestPersistentByDefault:
    """RFC 9112 §9.1 — HTTP/1.1 connections are persistent by default."""

    def test_two_requests_on_one_socket(self, h1_app):
        s = open_socket('127.0.0.1', h1_app.port)
        try:
            for _ in range(2):
                s.sendall(b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n')
                # Read one full response.  /'s body is "ok" so it's 2 bytes.
                buf = b''
                while b'\r\n\r\n' not in buf:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                # Read body if Content-Length was sent.
                head, _, body = buf.partition(b'\r\n\r\n')
                cl = 0
                for ln in head.split(b'\r\n'):
                    if ln.lower().startswith(b'content-length:'):
                        cl = int(ln.split(b':', 1)[1].strip())
                        break
                while len(body) < cl:
                    body += s.recv(4096)
                r = parse_response(buf + body[len(body) - cl:])
                assert r.status == 200, (
                    f'persistent connection broken; got {r.status}')
        finally:
            s.close()


@pytest.mark.integration
class TestConnectionClose:
    """RFC 9112 §9.1 — ``Connection: close`` on the request signals the
    last request on this connection.  The response MUST also include
    ``Connection: close`` and the server MUST close after."""

    def test_request_with_connection_close_gets_close_response(self, h1_app):
        s = open_socket('127.0.0.1', h1_app.port)
        try:
            s.sendall(b'GET / HTTP/1.1\r\n'
                      b'Host: localhost\r\n'
                      b'Connection: close\r\n\r\n')
            buf = b''
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            r = parse_response(buf, closed=True)
            assert r.status == 200
            close_hdr = r.header(b'connection')
            assert close_hdr is None or close_hdr.lower() == b'close', (
                f'server response should not contradict the close request; '
                f'got Connection: {close_hdr!r}')
            assert r.closed, 'server must close after Connection: close'
        finally:
            s.close()


@pytest.mark.integration
class TestPipelining:
    """RFC 9112 §9.3 — pipelined requests MUST be served in order.

    Pipelining is rare in modern clients (browsers disabled it years
    ago) but the spec still requires it.  Tests two GETs queued before
    the first response arrives.
    """

    def test_pipelined_requests_served_in_order(self, h1_app):
        s = open_socket('127.0.0.1', h1_app.port, timeout=5)
        try:
            s.sendall(
                b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n'
                b'POST /echo HTTP/1.1\r\nHost: localhost\r\n'
                b'Content-Length: 5\r\n\r\nhello'
            )
            buf = b''
            try:
                # Read until both responses + their bodies have arrived.
                # /ok body is 2 bytes, echo body is 5 bytes; together ~250 bytes
                # of wire data.  Two seconds is plenty.
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    if buf.count(b'\r\n\r\n') >= 2 and b'hello' in buf:
                        # both response bodies arrived
                        break
            except (socket.timeout, TimeoutError):
                pass
            # Split into two HTTP responses on the wire — find the second
            # ``HTTP/1.1 `` marker.
            second = buf.find(b'HTTP/1.1 ', 8)
            assert second > 0, (
                f'second response missing — pipelining broken; got {buf!r}')
            first = parse_response(buf[:second])
            rest = parse_response(buf[second:])
            assert first.status == 200
            assert first.body.startswith(b'ok')
            assert rest.status == 200
            assert b'hello' in rest.body
        finally:
            s.close()


def _drain(s: socket.socket, seconds: float = 2.0) -> bytes:
    """Read until EOF or *seconds* elapse, whichever comes first.

    The asterisk-form cases below need to observe *absence* — that no second
    response follows — so they cannot stop at a response boundary.
    """
    s.settimeout(seconds)
    buf = b''
    while True:
        try:
            chunk = s.recv(4096)
        except (socket.timeout, TimeoutError):
            break
        if not chunk:
            break
        buf += chunk
    return buf


@pytest.mark.integration
class TestAsteriskFormKeepAlive:
    """RFC 9112 §3.2.4 + §9.1 — ``OPTIONS *`` is answered at the server level
    without routing, and the connection stays under the ordinary keep-alive
    contract afterwards.

    The server-wide answer is a separate branch of the keep-alive loop from
    the routed one, so the persistent-connection contract has to be asserted
    for it independently: a request that never reaches a handler must still
    leave the connection able to serve the next one.
    """

    def test_asterisk_then_pipelined_get(self, h1_app):
        # Server-wide OPTIONS must not consume the pipelined request behind it.
        s = open_socket('127.0.0.1', h1_app.port, timeout=5)
        try:
            s.sendall(
                b'OPTIONS * HTTP/1.1\r\nHost: localhost\r\n\r\n'
                b'GET / HTTP/1.1\r\nHost: localhost\r\n'
                b'Connection: close\r\n\r\n'
            )
            buf = _drain(s)
        finally:
            s.close()

        second = buf.find(b'HTTP/1.1 ', 8)
        assert second > 0, (
            f'no second response — keep-alive broken after OPTIONS *; {buf!r}')
        first = parse_response(buf[:second])
        rest = parse_response(buf[second:], closed=True)
        assert first.status == 204
        assert first.header(b'allow') is not None
        assert rest.status == 200, f'GET after OPTIONS * failed: {rest!r}'
        assert rest.body.startswith(b'ok')

    def test_asterisk_with_body_leaves_body_undrained_known_gap(self, h1_app):
        """KNOWN GAP — 405 here is *not* correct behaviour, it is today's.

        The server-wide answer replies without reading the request body, and
        nothing drains it, so the leftover bytes are parsed as the start of the
        next request: ``body`` + ``GET / HTTP/1.1`` becomes the method
        ``bodyGET``, which no route allows.  A drained path would answer 200.

        This asserts the status quo so that the surrounding keep-alive loop can
        be restructured without silently changing it.  When the drain gap is
        fixed, this test is *expected* to fail — update it deliberately rather
        than reading 405 as the contract.
        """
        s = open_socket('127.0.0.1', h1_app.port, timeout=5)
        try:
            s.sendall(
                b'OPTIONS * HTTP/1.1\r\nHost: localhost\r\n'
                b'Content-Length: 4\r\n\r\n'
                b'body'
                b'GET / HTTP/1.1\r\nHost: localhost\r\n'
                b'Connection: close\r\n\r\n'
            )
            buf = _drain(s)
        finally:
            s.close()

        second = buf.find(b'HTTP/1.1 ', 8)
        assert second > 0, f'no second response; got {buf!r}'
        first = parse_response(buf[:second])
        rest = parse_response(buf[second:], closed=True)
        assert first.status == 204
        assert rest.status == 405, (
            f'undrained-body framing changed (was 405); got {rest.status}')

    def test_asterisk_then_bare_crlf_closes(self, h1_app):
        # A bare CRLF where the next request-line belongs terminates the
        # connection — one response, then close.
        s = open_socket('127.0.0.1', h1_app.port, timeout=5)
        try:
            s.sendall(
                b'OPTIONS * HTTP/1.1\r\nHost: localhost\r\n\r\n'
                b'\r\n'
            )
            buf = _drain(s)
        finally:
            s.close()

        assert buf.count(b'HTTP/1.1 ') == 1, (
            f'expected exactly one response before close; got {buf!r}')
        assert parse_response(buf, closed=True).status == 204

    def test_asterisk_with_connection_close(self, h1_app):
        # §9.1 — Connection: close on the OPTIONS * itself ends the connection
        # after the server-wide answer.
        s = open_socket('127.0.0.1', h1_app.port, timeout=5)
        try:
            s.sendall(
                b'OPTIONS * HTTP/1.1\r\nHost: localhost\r\n'
                b'Connection: close\r\n\r\n'
            )
            buf = _drain(s)
        finally:
            s.close()

        assert buf.count(b'HTTP/1.1 ') == 1, (
            f'expected exactly one response; got {buf!r}')
        assert parse_response(buf, closed=True).status == 204

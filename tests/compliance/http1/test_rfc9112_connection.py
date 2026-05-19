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

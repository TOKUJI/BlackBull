"""RFC 9112 §9.3 — pipelining edge cases (with body-framing interactions).

Plain GET+GET pipelining is exercised in ``test_rfc9112_connection.py``.
This file focuses on the harder cases: a request *with a body* pipelined
ahead of a second request on the same socket.  Mishandling these is
where smuggling vectors hide — the server has to find the second
request's start-line at exactly the right byte offset, which means
correctly framing the first request's body.

Mirrors h2o's ``t/40http1-pipeline.t``.
"""
import socket

import pytest

from .conftest import open_socket, parse_response, read_until_eof


def _read_two_responses(s: socket.socket, timeout: float = 3.0) -> bytes:
    """Drain the socket until at least two ``HTTP/1.1 `` markers are visible
    or the peer EOFs / the per-recv timeout fires."""
    s.settimeout(timeout)
    buf = b''
    while True:
        try:
            chunk = s.recv(4096)
        except (socket.timeout, TimeoutError):
            break
        if not chunk:
            break
        buf += chunk
        # Two responses are present when we have two start-lines AND a
        # second body has had time to land.  ``HTTP/1.1 `` count >= 2 is
        # the cheapest indicator; with Connection: close on the second
        # request the peer closes after, which also breaks us out.
        if buf.count(b'HTTP/1.1 ') >= 2 and (
            buf.endswith(b'\r\n\r\n') or b'\r\n\r\nok' in buf):
            # Try one more recv with a tiny grace window so any final body
            # bytes that lagged behind the response headers arrive too.
            s.settimeout(0.3)
            try:
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
            except (socket.timeout, TimeoutError):
                pass
            break
    return buf


def _split_two_responses(buf: bytes) -> tuple[bytes, bytes]:
    """Locate the second ``HTTP/1.1 ``-prefixed response and split."""
    second = buf.find(b'HTTP/1.1 ', 8)
    assert second > 0, (
        f'second response missing — pipelining broken; got {buf!r}')
    return buf[:second], buf[second:]


@pytest.mark.integration
class TestPipelinedBodyFraming:
    """The first request's body MUST end at exactly the right byte so the
    second request's start-line is parsed correctly."""

    def test_post_content_length_then_get(self, h1_app):
        """POST with Content-Length, then GET on the same socket.  The
        server must find the GET right after the 5 bytes of body — not
        eat extra bytes, not stop short."""
        s = open_socket('127.0.0.1', h1_app.port, timeout=5)
        try:
            s.sendall(
                b'POST /echo HTTP/1.1\r\n'
                b'Host: localhost\r\n'
                b'Content-Length: 5\r\n\r\n'
                b'hello'
                b'GET / HTTP/1.1\r\n'
                b'Host: localhost\r\n'
                b'Connection: close\r\n\r\n'
            )
            buf = _read_two_responses(s)
        finally:
            s.close()

        first, second = _split_two_responses(buf)
        r1 = parse_response(first)
        r2 = parse_response(second, closed=True)
        assert r1.status == 200, f'first response: {r1!r}'
        assert b'hello' in r1.body, f'echo did not return body: {r1.body!r}'
        assert r2.status == 200, f'second response: {r2!r}'
        assert r2.body.startswith(b'ok'), f'second body: {r2.body!r}'

    def test_post_chunked_then_get(self, h1_app):
        """POST with chunked encoding then GET on the same socket.  The
        chunked-encoding parser must consume exactly through ``0\\r\\n\\r\\n``
        and leave the second request's bytes for the next iteration."""
        s = open_socket('127.0.0.1', h1_app.port, timeout=5)
        try:
            s.sendall(
                b'POST /echo HTTP/1.1\r\n'
                b'Host: localhost\r\n'
                b'Transfer-Encoding: chunked\r\n\r\n'
                b'5\r\nhello\r\n'
                b'6\r\n world\r\n'
                b'0\r\n\r\n'
                b'GET / HTTP/1.1\r\n'
                b'Host: localhost\r\n'
                b'Connection: close\r\n\r\n'
            )
            buf = _read_two_responses(s)
        finally:
            s.close()

        first, second = _split_two_responses(buf)
        r1 = parse_response(first)
        r2 = parse_response(second, closed=True)
        assert r1.status == 200
        assert r1.body == b'hello world', (
            f'chunked echo body mismatch: {r1.body!r}')
        assert r2.status == 200
        assert r2.body.startswith(b'ok'), f'GET after chunked broke: {r2.body!r}'

    def test_post_chunked_with_trailers_then_get(self, h1_app):
        """A chunked body with trailing headers (Trailer: …) must be fully
        consumed including the trailer block before the second request."""
        s = open_socket('127.0.0.1', h1_app.port, timeout=5)
        try:
            s.sendall(
                b'POST /echo HTTP/1.1\r\n'
                b'Host: localhost\r\n'
                b'Transfer-Encoding: chunked\r\n'
                b'Trailer: X-Checksum\r\n\r\n'
                b'5\r\nhello\r\n'
                b'0\r\n'
                b'X-Checksum: abc123\r\n\r\n'
                b'GET / HTTP/1.1\r\n'
                b'Host: localhost\r\n'
                b'Connection: close\r\n\r\n'
            )
            buf = _read_two_responses(s)
        finally:
            s.close()

        first, second = _split_two_responses(buf)
        r1 = parse_response(first)
        r2 = parse_response(second, closed=True)
        assert r1.status == 200
        assert r1.body == b'hello'
        assert r2.status == 200
        assert r2.body.startswith(b'ok')

    def test_three_pipelined_requests(self, h1_app):
        """A burst of three pipelined requests on one socket must produce
        three responses in order.  This stresses the keep-alive loop's
        boundary detection."""
        s = open_socket('127.0.0.1', h1_app.port, timeout=5)
        try:
            s.sendall(
                b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n'
                b'POST /echo HTTP/1.1\r\nHost: localhost\r\n'
                b'Content-Length: 4\r\n\r\nabcd'
                b'GET / HTTP/1.1\r\nHost: localhost\r\n'
                b'Connection: close\r\n\r\n'
            )
            s.settimeout(3.0)
            buf = read_until_eof(s, max_bytes=8192)
        finally:
            s.close()

        markers = [i for i in range(len(buf)) if buf.startswith(b'HTTP/1.1 ', i)]
        assert len(markers) >= 3, (
            f'expected three responses on pipelined socket; saw {len(markers)} '
            f'HTTP/1.1 markers in {buf!r}')

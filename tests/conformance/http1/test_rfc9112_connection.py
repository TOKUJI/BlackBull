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
import base64
import os
import socket

import pytest

from .conftest import open_socket, parse_response, read_until_eof


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
            buf = read_until_eof(s)
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

    def test_asterisk_with_body_drains_before_next_request(self, h1_app):
        """RFC 9110 §9.3.7 permits content on OPTIONS, so the server-wide
        answer must still consume it.

        Undrained, the body bytes become the start of the next request line:
        ``body`` + ``GET / HTTP/1.1`` parses as the method ``bodyGET``, which
        no route allows, and the client's next request is destroyed by the
        framing of the previous one (405 instead of 200).  Behind a
        connection-pooling reverse proxy that desync is the request-smuggling
        shape, so the 200 below is the security-relevant assertion, not just a
        keep-alive nicety.
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
            buf = read_until_eof(s)
        finally:
            s.close()

        second = buf.find(b'HTTP/1.1 ', 8)
        assert second > 0, f'no second response; got {buf!r}'
        first = parse_response(buf[:second])
        rest = parse_response(buf[second:], closed=True)
        assert first.status == 204
        assert rest.status == 200, (
            f'body was not drained — the next request was parsed out of the '
            f'leftover bytes; got {rest.status}')
        assert rest.body.startswith(b'ok')

    def test_asterisk_with_chunked_body_then_pipelined_get(self, h1_app):
        """The drain must follow chunked framing through the terminal chunk.

        A length-oblivious drain would stop early and leave ``0\\r\\n\\r\\n``
        (or the chunk-size lines) in the reader — the same desync as the
        Content-Length case, reached through the other framing.
        """
        s = open_socket('127.0.0.1', h1_app.port, timeout=5)
        try:
            s.sendall(
                b'OPTIONS * HTTP/1.1\r\nHost: localhost\r\n'
                b'Transfer-Encoding: chunked\r\n\r\n'
                b'4\r\nbody\r\n0\r\n\r\n'
                b'GET / HTTP/1.1\r\nHost: localhost\r\n'
                b'Connection: close\r\n\r\n'
            )
            buf = read_until_eof(s)
        finally:
            s.close()

        second = buf.find(b'HTTP/1.1 ', 8)
        assert second > 0, f'no second response; got {buf!r}'
        first = parse_response(buf[:second])
        rest = parse_response(buf[second:], closed=True)
        assert first.status == 204
        assert rest.status == 200, (
            f'chunked body was not drained through the terminal chunk; '
            f'got {rest.status}')
        assert rest.body.startswith(b'ok')

    def test_asterisk_with_large_body_within_bound_drains(self, h1_app):
        """A body at the drain bound is still fully drained.

        Pairs with the over-bound case below to pin the bound from both
        sides: at the bound the connection survives and the pipelined request
        is answered; past it the connection closes.  A body this size also
        exceeds ``StreamReader``'s own buffer limit, so an undrained
        connection dies here for a reason unrelated to the drain — the 200
        is what separates draining from merely surviving.
        """
        body = b'x' * (64 * 1024)          # == _MAX_KEEPALIVE_DRAIN
        s = open_socket('127.0.0.1', h1_app.port, timeout=5)
        try:
            s.sendall(
                b'OPTIONS * HTTP/1.1\r\nHost: localhost\r\n'
                b'Content-Length: ' + str(len(body)).encode() + b'\r\n\r\n'
                + body
                + b'GET / HTTP/1.1\r\nHost: localhost\r\n'
                  b'Connection: close\r\n\r\n'
            )
            buf = read_until_eof(s)
        finally:
            s.close()

        second = buf.find(b'HTTP/1.1 ', 8)
        assert second > 0, f'no second response; got {buf[:200]!r}'
        assert parse_response(buf[:second]).status == 204
        rest = parse_response(buf[second:], closed=True)
        assert rest.status == 200, (
            f'body at the drain bound was not drained; got {rest.status}')
        assert rest.body.startswith(b'ok')

    def test_asterisk_with_oversized_body_closes(self, h1_app):
        """A body past the keep-alive drain bound closes the connection.

        Draining is bounded so an attacker cannot make the server read an
        arbitrary amount of unwanted body to keep one connection alive; past
        the bound the routed path closes, and the server-wide answer must not
        become the cheaper way to buy an unbounded read.  Closing is safe —
        any desync dies with the connection.

        The exchange is staged rather than sent in one write.  Closing with an
        unread body still in the receive buffer makes the kernel send RST,
        which discards data we have not read yet — so the 204 is collected
        *before* the oversized body goes out, and the close is then observed
        on its own.  Sent as one blob, this test would race the reset.
        """
        body = b'x' * (64 * 1024 + 4096)   # over _MAX_KEEPALIVE_DRAIN
        s = open_socket('127.0.0.1', h1_app.port, timeout=5)
        try:
            # 1. Headers only — the server-wide answer needs nothing else.
            s.sendall(
                b'OPTIONS * HTTP/1.1\r\nHost: localhost\r\n'
                b'Content-Length: ' + str(len(body)).encode() + b'\r\n\r\n'
            )
            head = b''
            while b'\r\n\r\n' not in head:
                chunk = s.recv(4096)
                assert chunk, f'connection closed before the answer; got {head!r}'
                head += chunk
            assert parse_response(head).status == 204

            # 2. Now the over-bound body, plus a request that must never run.
            try:
                s.sendall(
                    body
                    + b'GET / HTTP/1.1\r\nHost: localhost\r\n'
                      b'Connection: close\r\n\r\n'
                )
            except OSError:
                pass          # server may already have closed mid-write

            # 3. The connection must end — by EOF or by RST — with the
            #    pipelined GET unanswered.  Read to the end ourselves so a
            #    timeout is distinguishable from a close: a server that simply
            #    ignored the request would also produce no second response.
            tail = b''
            closed = False
            try:
                while len(tail) < 65536:
                    chunk = s.recv(4096)
                    if not chunk:
                        closed = True     # clean FIN
                        break
                    tail += chunk
            except ConnectionResetError:
                closed = True             # RST — a close either way
            except (socket.timeout, TimeoutError):
                pass                      # still open: the failure below
        finally:
            s.close()

        assert closed, 'connection stayed open past the drain bound'
        assert b'HTTP/1.1 ' not in tail, (
            f'pipelined request was answered past the drain bound; got {tail!r}')

    def test_asterisk_then_bare_crlf_closes(self, h1_app):
        # A bare CRLF where the next request-line belongs terminates the
        # connection — one response, then close.
        s = open_socket('127.0.0.1', h1_app.port, timeout=5)
        try:
            s.sendall(
                b'OPTIONS * HTTP/1.1\r\nHost: localhost\r\n\r\n'
                b'\r\n'
            )
            buf = read_until_eof(s)
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
            buf = read_until_eof(s)
        finally:
            s.close()

        assert buf.count(b'HTTP/1.1 ') == 1, (
            f'expected exactly one response; got {buf!r}')
        assert parse_response(buf, closed=True).status == 204


@pytest.mark.integration
class TestUpgradeRequestBody:
    """RFC 9110 §7.8 — content on a request that switches protocols.

    The upgrade path leaves the keep-alive loop before its drain, so the
    body-framing contract that the loop enforces has to be asserted here
    separately.
    """

    def _ws_handshake(self, port, extra_headers=b'', body=b''):
        key = base64.b64encode(os.urandom(16))
        s = open_socket('127.0.0.1', port, timeout=5)
        try:
            s.sendall(
                b'GET /ws HTTP/1.1\r\nHost: localhost\r\n'
                b'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                b'Sec-WebSocket-Key: ' + key + b'\r\n'
                b'Sec-WebSocket-Version: 13\r\n'
                + extra_headers + b'\r\n' + body
            )
            return read_until_eof(s)
        finally:
            s.close()

    @staticmethod
    def _masked_text_frame(payload: bytes) -> bytes:
        mask = b'\xaa\xbb\xcc\xdd'
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return b'\x81' + bytes([0x80 | len(payload)]) + mask + masked

    def test_ws_upgrade_with_body_is_rejected(self, h1_app):
        """A handshake that declares content is refused before the switch.

        Those bytes are HTTP request content per ``Content-Length``, and the
        first WebSocket frames per the 101 — two framings over the same
        octets, which is exactly what intermediaries disagree about.  The
        server refuses rather than picking one: undrained, the body reached
        the application as a WebSocket message (``echo:'SMUGGLED'``), which is
        the smuggling shape with the app as the victim.

        RFC 9110 §9.3.1 gives content on GET no defined semantics and tells
        clients not to send it, so refusing costs nothing real — unlike
        ``OPTIONS``, where §9.3.7 explicitly permits content and the drain is
        therefore the only correct answer.
        """
        payload = b"'SMUGGLED'"
        frame = self._masked_text_frame(payload)
        buf = self._ws_handshake(
            h1_app.port,
            extra_headers=b'Content-Length: ' + str(len(frame)).encode() + b'\r\n',
            body=frame,
        )

        assert parse_response(buf, closed=True).status == 400, (
            f'bodied handshake was not refused; got {buf[:120]!r}')
        assert b'101' not in buf.split(b'\r\n')[0], (
            f'protocol switch happened anyway; got {buf[:120]!r}')
        assert payload not in buf, (
            f'body still reached the application; got {buf!r}')

    def test_ws_upgrade_with_chunked_body_is_rejected(self, h1_app):
        # The other framing reaches the same refusal — a chunked handshake
        # declares content just as much as a Content-Length one does.
        buf = self._ws_handshake(
            h1_app.port,
            extra_headers=b'Transfer-Encoding: chunked\r\n',
            body=b'4\r\nbody\r\n0\r\n\r\n',
        )
        assert parse_response(buf, closed=True).status == 400, (
            f'chunked handshake was not refused; got {buf[:120]!r}')

    def test_ws_upgrade_with_zero_content_length_still_upgrades(self, h1_app):
        """``Content-Length: 0`` declares *no* content, so it must still work.

        Clients and proxies do attach a zero Content-Length to GET requests.
        Zero bytes cannot be framed two ways, so there is nothing to disagree
        about and nothing to refuse — the refusal above is about content, not
        about the header being present.
        """
        buf = self._ws_handshake(
            h1_app.port, extra_headers=b'Content-Length: 0\r\n')
        assert buf.split(b'\r\n')[0] == b'HTTP/1.1 101 Switching Protocols', (
            f'zero-length handshake was refused; got {buf[:120]!r}')

    def test_ws_upgrade_without_body_still_upgrades(self, h1_app):
        # The plain handshake is the control: the check must not cost the
        # ordinary path its upgrade.
        buf = self._ws_handshake(h1_app.port)
        assert buf.split(b'\r\n')[0] == b'HTTP/1.1 101 Switching Protocols', (
            f'plain handshake was refused; got {buf[:120]!r}')

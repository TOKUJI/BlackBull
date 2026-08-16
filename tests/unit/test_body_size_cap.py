"""The total-body ceiling lives on the recipient, not on the caller driving it.

``BB_MAX_BODY_SIZE`` is answered in two places for two reasons: the actor
refuses an over-cap ``Content-Length`` at head time (no octet is read, so the
refusal is free), and the recipient counts the octets themselves.  These are
the recipient's half — the half that still holds when nobody asked the head:
a directly-driven recipient, an external ASGI host, a ``chunked`` body that
declared no length at all, or a peer that under-declared one.

The refusal is deliberately terminal for the connection.  A body we stopped
reading is still arriving, so there is no message boundary left to trust —
``must_close`` says so and ``needs_drain()`` refuses to read the very octets
the cap declined.  The wire-level consequences are in
``tests/conformance/http1/test_rfc9110_body_cap.py``.
"""
from http import HTTPStatus

import pytest

from blackbull.connection import Connection
from blackbull.headers import Headers
from blackbull.router import HTTPException
from blackbull.server.recipient import AbstractReader, HTTP1Recipient

pytestmark = pytest.mark.asyncio


class _BufReader(AbstractReader):
    """Serves a fixed wire buffer; EOF (empty bytes) once drained."""

    def __init__(self, data: bytes = b'') -> None:
        self._buf = bytearray(data)

    async def read(self, n: int) -> bytes:
        if not self._buf:
            return b''
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out


def _conn(headers) -> Connection:
    return Connection(method='POST', path='/p', raw_path=b'/p',
                      headers=Headers(headers), type='http')


def _chunked(payloads: list[bytes]) -> bytes:
    return b''.join(b'%x\r\n%s\r\n' % (len(p), p)
                    for p in payloads) + b'0\r\n\r\n'


def _chunked_recipient(payloads: list[bytes], cap: int) -> HTTP1Recipient:
    return HTTP1Recipient(
        _BufReader(_chunked(payloads)),
        _conn([(b'transfer-encoding', b'chunked')]),
        chunk_size=64 * 1024, max_body=cap)


def _declared_recipient(body: bytes, cap: int, *,
                        declared: int | None = None) -> HTTP1Recipient:
    n = len(body) if declared is None else declared
    return HTTP1Recipient(
        _BufReader(body),
        _conn([(b'content-length', str(n).encode())]),
        chunk_size=64 * 1024, chunk_max=64 * 1024, max_body=cap)


async def _drain(recipient: HTTP1Recipient) -> bytes:
    out = bytearray()
    while (chunk := await recipient.next_chunk()) is not None:
        out += chunk
    return bytes(out)


class TestTheCapCountsOctetsNotDeclarations:
    async def test_chunked_body_over_the_cap_raises_413(self):
        """No chunk is oversized; their sum is.  A per-read bound cannot see
        this, which is why the cap is a running total."""
        r = _chunked_recipient([b'a' * 700, b'b' * 700], cap=1024)
        with pytest.raises(HTTPException) as exc:
            await _drain(r)
        assert exc.value.status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE

    async def test_chunked_body_at_the_cap_is_delivered(self):
        r = _chunked_recipient([b'a' * 512, b'b' * 512], cap=1024)
        assert await _drain(r) == b'a' * 512 + b'b' * 512

    async def test_an_under_declared_content_length_is_still_counted(self):
        """The declaration is the actor's evidence; the octets are ours.

        A head-time check trusts ``Content-Length``.  This recipient is handed
        one that lies — small enough to pass any head check — and the cap still
        holds, because it counts what arrives.
        """
        r = _declared_recipient(b'x' * 4096, cap=1024, declared=4096)
        with pytest.raises(HTTPException) as exc:
            await _drain(r)
        assert exc.value.status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE

    async def test_zero_cap_means_no_cap(self):
        """``0`` is uvicorn's behaviour: the app owns the 413 decision."""
        r = _chunked_recipient([b'a' * 100_000], cap=0)
        assert len(await _drain(r)) == 100_000


class TestARefusedBodyEndsTheConnection:
    async def test_must_close_is_set_by_the_refusal(self):
        r = _chunked_recipient([b'a' * 700, b'b' * 700], cap=1024)
        with pytest.raises(HTTPException):
            await _drain(r)
        assert r.must_close is True

    async def test_needs_drain_refuses_to_read_what_the_cap_declined(self):
        """Draining is how a keep-alive connection finds the next message
        boundary.  There is no next message here: the octets still arriving
        are the ones we refused, and reading them would re-raise the 413 in
        the actor's loop instead of the handler's."""
        r = _chunked_recipient([b'a' * 700, b'b' * 700], cap=1024)
        with pytest.raises(HTTPException):
            await _drain(r)
        assert r.needs_drain() is False

    async def test_a_served_request_does_not_close_the_connection(self):
        r = _chunked_recipient([b'a' * 512], cap=1024)
        await _drain(r)
        assert r.must_close is False

    async def test_the_refusal_does_not_leak_into_the_next_request(self):
        """One recipient per connection, rebound per request — so a verdict
        about request N must not still be true for request N+1.  (It cannot
        happen on the wire, since the refusal closes the connection; the point
        is that the per-request field is reset in ``bind``, where every other
        per-request field lives.)"""
        r = _chunked_recipient([b'a' * 700, b'b' * 700], cap=1024)
        with pytest.raises(HTTPException):
            await _drain(r)
        r.bind(_conn([(b'content-length', b'4')]))
        assert r.must_close is False
        # And the consequence that follows from it: the rebound recipient is
        # willing to read a body again, where the refused one was not.
        assert r.needs_drain() is True


class TestTheCapIsReportedAsACapHit:
    async def test_a_refusal_records_a_cap_hit(self, monkeypatch):
        """Operators find out that a limit fired, and which one, the same way
        every other limit reports it."""
        seen = []
        monkeypatch.setattr('blackbull.server.recipient.log_cap_hit',
                            lambda name, **kw: seen.append((name, kw)))
        r = _chunked_recipient([b'a' * 700, b'b' * 700], cap=1024)
        with pytest.raises(HTTPException):
            await _drain(r)
        assert [name for name, _ in seen] == ['max_body_size']
        assert seen[0][1]['limit'] == 1024
        assert seen[0][1]['requested'] == 1400

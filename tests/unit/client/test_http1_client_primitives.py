"""Sprint 17 Phase 2 — unit tests for ``HTTP1Client``'s low-level primitives.

These methods bypass HTTP1RequestSender's framing safeguards and exist so
differential tests can put deliberately malformed or trickled bytes on the
wire.  Each test exercises one primitive with a fake writer / reader so we
don't need a live BlackBull instance.
"""
from __future__ import annotations

import asyncio
import time
from http import HTTPMethod

import pytest

from blackbull.client.http1 import HTTP1Client
from blackbull.server.recipient import AbstractReader, IncompleteReadError
from blackbull.server.sender import AbstractWriter


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class _CapturingWriter(AbstractWriter):
    """Captures all bytes written, in order, and records call timings."""

    def __init__(self) -> None:
        self.data = bytearray()
        self.writes: list[bytes] = []
        self.call_times: list[float] = []
        self._t0 = time.monotonic()

    async def write(self, data: bytes) -> None:
        self.data.extend(data)
        self.writes.append(bytes(data))
        self.call_times.append(time.monotonic() - self._t0)


class _CannedReader(AbstractReader):
    """Plays back a fixed byte stream for read_response unit tests."""

    def __init__(self, payload: bytes) -> None:
        self._buf = payload
        self._pos = 0

    async def read(self, n: int = -1) -> bytes:
        if n < 0:
            out = self._buf[self._pos:]
            self._pos = len(self._buf)
            return out
        out = self._buf[self._pos:self._pos + n]
        self._pos += len(out)
        return out

    async def readuntil(self, separator: bytes) -> bytes:
        idx = self._buf.find(separator, self._pos)
        if idx == -1:
            partial = self._buf[self._pos:]
            self._pos = len(self._buf)
            raise IncompleteReadError(partial)
        end = idx + len(separator)
        out = self._buf[self._pos:end]
        self._pos = end
        return out

    async def readexactly(self, n: int) -> bytes:
        out = self._buf[self._pos:self._pos + n]
        if len(out) < n:
            self._pos = len(self._buf)
            raise IncompleteReadError(out)
        self._pos += n
        return out


class _NeverReader(AbstractReader):
    """Hangs forever on every read — used to exercise the read-timeout path."""

    async def read(self, n: int = -1) -> bytes:  # pragma: no cover (loops)
        await asyncio.Event().wait()
        return b''

    async def readuntil(self, separator: bytes) -> bytes:  # pragma: no cover
        await asyncio.Event().wait()
        return b''

    async def readexactly(self, n: int) -> bytes:  # pragma: no cover
        await asyncio.Event().wait()
        return b''


def _client_with_fakes(*, record: bool = False,
                      reader: AbstractReader | None = None) -> tuple[HTTP1Client, _CapturingWriter]:
    """Build a client whose I/O is hooked to fake reader / writer instances.

    No real socket; no real connect.  The primitives just need _writer (and
    optionally _reader) on the instance.
    """
    c = HTTP1Client('test-host', 8000, record_wire_bytes=record)
    w = _CapturingWriter()
    c._writer = w                             # type: ignore[assignment]
    if reader is not None:
        c._reader = reader                    # type: ignore[assignment]
    return c, w


# ---------------------------------------------------------------------------
# send_raw
# ---------------------------------------------------------------------------

class TestSendRaw:
    @pytest.mark.asyncio
    async def test_writes_data_unchanged(self):
        c, w = _client_with_fakes()
        await c.send_raw(b'POST / HTTP/1.1\r\n\r\n')
        assert bytes(w.data) == b'POST / HTTP/1.1\r\n\r\n'

    @pytest.mark.asyncio
    async def test_empty_data_is_one_write(self):
        c, w = _client_with_fakes()
        await c.send_raw(b'')
        assert bytes(w.data) == b''
        # Even empty input goes through write() once for symmetry.
        assert len(w.writes) == 1

    @pytest.mark.asyncio
    async def test_byte_interval_splits_into_per_byte_writes(self):
        c, w = _client_with_fakes()
        await c.send_raw(b'hello', byte_interval=0.01)
        assert bytes(w.data) == b'hello'
        # 5 bytes → 5 separate writes
        assert w.writes == [b'h', b'e', b'l', b'l', b'o']

    @pytest.mark.asyncio
    async def test_byte_interval_actually_sleeps(self):
        c, w = _client_with_fakes()
        # 4 bytes with 30 ms apart → expect at least 3 × 30 ms = 90 ms
        await c.send_raw(b'abcd', byte_interval=0.03)
        elapsed = w.call_times[-1] - w.call_times[0]
        assert elapsed >= 0.08, f'expected >=80 ms between first and last write, got {elapsed:.3f}s'

    @pytest.mark.asyncio
    async def test_byte_interval_zero_is_single_write(self):
        c, w = _client_with_fakes()
        await c.send_raw(b'abcde', byte_interval=0.0)
        assert w.writes == [b'abcde']

    @pytest.mark.asyncio
    async def test_no_buffer_capture_by_default(self):
        c, _ = _client_with_fakes()
        await c.send_raw(b'hello')
        assert c.wire_buffer == b''

    @pytest.mark.asyncio
    async def test_buffer_capture_when_recording(self):
        c, _ = _client_with_fakes(record=True)
        await c.send_raw(b'line1\r\n')
        await c.send_raw(b'line2\r\n', byte_interval=0.001)
        assert c.wire_buffer == b'line1\r\nline2\r\n'

    @pytest.mark.asyncio
    async def test_buffer_reset(self):
        c, _ = _client_with_fakes(record=True)
        await c.send_raw(b'before\r\n')
        c.reset_wire_buffer()
        await c.send_raw(b'after\r\n')
        assert c.wire_buffer == b'after\r\n'

    @pytest.mark.asyncio
    async def test_send_raw_without_connect_raises(self):
        c = HTTP1Client('h', 1)
        # No __aenter__ called → _writer is None.
        with pytest.raises(AssertionError):
            await c.send_raw(b'x')


# ---------------------------------------------------------------------------
# send_request_line
# ---------------------------------------------------------------------------

class TestSendRequestLine:
    @pytest.mark.asyncio
    async def test_bytes_method_target_emits_default_version(self):
        c, w = _client_with_fakes()
        await c.send_request_line(b'GET', b'/path')
        assert bytes(w.data) == b'GET /path HTTP/1.1\r\n'

    @pytest.mark.asyncio
    async def test_str_method_str_target_encodes(self):
        c, w = _client_with_fakes()
        await c.send_request_line('POST', '/echo')
        assert bytes(w.data) == b'POST /echo HTTP/1.1\r\n'

    @pytest.mark.asyncio
    async def test_httpmethod_enum_accepted(self):
        c, w = _client_with_fakes()
        await c.send_request_line(HTTPMethod.PUT, '/echo')
        assert bytes(w.data) == b'PUT /echo HTTP/1.1\r\n'

    @pytest.mark.asyncio
    async def test_custom_version_passed_through(self):
        c, w = _client_with_fakes()
        await c.send_request_line(b'GET', b'/', version=b'HTTP/0.9')
        assert bytes(w.data) == b'GET / HTTP/0.9\r\n'

    @pytest.mark.asyncio
    async def test_arbitrary_method_token_accepted_no_validation(self):
        c, w = _client_with_fakes()
        # Garbage method token — the primitive does NOT validate.
        await c.send_request_line(b'BREW', b'/coffee')
        assert bytes(w.data) == b'BREW /coffee HTTP/1.1\r\n'

    @pytest.mark.asyncio
    async def test_method_with_leading_space_emitted_verbatim(self):
        # Sprint 17 deliberately allows malformed request lines — the
        # differential test uses this to probe how nginx and BlackBull
        # diverge on RFC-invalid input.
        c, w = _client_with_fakes()
        await c.send_request_line(b'   GET', b'/')
        assert bytes(w.data) == b'   GET / HTTP/1.1\r\n'


# ---------------------------------------------------------------------------
# send_header_line / end_headers
# ---------------------------------------------------------------------------

class TestHeaderLines:
    @pytest.mark.asyncio
    async def test_send_header_line_format(self):
        c, w = _client_with_fakes()
        await c.send_header_line(b'Host', b'localhost:8000')
        assert bytes(w.data) == b'Host: localhost:8000\r\n'

    @pytest.mark.asyncio
    async def test_send_header_line_no_dedup(self):
        # Caller can deliberately send two Content-Length lines to probe
        # the server's CL.CL smuggling defence (RFC 9112 §6.1).
        c, w = _client_with_fakes()
        await c.send_header_line(b'Content-Length', b'10')
        await c.send_header_line(b'Content-Length', b'20')
        assert bytes(w.data) == b'Content-Length: 10\r\nContent-Length: 20\r\n'

    @pytest.mark.asyncio
    async def test_end_headers_emits_only_crlf(self):
        c, w = _client_with_fakes()
        await c.end_headers()
        assert bytes(w.data) == b'\r\n'


# ---------------------------------------------------------------------------
# send_body_bytes / send_chunk / end_chunked
# ---------------------------------------------------------------------------

class TestBodyPrimitives:
    @pytest.mark.asyncio
    async def test_send_body_bytes_writes_unchanged(self):
        c, w = _client_with_fakes()
        await c.send_body_bytes(b'hello world')
        assert bytes(w.data) == b'hello world'

    @pytest.mark.asyncio
    async def test_send_body_bytes_byte_interval(self):
        c, w = _client_with_fakes()
        await c.send_body_bytes(b'ab', byte_interval=0.01)
        assert w.writes == [b'a', b'b']

    @pytest.mark.asyncio
    async def test_send_chunk_frames_with_hex_size(self):
        c, w = _client_with_fakes()
        await c.send_chunk(b'hello')
        assert bytes(w.data) == b'5\r\nhello\r\n'

    @pytest.mark.asyncio
    async def test_send_chunk_handles_large_size(self):
        c, w = _client_with_fakes()
        big = b'x' * 256
        await c.send_chunk(big)
        # 256 in hex is "100"
        assert bytes(w.data) == b'100\r\n' + big + b'\r\n'

    @pytest.mark.asyncio
    async def test_end_chunked_emits_terminator(self):
        c, w = _client_with_fakes()
        await c.end_chunked()
        assert bytes(w.data) == b'0\r\n\r\n'

    @pytest.mark.asyncio
    async def test_full_chunked_request_round_trip(self):
        # A test using only primitives can express a complete chunked
        # request — no HTTP1RequestSender involvement.
        c, w = _client_with_fakes()
        await c.send_request_line(b'POST', b'/echo')
        await c.send_header_line(b'Host', b'localhost')
        await c.send_header_line(b'Transfer-Encoding', b'chunked')
        await c.end_headers()
        await c.send_chunk(b'aa')
        await c.send_chunk(b'bb')
        await c.end_chunked()
        wire = bytes(w.data)
        assert wire.startswith(b'POST /echo HTTP/1.1\r\n')
        assert b'Transfer-Encoding: chunked\r\n' in wire
        assert b'\r\n\r\n2\r\naa\r\n2\r\nbb\r\n0\r\n\r\n' in wire


# ---------------------------------------------------------------------------
# read_response
# ---------------------------------------------------------------------------

class TestReadResponse:
    @pytest.mark.asyncio
    async def test_parses_simple_response(self):
        reader = _CannedReader(
            b'HTTP/1.1 200 OK\r\n'
            b'content-type: text/plain\r\n'
            b'content-length: 5\r\n'
            b'\r\n'
            b'hello'
        )
        c, _ = _client_with_fakes(reader=reader)
        resp = await c.read_response()
        assert resp.status == 200
        assert resp.body == b'hello'
        assert b'content-type' in [k for k, _ in resp.headers]

    @pytest.mark.asyncio
    async def test_timeout_raises_on_slow_response(self):
        c, _ = _client_with_fakes(reader=_NeverReader())
        with pytest.raises(asyncio.TimeoutError):
            await c.read_response(timeout=0.05)

    @pytest.mark.asyncio
    async def test_no_timeout_by_default(self):
        # Without a timeout, the call returns as soon as the canned reader
        # has produced enough bytes.  Use Content-Length: 0 explicitly —
        # HTTP1ResponseRecipient currently mis-handles absent
        # Content-Length (Headers.get returns b'', the `cl_raw is not
        # None` check then fires int(b'') → ValueError).  That pre-Sprint-17
        # bug is logged for a separate fix; it's not in Phase 2 scope.
        reader = _CannedReader(
            b'HTTP/1.1 204 No Content\r\n'
            b'content-length: 0\r\n'
            b'\r\n'
        )
        c, _ = _client_with_fakes(reader=reader)
        resp = await c.read_response()
        assert resp.status == 204
        assert resp.body == b''

    @pytest.mark.asyncio
    async def test_read_response_without_connect_raises(self):
        c = HTTP1Client('h', 1)
        with pytest.raises(AssertionError):
            await c.read_response()

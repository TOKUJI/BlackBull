"""End-to-end gRPC tests via BlackBull's own ``HTTP2Client`` over h2c.

These tests complement the in-process ASGI harness tests by exercising
the **full HTTP/2 stack** — real TCP socket, connection preface, HPACK,
flow control, frame multiplexing — with real gRPC Length-Prefixed-Message
payloads.

Unlike ``httpx``-based tests (which crash on ``http.response.trailers``),
``HTTP2Client`` natively handles HTTP/2 HEADERS frames with END_STREAM,
making it possible to verify:

* Success responses: HEADERS → DATA → TRAILERS (HEADERS+END_STREAM)
* Trailers-only error responses: HEADERS+END_STREAM (no DATA)
* gRPC status/message in trailing headers
* Concurrent gRPC calls over a single H2 connection
* Large payload round-trips

Architecture::

    pytest test
      └─ asyncio.create_task(ASGIServer.run())  ← ephemeral port
      └─ HTTP2Client('127.0.0.1', port)        ← h2c (plain TCP, no TLS)
           └─ c.request('POST', '/Svc/Method',
                        headers=[('content-type', 'application/grpc')],
                        body=encode_message(payload))
"""
from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from blackbull import BlackBull
from blackbull.grpc import (
    GrpcServiceRegistry, GrpcStatus, GrpcError,
    encode_message, decode_messages, GrpcDecodeError,
)
from blackbull.client.http2 import HTTP2Client
from blackbull.server.server import ASGIServer


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SERVER_STARTUP_WAIT_SECONDS = 0.15
_EPHEMERAL_PORT = 0


# ---------------------------------------------------------------------------
# Fixture: gRPC app served via ASGIServer on ephemeral port
# ---------------------------------------------------------------------------

def _make_grpc_app() -> BlackBull:
    """Build a BlackBull app with gRPC support for end-to-end testing.

    Registered methods:

    * ``/echo.Echo/Echo`` — reverses the request bytes
    * ``/echo.Echo/Identity`` — returns request verbatim
    * ``/echo.Echo/Empty`` — returns empty bytes
    * ``/math.Arith/Add`` — unpacks two little-endian int32, returns their sum
    * ``/err.Err/Explode`` — raises ``RuntimeError`` (→ INTERNAL)
    * ``/err.Err/GrpcErr`` — raises ``GrpcError(NOT_FOUND, ...)``
    * ``/err.Err/Abort`` — calls ``context.abort(PERMISSION_DENIED, ...)``
    * ``/meta.Meta/EchoHeader`` — echoes ``x-echo`` header value
    * ``/slow.Slow/Delay`` — sleeps 50 ms then returns
    """
    import struct

    app = BlackBull()
    reg = GrpcServiceRegistry()

    @reg.method('/echo.Echo/Echo')
    async def echo(request, context):
        return request[::-1]

    @reg.method('/echo.Echo/Identity')
    async def identity(request, context):
        return request

    @reg.method('/echo.Echo/Empty')
    async def empty_handler(request, context):
        return b''

    @reg.method('/math.Arith/Add')
    async def add(request, context):
        a, b = struct.unpack('<ii', request)
        return struct.pack('<i', a + b)

    @reg.method('/err.Err/Explode')
    async def explode(request, context):
        raise RuntimeError('kaboom')

    @reg.method('/err.Err/GrpcErr')
    async def grpc_err(request, context):
        raise GrpcError(GrpcStatus.NOT_FOUND, 'item not found')

    @reg.method('/err.Err/Abort')
    async def abort_handler(request, context):
        context.abort(GrpcStatus.PERMISSION_DENIED, 'access denied')
        return b''  # unreachable

    @reg.method('/meta.Meta/EchoHeader')
    async def echo_header(request, context):
        val = context.metadata(b'x-echo', b'no-header')
        context.set_trailing_metadata([(b'x-response', val)])
        return val

    @reg.method('/slow.Slow/Delay')
    async def delay(request, context):
        await asyncio.sleep(0.05)
        return request

    app.enable_grpc(reg)
    return app


@pytest_asyncio.fixture
async def grpc_h2_port():
    """Start a gRPC-enabled BlackBull app on an ephemeral port via h2c.

    Yields the port number.  The server is torn down after the test.
    """
    app = _make_grpc_app()
    server = ASGIServer(app)
    server.open_socket(port=_EPHEMERAL_PORT)
    port = server.port

    task = asyncio.create_task(server.run())
    await asyncio.sleep(_SERVER_STARTUP_WAIT_SECONDS)

    try:
        yield port
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _grpc_request_body(payload: bytes, *, compressed: bool = False) -> bytes:
    """Wrap *payload* as a gRPC Length-Prefixed-Message."""
    return encode_message(payload, compressed=compressed)


def _parse_grpc_response_body(body: bytes) -> list[tuple[bool, bytes]]:
    """Decode LPM-framed gRPC response body."""
    return decode_messages(body)


# ---------------------------------------------------------------------------
# §1 — Basic unary gRPC calls (success path: HEADERS → DATA → TRAILERS)
# ---------------------------------------------------------------------------

class TestGrpcH2BasicUnary:
    """Happy-path unary gRPC calls over a real HTTP/2 connection."""

    @pytest.mark.asyncio
    async def test_echo_reverses_payload(self, grpc_h2_port):
        async with HTTP2Client('127.0.0.1', grpc_h2_port) as c:
            res = await c.request(
                'POST', '/echo.Echo/Echo',
                headers=[('content-type', 'application/grpc')],
                body=_grpc_request_body(b'hello'))

        assert res.status == 200
        assert _parse_grpc_response_body(res.body) == [(False, b'olleh')]
        assert res.headers.get(b'grpc-status') == b'0'
        assert res.headers.get(b'content-type') == b'application/grpc'

    @pytest.mark.asyncio
    async def test_identity_round_trips_binary(self, grpc_h2_port):
        """All byte values 0x00–0xFF must round-trip unchanged."""
        payload = bytes(range(256))
        async with HTTP2Client('127.0.0.1', grpc_h2_port) as c:
            res = await c.request(
                'POST', '/echo.Echo/Identity',
                headers=[('content-type', 'application/grpc')],
                body=_grpc_request_body(payload))

        assert res.status == 200
        assert _parse_grpc_response_body(res.body) == [(False, payload)]
        assert res.headers.get(b'grpc-status') == b'0'

    @pytest.mark.asyncio
    async def test_empty_payload_round_trips(self, grpc_h2_port):
        """Empty request → empty response (zero-length LPM message)."""
        async with HTTP2Client('127.0.0.1', grpc_h2_port) as c:
            res = await c.request(
                'POST', '/echo.Echo/Empty',
                headers=[('content-type', 'application/grpc')],
                body=_grpc_request_body(b''))

        assert res.status == 200
        assert _parse_grpc_response_body(res.body) == [(False, b'')]
        assert res.headers.get(b'grpc-status') == b'0'

    @pytest.mark.asyncio
    async def test_empty_request_body_is_rejected(self, grpc_h2_port):
        """Sending zero bytes (no LPM frame at all) must be rejected."""
        async with HTTP2Client('127.0.0.1', grpc_h2_port) as c:
            res = await c.request(
                'POST', '/echo.Echo/Identity',
                headers=[('content-type', 'application/grpc')],
                body=b'')

        # Zero messages → error; should be INTERNAL or UNIMPLEMENTED
        assert res.status == 200
        assert res.headers.get(b'grpc-status') in (b'12', b'13')  # UNIMPLEMENTED or INTERNAL

    @pytest.mark.asyncio
    async def test_structured_payload_add(self, grpc_h2_port):
        """Two int32 values packed as little-endian, sum returned."""
        import struct
        payload = struct.pack('<ii', 123, 456)
        async with HTTP2Client('127.0.0.1', grpc_h2_port) as c:
            res = await c.request(
                'POST', '/math.Arith/Add',
                headers=[('content-type', 'application/grpc')],
                body=_grpc_request_body(payload))

        assert res.status == 200
        result = _parse_grpc_response_body(res.body)
        assert len(result) == 1
        assert struct.unpack('<i', result[0][1])[0] == 579


# ---------------------------------------------------------------------------
# §2 — Error paths
# ---------------------------------------------------------------------------

class TestGrpcH2Errors:
    """gRPC error responses over real HTTP/2."""

    @pytest.mark.asyncio
    async def test_unimplemented_method(self, grpc_h2_port):
        """Unregistered method → UNIMPLEMENTED (12) in trailers-only."""
        async with HTTP2Client('127.0.0.1', grpc_h2_port) as c:
            res = await c.request(
                'POST', '/No.Such/Method',
                headers=[('content-type', 'application/grpc')],
                body=_grpc_request_body(b''))

        assert res.status == 200
        assert res.body == b''  # trailers-only: no DATA frame
        assert res.headers.get(b'grpc-status') == str(int(GrpcStatus.UNIMPLEMENTED)).encode()

    @pytest.mark.asyncio
    async def test_handler_grpc_error(self, grpc_h2_port):
        """``GrpcError`` → correct grpc-status in trailers-only."""
        async with HTTP2Client('127.0.0.1', grpc_h2_port) as c:
            res = await c.request(
                'POST', '/err.Err/GrpcErr',
                headers=[('content-type', 'application/grpc')],
                body=_grpc_request_body(b'x'))

        assert res.status == 200
        assert res.body == b''
        assert res.headers.get(b'grpc-status') == str(int(GrpcStatus.NOT_FOUND)).encode()
        assert b'item not found' in res.headers.get(b'grpc-message', b'')

    @pytest.mark.asyncio
    async def test_handler_exception_becomes_internal(self, grpc_h2_port):
        """Unhandled exception → INTERNAL (13) in trailers-only."""
        async with HTTP2Client('127.0.0.1', grpc_h2_port) as c:
            res = await c.request(
                'POST', '/err.Err/Explode',
                headers=[('content-type', 'application/grpc')],
                body=_grpc_request_body(b''))

        assert res.status == 200
        assert res.body == b''
        assert res.headers.get(b'grpc-status') == str(int(GrpcStatus.INTERNAL)).encode()

    @pytest.mark.asyncio
    async def test_context_abort(self, grpc_h2_port):
        """``context.abort()`` → PERMISSION_DENIED in trailers-only."""
        async with HTTP2Client('127.0.0.1', grpc_h2_port) as c:
            res = await c.request(
                'POST', '/err.Err/Abort',
                headers=[('content-type', 'application/grpc')],
                body=_grpc_request_body(b''))

        assert res.status == 200
        assert res.body == b''
        assert res.headers.get(b'grpc-status') == \
            str(int(GrpcStatus.PERMISSION_DENIED)).encode()

    @pytest.mark.asyncio
    async def test_compressed_message_rejected(self, grpc_h2_port):
        """Compressed flag set → UNIMPLEMENTED (compression not supported)."""
        async with HTTP2Client('127.0.0.1', grpc_h2_port) as c:
            res = await c.request(
                'POST', '/echo.Echo/Identity',
                headers=[('content-type', 'application/grpc')],
                body=_grpc_request_body(b'data', compressed=True))

        assert res.status == 200
        assert res.body == b''
        assert res.headers.get(b'grpc-status') == \
            str(int(GrpcStatus.UNIMPLEMENTED)).encode()

    @pytest.mark.asyncio
    async def test_truncated_lpm_body_returns_internal(self, grpc_h2_port):
        """A truncated LPM frame → INTERNAL."""
        async with HTTP2Client('127.0.0.1', grpc_h2_port) as c:
            res = await c.request(
                'POST', '/echo.Echo/Identity',
                headers=[('content-type', 'application/grpc')],
                body=b'\x00\x00\x00')  # truncated prefix

        assert res.status == 200
        assert res.headers.get(b'grpc-status') == str(int(GrpcStatus.INTERNAL)).encode()


# ---------------------------------------------------------------------------
# §3 — Metadata (request headers → response trailers)
# ---------------------------------------------------------------------------

class TestGrpcH2Metadata:
    """Request metadata and trailing metadata over real HTTP/2."""

    @pytest.mark.asyncio
    async def test_request_header_echoed_in_response_body(self, grpc_h2_port):
        """Handler reads ``x-echo`` header and returns it as response body."""
        async with HTTP2Client('127.0.0.1', grpc_h2_port) as c:
            res = await c.request(
                'POST', '/meta.Meta/EchoHeader',
                headers=[('content-type', 'application/grpc'),
                         ('x-echo', 'my-custom-value')],
                body=_grpc_request_body(b''))

        assert res.status == 200
        assert _parse_grpc_response_body(res.body) == [(False, b'my-custom-value')]
        assert res.headers.get(b'grpc-status') == b'0'

    @pytest.mark.asyncio
    async def test_trailing_metadata_in_response(self, grpc_h2_port):
        """Handler sets ``x-response`` trailing metadata — must appear in
        response headers (merged by HTTP2Client).

        Uses ASCII-safe header values to avoid HPACK str↔bytes corruption
        of binary data in the HTTP2Client (binary metadata should use the
        ``-bin`` suffix per gRPC spec)."""
        async with HTTP2Client('127.0.0.1', grpc_h2_port) as c:
            res = await c.request(
                'POST', '/meta.Meta/EchoHeader',
                headers=[('content-type', 'application/grpc'),
                         ('x-echo', 'custom-value')],
                body=_grpc_request_body(b''))

        assert res.status == 200
        # Trailing metadata x-response should be in the merged headers
        assert res.headers.get(b'x-response') == b'custom-value'

    @pytest.mark.asyncio
    async def test_missing_header_default_value(self, grpc_h2_port):
        """When ``x-echo`` header is absent, handler returns default."""
        async with HTTP2Client('127.0.0.1', grpc_h2_port) as c:
            res = await c.request(
                'POST', '/meta.Meta/EchoHeader',
                headers=[('content-type', 'application/grpc')],
                body=_grpc_request_body(b''))

        assert res.status == 200
        assert _parse_grpc_response_body(res.body) == [(False, b'no-header')]


# ---------------------------------------------------------------------------
# §4 — Large payloads
# ---------------------------------------------------------------------------

class TestGrpcH2LargePayloads:
    """Large gRPC messages over real HTTP/2.

    A 16 KiB payload + 5-byte LPM prefix exceeds ``SETTINGS_MAX_FRAME_SIZE``
    (16384), so the request body must span multiple DATA frames.
    ``HTTP2Client.request()`` now sends the body through the sender's
    flow-controlled ``_write_data`` path, which fragments it correctly."""

    @pytest.mark.timeout(15)
    @pytest.mark.asyncio
    async def test_16kb_payload_round_trips(self, grpc_h2_port):
        """16 KiB payload (> H2 max frame size) round-trips via Identity.

        Exercises the client's DATA-frame fragmentation: 16 KiB + the 5-byte
        LPM prefix exceeds SETTINGS_MAX_FRAME_SIZE (16384), so the request
        body must span multiple DATA frames.  The response (16389 bytes) fits
        within the 65535-byte initial flow-control window, so no mid-stream
        WINDOW_UPDATE handshake is required.
        """
        payload = bytes((i % 256) for i in range(16 * 1024))
        async with HTTP2Client('127.0.0.1', grpc_h2_port) as c:
            res = await c.request(
                'POST', '/echo.Echo/Identity',
                headers=[('content-type', 'application/grpc')],
                body=_grpc_request_body(payload))

        assert res.status == 200
        assert _parse_grpc_response_body(res.body) == [(False, payload)]
        assert res.headers.get(b'grpc-status') == b'0'


# ---------------------------------------------------------------------------
# §5 — Concurrent gRPC calls (H2 multiplexing)
# ---------------------------------------------------------------------------

class TestGrpcH2Concurrent:
    """Multiple concurrent gRPC calls over a single HTTP/2 connection.
    This exercises stream multiplexing — a core HTTP/2 feature that
    the in-process ASGI harness cannot verify."""

    @pytest.mark.asyncio
    async def test_two_concurrent_unary_calls(self, grpc_h2_port):
        """Two calls multiplexed over one H2 connection must not interfere."""
        async with HTTP2Client('127.0.0.1', grpc_h2_port) as c:
            r1, r2 = await asyncio.gather(
                c.request('POST', '/echo.Echo/Echo',
                          headers=[('content-type', 'application/grpc')],
                          body=_grpc_request_body(b'first')),
                c.request('POST', '/echo.Echo/Echo',
                          headers=[('content-type', 'application/grpc')],
                          body=_grpc_request_body(b'second')),
            )

        assert r1.status == 200
        assert r2.status == 200
        assert _parse_grpc_response_body(r1.body) == [(False, b'tsrif')]
        assert _parse_grpc_response_body(r2.body) == [(False, b'dnoces')]

    @pytest.mark.asyncio
    async def test_five_concurrent_calls(self, grpc_h2_port):
        """Five concurrent calls with unique payloads must all return correctly."""
        async with HTTP2Client('127.0.0.1', grpc_h2_port) as c:
            tasks = []
            for i in range(5):
                payload = f'msg{i}'.encode()
                tasks.append(
                    c.request('POST', '/echo.Echo/Identity',
                              headers=[('content-type', 'application/grpc')],
                              body=_grpc_request_body(payload))
                )
            results = await asyncio.gather(*tasks)

        for i, res in enumerate(results):
            assert res.status == 200
            expected = f'msg{i}'.encode()
            assert _parse_grpc_response_body(res.body) == [(False, expected)]

    @pytest.mark.asyncio
    async def test_concurrent_calls_to_different_methods(self, grpc_h2_port):
        """Concurrent calls to Echo and Add — different handlers, same connection."""
        import struct
        async with HTTP2Client('127.0.0.1', grpc_h2_port) as c:
            r1, r2 = await asyncio.gather(
                c.request('POST', '/echo.Echo/Echo',
                          headers=[('content-type', 'application/grpc')],
                          body=_grpc_request_body(b'hello')),
                c.request('POST', '/math.Arith/Add',
                          headers=[('content-type', 'application/grpc')],
                          body=_grpc_request_body(struct.pack('<ii', 10, 20))),
            )

        assert r1.status == 200
        assert r2.status == 200
        assert _parse_grpc_response_body(r1.body) == [(False, b'olleh')]
        assert struct.unpack('<i', _parse_grpc_response_body(r2.body)[0][1])[0] == 30

    @pytest.mark.asyncio
    async def test_concurrent_calls_with_errors(self, grpc_h2_port):
        """Mix of success and error calls — errors must not poison other streams."""
        async with HTTP2Client('127.0.0.1', grpc_h2_port) as c:
            r_ok1, r_err, r_ok2 = await asyncio.gather(
                c.request('POST', '/echo.Echo/Identity',
                          headers=[('content-type', 'application/grpc')],
                          body=_grpc_request_body(b'ok1')),
                c.request('POST', '/err.Err/GrpcErr',
                          headers=[('content-type', 'application/grpc')],
                          body=_grpc_request_body(b'')),
                c.request('POST', '/echo.Echo/Identity',
                          headers=[('content-type', 'application/grpc')],
                          body=_grpc_request_body(b'ok2')),
            )

        # Success calls must still succeed
        assert r_ok1.status == 200
        assert r_ok2.status == 200
        assert _parse_grpc_response_body(r_ok1.body) == [(False, b'ok1')]
        assert _parse_grpc_response_body(r_ok2.body) == [(False, b'ok2')]

        # Error call must report correctly
        assert r_err.status == 200
        assert r_err.headers.get(b'grpc-status') == \
            str(int(GrpcStatus.NOT_FOUND)).encode()


# ---------------------------------------------------------------------------
# §6 — Slow handlers
# ---------------------------------------------------------------------------

class TestGrpcH2SlowHandler:
    """Handlers with artificial delays exercise H2 stream isolation."""

    @pytest.mark.asyncio
    async def test_slow_handler_completes(self, grpc_h2_port):
        """A handler that sleeps 50 ms must still complete correctly."""
        async with HTTP2Client('127.0.0.1', grpc_h2_port) as c:
            res = await c.request(
                'POST', '/slow.Slow/Delay',
                headers=[('content-type', 'application/grpc')],
                body=_grpc_request_body(b'patience'))

        assert res.status == 200
        assert _parse_grpc_response_body(res.body) == [(False, b'patience')]
        assert res.headers.get(b'grpc-status') == b'0'


# ---------------------------------------------------------------------------
# §7 — Response shape verification
# ---------------------------------------------------------------------------

class TestGrpcH2ResponseShape:
    """Verify the correct H2 frame sequence for gRPC responses."""

    @pytest.mark.asyncio
    async def test_success_response_has_content_type(self, grpc_h2_port):
        """Success response must carry ``Content-Type: application/grpc``."""
        async with HTTP2Client('127.0.0.1', grpc_h2_port) as c:
            res = await c.request(
                'POST', '/echo.Echo/Echo',
                headers=[('content-type', 'application/grpc')],
                body=_grpc_request_body(b'test'))

        assert res.headers.get(b'content-type') == b'application/grpc'

    @pytest.mark.asyncio
    async def test_success_response_has_grpc_status_zero(self, grpc_h2_port):
        """Success response trailers must include ``grpc-status: 0``."""
        async with HTTP2Client('127.0.0.1', grpc_h2_port) as c:
            res = await c.request(
                'POST', '/echo.Echo/Echo',
                headers=[('content-type', 'application/grpc')],
                body=_grpc_request_body(b'test'))

        assert res.headers.get(b'grpc-status') == b'0'

    @pytest.mark.asyncio
    async def test_error_response_has_empty_body(self, grpc_h2_port):
        """Trailers-only error response must have zero-length body (no DATA frame)."""
        async with HTTP2Client('127.0.0.1', grpc_h2_port) as c:
            res = await c.request(
                'POST', '/No.Such/Method',
                headers=[('content-type', 'application/grpc')],
                body=_grpc_request_body(b''))

        assert res.body == b''

    @pytest.mark.asyncio
    async def test_success_response_has_non_empty_body(self, grpc_h2_port):
        """Success response must contain exactly one LPM-framed response message."""
        async with HTTP2Client('127.0.0.1', grpc_h2_port) as c:
            res = await c.request(
                'POST', '/echo.Echo/Echo',
                headers=[('content-type', 'application/grpc')],
                body=_grpc_request_body(b'data'))

        messages = _parse_grpc_response_body(res.body)
        assert len(messages) == 1, (
            f'unary response must contain exactly 1 message, got {len(messages)}')
        assert messages[0] == (False, b'atad')

    @pytest.mark.asyncio
    async def test_grpc_status_is_present_in_all_responses(self, grpc_h2_port):
        """Every gRPC response (success or error) must include ``grpc-status``
        in either initial headers (trailers-only) or trailing headers."""
        # Test both paths
        async with HTTP2Client('127.0.0.1', grpc_h2_port) as c:
            # Success
            r1 = await c.request(
                'POST', '/echo.Echo/Echo',
                headers=[('content-type', 'application/grpc')],
                body=_grpc_request_body(b'x'))
            assert b'grpc-status' in dict(r1.headers), 'success: grpc-status missing'

            # Unimplemented
            r2 = await c.request(
                'POST', '/No.Such/Method',
                headers=[('content-type', 'application/grpc')],
                body=_grpc_request_body(b''))
            assert b'grpc-status' in dict(r2.headers), 'unimplemented: grpc-status missing'

            # GrpcError
            r3 = await c.request(
                'POST', '/err.Err/GrpcErr',
                headers=[('content-type', 'application/grpc')],
                body=_grpc_request_body(b''))
            assert b'grpc-status' in dict(r3.headers), 'grpc-error: grpc-status missing'

            # Exception → INTERNAL
            r4 = await c.request(
                'POST', '/err.Err/Explode',
                headers=[('content-type', 'application/grpc')],
                body=_grpc_request_body(b''))
            assert b'grpc-status' in dict(r4.headers), 'exception: grpc-status missing'


# ---------------------------------------------------------------------------
# §8 — HTTP/2 connection reuse (multiple requests on same connection)
# ---------------------------------------------------------------------------

class TestGrpcH2ConnectionReuse:
    """Multiple sequential gRPC calls over the same H2 connection."""

    @pytest.mark.asyncio
    async def test_sequential_calls_reuse_connection(self, grpc_h2_port):
        """Ten sequential gRPC calls on the same connection must all succeed."""
        async with HTTP2Client('127.0.0.1', grpc_h2_port) as c:
            for i in range(10):
                payload = f'call{i}'.encode()
                res = await c.request(
                    'POST', '/echo.Echo/Identity',
                    headers=[('content-type', 'application/grpc')],
                    body=_grpc_request_body(payload))

                assert res.status == 200, f'call {i} failed with status {res.status}'
                assert _parse_grpc_response_body(res.body) == [(False, payload)], (
                    f'call {i} body mismatch')
                assert res.headers.get(b'grpc-status') == b'0', (
                    f'call {i} grpc-status missing')

    @pytest.mark.asyncio
    async def test_interleaved_success_and_error(self, grpc_h2_port):
        """Alternating between a success call and an error call on the same connection."""
        async with HTTP2Client('127.0.0.1', grpc_h2_port) as c:
            # Call 1: success
            r = await c.request(
                'POST', '/echo.Echo/Echo',
                headers=[('content-type', 'application/grpc')],
                body=_grpc_request_body(b'abc'))
            assert _parse_grpc_response_body(r.body) == [(False, b'cba')]

            # Call 2: unimplemented
            r = await c.request(
                'POST', '/No.Such/Method',
                headers=[('content-type', 'application/grpc')],
                body=_grpc_request_body(b''))
            assert r.headers.get(b'grpc-status') == str(int(GrpcStatus.UNIMPLEMENTED)).encode()

            # Call 3: success again
            r = await c.request(
                'POST', '/echo.Echo/Identity',
                headers=[('content-type', 'application/grpc')],
                body=_grpc_request_body(b'xyz'))
            assert _parse_grpc_response_body(r.body) == [(False, b'xyz')]

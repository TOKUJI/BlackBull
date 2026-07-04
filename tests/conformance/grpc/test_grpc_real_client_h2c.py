"""Interop conformance: a **real third-party gRPC client** (grpcio) driving
BlackBull's unary gRPC over a real h2c socket (Sprint 58).

Every other gRPC test in the suite drives the server with BlackBull's own
``HTTP2Client`` or the in-process ``serve_grpc`` harness — both of which are
lenient about the exact HEADERS/DATA/TRAILERS framing.  This module is the only
one that puts an *external, spec-strict* client (``grpcio``) on the wire, so it
is what catches interop bugs BlackBull's own client tolerates.

It caught the Trailers-Only framing bug (Sprint 58): the error path emitted
``grpc-status`` in a non-terminal HEADERS frame followed by an empty
END_STREAM DATA frame, which grpcio decoded as UNKNOWN /
"Stream removed (Data frame with END_STREAM flag received)".  The fix routes
errors through Response-Headers + trailers so the status rides a *trailing*
HEADERS frame.

``grpcio`` is not a BlackBull dependency (the framework's gRPC layer is
pure-Python — handlers exchange raw message bytes), so the whole module is
skipped when it is not importable.  The dedicated ``grpc-interop`` CI job
installs ``grpcio`` so this actually runs on every push/PR.

The grpcio client is blocking and manages its own threads, so client calls run
via ``asyncio.to_thread`` while the BlackBull server runs in the test's event
loop.  Generic ``(bytes) -> bytes`` (de)serializers are used so no ``.proto``
compilation is needed — the raw message bytes are the body.
"""
from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

grpc = pytest.importorskip(
    'grpc', reason='grpcio (real gRPC client) not installed; '
                   'install the grpc-interop extra to run this suite')

from blackbull import BlackBull
from blackbull.grpc import GrpcServiceRegistry, GrpcStatus, GrpcError
from blackbull.server.server import ASGIServer


_SERVER_STARTUP_WAIT_SECONDS = 0.15
_EPHEMERAL_PORT = 0
_CALL_TIMEOUT = 5.0


def _make_grpc_app() -> BlackBull:
    """A gRPC app exercising the success path and each error class."""
    app = BlackBull()
    reg = GrpcServiceRegistry()

    @reg.method('/echo.Echo/Echo')
    async def echo(request, context):
        return request[::-1]

    @reg.method('/echo.Echo/Big')
    async def big(request, context):
        # Response > the 65535-byte initial window: exercises flow control
        # under a real client on the success path.
        return b'Z' * 100_000

    @reg.method('/echo.Echo/Stream')
    async def stream(request, context):
        # Server-streaming: request bytes = ascii count; yield that many.
        n = int(request.decode() or '0')
        for i in range(n):
            yield f'msg{i}'.encode()

    @reg.method('/echo.Echo/StreamBoom')
    async def stream_boom(request, context):
        # Emits one message then fails — the client must see the message then
        # the error status in trailers.
        yield b'first'
        raise GrpcError(GrpcStatus.PERMISSION_DENIED, 'stop')

    @reg.method('/echo.Echo/Collect')
    async def collect(request_iter, context):
        # Client-streaming: concatenate every request message into one reply.
        return b','.join([m async for m in request_iter])

    @reg.method('/echo.Echo/CountBytes')
    async def count_bytes(request_iter, context):
        # Client-streaming over a large request stream: sum message lengths so
        # the request side crosses the 65535-byte initial window under a real
        # client (request-direction flow control).
        total = 0
        async for m in request_iter:
            total += len(m)
        return str(total).encode()

    @reg.method('/echo.Echo/Chat')
    async def chat(request_iter, context):
        # Bidirectional: echo each request message back as its own response.
        # Same-size echo means a large request stream also produces a large
        # response stream — concurrent bidirectional flow control on one stream.
        async for m in request_iter:
            yield m

    @reg.method('/echo.Echo/Leading')
    async def leading(request, context):
        # Flush leading metadata before the reply so a real client reads it
        # from the initial HEADERS (call.initial_metadata()).
        await context.send_initial_metadata([(b'x-leading', b'hi')])
        return b'body'

    @reg.method('/echo.Echo/Peer')
    async def peer(request, context):
        # Echo the peer the server derived from the real socket back to the
        # client (should be ipv4:127.0.0.1:<port>).
        return context.peer().encode()

    @reg.method('/err.Err/Explode')
    async def explode(request, context):
        raise RuntimeError('kaboom')

    @reg.method('/err.Err/Denied')
    async def denied(request, context):
        raise GrpcError(GrpcStatus.PERMISSION_DENIED, 'access denied')

    @reg.method('/err.Err/Abort')
    async def abort_handler(request, context):
        context.abort(GrpcStatus.NOT_FOUND, 'item not found')
        return b''  # unreachable

    app.enable_grpc(reg)
    return app


@pytest_asyncio.fixture
async def grpc_server_port():
    """Start a gRPC-enabled BlackBull app on an ephemeral h2c port."""
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


def _blocking_unary(port: int, method: str, payload: bytes):
    """Issue one blocking unary call with a real grpcio channel.

    Returns ``(ok, value_or_error)`` — ``(True, response_bytes)`` on success,
    ``(False, grpc.RpcError)`` on a non-OK status.  Runs entirely inside the
    worker thread ``asyncio.to_thread`` hands it, so grpcio's own IO threads
    never touch the server's event loop.
    """
    with grpc.insecure_channel(f'127.0.0.1:{port}') as channel:
        grpc.channel_ready_future(channel).result(timeout=_CALL_TIMEOUT)
        call = channel.unary_unary(
            method,
            request_serializer=lambda b: b,
            response_deserializer=lambda b: b,
        )
        try:
            return (True, call(payload, timeout=_CALL_TIMEOUT))
        except grpc.RpcError as exc:  # noqa: BLE001 — surface the status to the test
            return (False, exc)


async def _unary(port: int, method: str, payload: bytes = b''):
    return await asyncio.to_thread(_blocking_unary, port, method, payload)


def _blocking_unary_gzip(port: int, method: str, payload: bytes):
    """Like :func:`_blocking_unary` but asks grpcio to gzip the request.

    ``compression=grpc.Compression.Gzip`` makes the client set the
    Compressed-Flag + ``grpc-encoding: gzip`` on the request (server must
    decompress), and it advertises ``grpc-accept-encoding: gzip`` so the server
    may gzip the response back (client must decompress) — a full both-directions
    wire-compat check against real grpcio."""
    with grpc.insecure_channel(f'127.0.0.1:{port}') as channel:
        grpc.channel_ready_future(channel).result(timeout=_CALL_TIMEOUT)
        call = channel.unary_unary(
            method,
            request_serializer=lambda b: b,
            response_deserializer=lambda b: b,
        )
        try:
            return (True, call(payload, timeout=_CALL_TIMEOUT,
                               compression=grpc.Compression.Gzip))
        except grpc.RpcError as exc:  # noqa: BLE001 — surface the status
            return (False, exc)


async def _unary_gzip(port: int, method: str, payload: bytes = b''):
    return await asyncio.to_thread(_blocking_unary_gzip, port, method, payload)


def _blocking_unary_with_initial_md(port: int, method: str, payload: bytes):
    """Issue a unary call and return ``(response, initial_metadata)`` — the
    leading metadata grpcio read from the initial HEADERS."""
    with grpc.insecure_channel(f'127.0.0.1:{port}') as channel:
        grpc.channel_ready_future(channel).result(timeout=_CALL_TIMEOUT)
        call = channel.unary_unary(
            method,
            request_serializer=lambda b: b,
            response_deserializer=lambda b: b,
        )
        future = call.future(payload, timeout=_CALL_TIMEOUT)
        return (future.result(), tuple(future.initial_metadata()))


async def _unary_with_initial_md(port: int, method: str, payload: bytes = b''):
    return await asyncio.to_thread(_blocking_unary_with_initial_md, port, method, payload)


def _blocking_server_stream(port: int, method: str, payload: bytes):
    """Issue one blocking server-streaming call with a real grpcio channel.

    Returns ``(messages, error)`` — ``error`` is ``None`` on a clean OK stream
    or the ``grpc.RpcError`` if the stream ended non-OK (``messages`` still
    holds whatever arrived before the error)."""
    messages: list[bytes] = []
    with grpc.insecure_channel(f'127.0.0.1:{port}') as channel:
        grpc.channel_ready_future(channel).result(timeout=_CALL_TIMEOUT)
        call = channel.unary_stream(
            method,
            request_serializer=lambda b: b,
            response_deserializer=lambda b: b,
        )
        try:
            for msg in call(payload, timeout=_CALL_TIMEOUT):
                messages.append(msg)
            return (messages, None)
        except grpc.RpcError as exc:  # noqa: BLE001 — surface the status
            return (messages, exc)


async def _server_stream(port: int, method: str, payload: bytes = b''):
    return await asyncio.to_thread(_blocking_server_stream, port, method, payload)


def _blocking_client_stream(port: int, method: str, payloads: list[bytes]):
    """Issue one blocking client-streaming call with a real grpcio channel.

    Streams every item of *payloads* as a request message and returns
    ``(ok, value_or_error)`` — ``(True, response_bytes)`` on success,
    ``(False, grpc.RpcError)`` on a non-OK status."""
    with grpc.insecure_channel(f'127.0.0.1:{port}') as channel:
        grpc.channel_ready_future(channel).result(timeout=_CALL_TIMEOUT)
        call = channel.stream_unary(
            method,
            request_serializer=lambda b: b,
            response_deserializer=lambda b: b,
        )
        try:
            return (True, call(iter(payloads), timeout=_CALL_TIMEOUT))
        except grpc.RpcError as exc:  # noqa: BLE001 — surface the status
            return (False, exc)


async def _client_stream(port: int, method: str, payloads: list[bytes]):
    return await asyncio.to_thread(_blocking_client_stream, port, method, payloads)


def _blocking_bidi(port: int, method: str, payloads: list[bytes]):
    """Issue one blocking bidirectional-streaming call (grpcio stream_stream).

    grpcio drives the request and response directions concurrently on one
    stream, so this is the real concurrent-read/write flow-control path.
    Returns ``(messages, error)`` like the server-streaming helper."""
    messages: list[bytes] = []
    with grpc.insecure_channel(f'127.0.0.1:{port}') as channel:
        grpc.channel_ready_future(channel).result(timeout=_CALL_TIMEOUT)
        call = channel.stream_stream(
            method,
            request_serializer=lambda b: b,
            response_deserializer=lambda b: b,
        )
        try:
            for msg in call(iter(payloads), timeout=_CALL_TIMEOUT):
                messages.append(msg)
            return (messages, None)
        except grpc.RpcError as exc:  # noqa: BLE001 — surface the status
            return (messages, exc)


async def _bidi(port: int, method: str, payloads: list[bytes]):
    return await asyncio.to_thread(_blocking_bidi, port, method, payloads)


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------

class TestRealClientSuccess:
    @pytest.mark.asyncio
    async def test_echo_round_trip(self, grpc_server_port):
        ok, value = await _unary(grpc_server_port, '/echo.Echo/Echo', b'blackbull')
        assert ok, f'unexpected error: {value}'
        assert value == b'llubkcalb'

    @pytest.mark.asyncio
    async def test_large_response_flow_control(self, grpc_server_port):
        # 100 KB response forces cross-window DATA framing to a strict client.
        ok, value = await _unary(grpc_server_port, '/echo.Echo/Big', b'x')
        assert ok, f'unexpected error: {value}'
        assert value == b'Z' * 100_000


# ---------------------------------------------------------------------------
# Compression (gzip) — real-client wire compatibility, both directions
# ---------------------------------------------------------------------------

class TestRealClientCompression:
    @pytest.mark.asyncio
    async def test_gzip_request_is_decompressed(self, grpc_server_port):
        # grpcio gzips the request; the server must decompress it before the
        # handler (which reverses the bytes) sees it.
        payload = b'compress-me' * 500
        ok, value = await _unary_gzip(grpc_server_port, '/echo.Echo/Echo', payload)
        assert ok, f'unexpected error: {value}'
        assert value == payload[::-1]

    @pytest.mark.asyncio
    async def test_gzip_large_response_round_trips(self, grpc_server_port):
        # 100 KB of 'Z' is highly compressible: the server gzips the response
        # (over threshold, client advertised gzip) and grpcio decompresses it.
        ok, value = await _unary_gzip(grpc_server_port, '/echo.Echo/Big', b'x')
        assert ok, f'unexpected error: {value}'
        assert value == b'Z' * 100_000


# ---------------------------------------------------------------------------
# GrpcContext (G3) — leading metadata + peer over a real socket
# ---------------------------------------------------------------------------

class TestRealClientContext:
    @pytest.mark.asyncio
    async def test_send_initial_metadata_reaches_client(self, grpc_server_port):
        response, initial_md = await _unary_with_initial_md(
            grpc_server_port, '/echo.Echo/Leading', b'x')
        assert response == b'body'
        # grpcio surfaces leading metadata lowercased as (key, value) pairs.
        assert ('x-leading', 'hi') in initial_md

    @pytest.mark.asyncio
    async def test_peer_is_derived_from_the_real_socket(self, grpc_server_port):
        ok, value = await _unary(grpc_server_port, '/echo.Echo/Peer', b'x')
        assert ok, f'unexpected error: {value}'
        # ASGI gives the server the client's address; peer() formats it grpc-style.
        assert value.startswith(b'ipv4:127.0.0.1:')


# ---------------------------------------------------------------------------
# Error path — the Trailers-Only regression this suite exists to guard.
# ---------------------------------------------------------------------------

class TestRealClientErrorStatus:
    """A strict client must decode every error class as its gRPC status —
    not UNKNOWN.  This is the regression the Sprint 58 Trailers-Only fix closes."""

    @pytest.mark.asyncio
    async def test_handler_crash_is_internal(self, grpc_server_port):
        ok, exc = await _unary(grpc_server_port, '/err.Err/Explode')
        assert not ok, 'expected an RpcError from a crashing handler'
        assert exc.code() == grpc.StatusCode.INTERNAL, exc

    @pytest.mark.asyncio
    async def test_grpc_error_status_and_message(self, grpc_server_port):
        ok, exc = await _unary(grpc_server_port, '/err.Err/Denied')
        assert not ok
        assert exc.code() == grpc.StatusCode.PERMISSION_DENIED, exc
        assert exc.details() == 'access denied'

    @pytest.mark.asyncio
    async def test_context_abort_status(self, grpc_server_port):
        ok, exc = await _unary(grpc_server_port, '/err.Err/Abort')
        assert not ok
        assert exc.code() == grpc.StatusCode.NOT_FOUND, exc
        assert exc.details() == 'item not found'

    @pytest.mark.asyncio
    async def test_unknown_method_is_unimplemented(self, grpc_server_port):
        ok, exc = await _unary(grpc_server_port, '/nope.No/Method')
        assert not ok
        assert exc.code() == grpc.StatusCode.UNIMPLEMENTED, exc


# ---------------------------------------------------------------------------
# Server-streaming — a real grpcio unary_stream call (the ghz stream-grpc shape).
# ---------------------------------------------------------------------------

class TestRealClientServerStreaming:
    @pytest.mark.asyncio
    async def test_stream_yields_messages_in_order(self, grpc_server_port):
        msgs, err = await _server_stream(grpc_server_port, '/echo.Echo/Stream', b'5')
        assert err is None, f'unexpected stream error: {err}'
        assert msgs == [f'msg{i}'.encode() for i in range(5)]

    @pytest.mark.asyncio
    async def test_empty_stream(self, grpc_server_port):
        msgs, err = await _server_stream(grpc_server_port, '/echo.Echo/Stream', b'0')
        assert err is None, f'unexpected stream error: {err}'
        assert msgs == []

    @pytest.mark.asyncio
    async def test_large_stream_flow_control(self, grpc_server_port):
        # 5000 messages — the HttpArena StreamSum shape; exercises multi-DATA
        # flow control end-to-end with a strict client.
        msgs, err = await _server_stream(grpc_server_port, '/echo.Echo/Stream', b'5000')
        assert err is None, f'unexpected stream error: {err}'
        assert len(msgs) == 5000
        assert msgs[0] == b'msg0' and msgs[-1] == b'msg4999'

    @pytest.mark.asyncio
    async def test_mid_stream_error_surfaces_after_message(self, grpc_server_port):
        msgs, err = await _server_stream(grpc_server_port, '/echo.Echo/StreamBoom', b'')
        # The delivered message arrives, then the error status in trailers.
        assert msgs == [b'first']
        assert err is not None
        assert err.code() == grpc.StatusCode.PERMISSION_DENIED, err
        assert err.details() == 'stop'


# ---------------------------------------------------------------------------
# Client-streaming — a real grpcio stream_unary call (Sprint 60 G1a).
# ---------------------------------------------------------------------------

class TestRealClientClientStreaming:
    @pytest.mark.asyncio
    async def test_collects_all_request_messages(self, grpc_server_port):
        ok, value = await _client_stream(
            grpc_server_port, '/echo.Echo/Collect', [b'a', b'bb', b'ccc'])
        assert ok, f'unexpected error: {value}'
        assert value == b'a,bb,ccc'

    @pytest.mark.asyncio
    async def test_empty_request_stream(self, grpc_server_port):
        ok, value = await _client_stream(grpc_server_port, '/echo.Echo/Collect', [])
        assert ok, f'unexpected error: {value}'
        assert value == b''

    @pytest.mark.asyncio
    async def test_large_request_stream_flow_control(self, grpc_server_port):
        # 200 × 1 KiB request messages (~200 KiB) crosses the 65535-byte initial
        # window on the *request* direction — exercises inbound flow control with
        # a strict client, the mirror of the server-streaming large-response case.
        payloads = [b'x' * 1024] * 200
        ok, value = await _client_stream(
            grpc_server_port, '/echo.Echo/CountBytes', payloads)
        assert ok, f'unexpected error: {value}'
        assert value == str(200 * 1024).encode()


# ---------------------------------------------------------------------------
# Bidirectional streaming — a real grpcio stream_stream call (Sprint 60 G1b).
# ---------------------------------------------------------------------------

class TestRealClientBidiStreaming:
    @pytest.mark.asyncio
    async def test_echo_each_message(self, grpc_server_port):
        msgs, err = await _bidi(
            grpc_server_port, '/echo.Echo/Chat', [b'a', b'bb', b'ccc'])
        assert err is None, f'unexpected bidi error: {err}'
        assert msgs == [b'a', b'bb', b'ccc']

    @pytest.mark.asyncio
    async def test_empty_stream(self, grpc_server_port):
        msgs, err = await _bidi(grpc_server_port, '/echo.Echo/Chat', [])
        assert err is None, f'unexpected bidi error: {err}'
        assert msgs == []

    @pytest.mark.asyncio
    async def test_both_directions_within_window(self, grpc_server_port):
        # 20 × 1 KiB (~20 KiB) each way — comfortably under both the 65535-byte
        # initial window and the 64-deep recipient queue, so neither direction
        # back-pressures.  Bidi's supported envelope: concurrent read/write on
        # one stream, kept clear of the enqueue-time-crediting boundary.
        payloads = [bytes([i % 256]) * 1024 for i in range(20)]
        msgs, err = await _bidi(grpc_server_port, '/echo.Echo/Chat', payloads)
        assert err is None, f'unexpected bidi error: {err}'
        assert msgs == payloads

    @pytest.mark.xfail(strict=False, reason=(
        'Large concurrent bidi crosses the 65535-byte window BOTH ways; the '
        'server credits inbound flow control on enqueue, not on app consumption, '
        'so a handler that stalls reading (blocked on yield / response '
        'back-pressure) overflows the 64-deep recipient queue and the stream is '
        'RST_STREAM(ENHANCE_YOUR_CALM). Needs consume-based inbound flow control '
        '(proposals/consume-based-inbound-flow-control.md). Intermittent, so '
        'strict=False.'))
    @pytest.mark.asyncio
    async def test_large_both_directions_over_window(self, grpc_server_port):
        # 200 × 1 KiB echoed back — BOTH directions cross the initial window
        # concurrently.  Known limitation until consume-based crediting lands.
        payloads = [bytes([i % 256]) * 1024 for i in range(200)]
        msgs, err = await _bidi(grpc_server_port, '/echo.Echo/Chat', payloads)
        assert err is None, f'unexpected bidi error: {err}'
        assert msgs == payloads


# ---------------------------------------------------------------------------
# Concurrency — the ghz load shape (many multiplexed streams / one channel).
# ---------------------------------------------------------------------------

class TestRealClientConcurrency:
    @pytest.mark.asyncio
    async def test_many_concurrent_calls_one_channel(self, grpc_server_port):
        async def one(i: int):
            payload = f'msg{i}'.encode()
            ok, value = await _unary(grpc_server_port, '/echo.Echo/Echo', payload)
            assert ok, f'call {i} failed: {value}'
            assert value == payload[::-1]

        await asyncio.gather(*(one(i) for i in range(50)))

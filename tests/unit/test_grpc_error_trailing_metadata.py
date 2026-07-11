"""Error paths carry handler-set trailing metadata (Sprint 66 core hook).

grpcio delivers ``set_trailing_metadata`` even when the call ends non-OK —
that is what the rich-error model rides on: ``google.rpc.Status`` bytes in a
``grpc-status-details-bin`` trailer attached before ``abort``.  BlackBull's
error writers previously emitted only ``grpc-status`` / ``grpc-message`` and
dropped ``context._trailing``; these tests pin the grpcio-compatible contract
for every handler-error shape (Trailers-Only, after-HEADERS, mid-stream,
unhandled exception, deadline).
"""
import asyncio

import pytest

from blackbull.grpc import (
    GrpcServiceRegistry, GrpcStatus, GrpcError, encode_message, decode_messages,
)
from blackbull.grpc.asgi import serve_grpc


_DETAILS_BIN = (b'grpc-status-details-bin', b'CAMSB2JhZCBhcmc')


def _grpc_scope(path, headers=None):
    base = [(b'content-type', b'application/grpc'), (b':method', b'POST')]
    if headers:
        base += headers
    return {'type': 'http', 'path': path, 'headers': base}


def _receive_with(body: bytes):
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {'type': 'http.request', 'body': body, 'more_body': False}
        return {'type': 'http.request', 'body': b'', 'more_body': False}
    return receive


def _collector():
    events = []

    async def send(event):
        events.append(event)
    return events, send


def _trailing_headers(events):
    """Headers of the http.response.trailers event (the terminal frame)."""
    for e in events:
        if e['type'] == 'http.response.trailers':
            return dict(e['headers'])
    raise AssertionError('no http.response.trailers event emitted')


@pytest.mark.asyncio
async def test_unary_abort_carries_trailing_metadata():
    """Trailers-Only error (nothing sent yet) includes context trailing
    metadata alongside grpc-status/grpc-message."""
    reg = GrpcServiceRegistry()

    @reg.method('/x/Abort')
    async def h(request, context):
        context.set_trailing_metadata([_DETAILS_BIN])
        context.abort(GrpcStatus.INVALID_ARGUMENT, 'bad arg')

    events, send = _collector()
    await serve_grpc(reg, _grpc_scope('/x/Abort'),
                     _receive_with(encode_message(b'')), send)
    tr = _trailing_headers(events)
    assert tr[b'grpc-status'] == str(int(GrpcStatus.INVALID_ARGUMENT)).encode()
    assert tr[_DETAILS_BIN[0]] == _DETAILS_BIN[1]


@pytest.mark.asyncio
async def test_unary_abort_after_initial_metadata_carries_trailing_metadata():
    """Error after HEADERS already went out (send_initial_metadata) still
    delivers the trailing metadata in the trailing HEADERS frame."""
    reg = GrpcServiceRegistry()

    @reg.method('/x/AbortLate')
    async def h(request, context):
        await context.send_initial_metadata([(b'x-lead', b'1')])
        context.set_trailing_metadata([_DETAILS_BIN])
        raise GrpcError(GrpcStatus.FAILED_PRECONDITION, 'nope')

    events, send = _collector()
    await serve_grpc(reg, _grpc_scope('/x/AbortLate'),
                     _receive_with(encode_message(b'')), send)
    assert events[0]['type'] == 'http.response.start'
    tr = _trailing_headers(events)
    assert tr[b'grpc-status'] == str(int(GrpcStatus.FAILED_PRECONDITION)).encode()
    assert tr[_DETAILS_BIN[0]] == _DETAILS_BIN[1]


@pytest.mark.asyncio
async def test_streaming_midstream_error_carries_trailing_metadata():
    """A server-streaming handler that yielded a message and then aborts
    reports the status *and* the trailing metadata in the trailers."""
    reg = GrpcServiceRegistry()

    @reg.method('/x/StreamFail')
    async def h(request, context):
        yield b'one'
        context.set_trailing_metadata([_DETAILS_BIN])
        raise GrpcError(GrpcStatus.RESOURCE_EXHAUSTED, 'over quota')

    events, send = _collector()
    await serve_grpc(reg, _grpc_scope('/x/StreamFail'),
                     _receive_with(encode_message(b'')), send)
    # The yielded message was committed before the error.
    bodies = b''.join(e['body'] for e in events if e['type'] == 'http.response.body')
    assert decode_messages(bodies) == [(False, b'one')]
    tr = _trailing_headers(events)
    assert tr[b'grpc-status'] == str(int(GrpcStatus.RESOURCE_EXHAUSTED)).encode()
    assert tr[_DETAILS_BIN[0]] == _DETAILS_BIN[1]


@pytest.mark.asyncio
async def test_unhandled_exception_carries_trailing_metadata():
    """INTERNAL (handler bug isolation) also delivers trailing metadata set
    before the crash — matches grpcio, which aborts with the context's
    trailing metadata regardless of how the RPC failed."""
    reg = GrpcServiceRegistry()

    @reg.method('/x/Boom')
    async def h(request, context):
        context.set_trailing_metadata([_DETAILS_BIN])
        raise RuntimeError('boom')

    events, send = _collector()
    await serve_grpc(reg, _grpc_scope('/x/Boom'),
                     _receive_with(encode_message(b'')), send)
    tr = _trailing_headers(events)
    assert tr[b'grpc-status'] == str(int(GrpcStatus.INTERNAL)).encode()
    assert tr[_DETAILS_BIN[0]] == _DETAILS_BIN[1]


@pytest.mark.asyncio
async def test_deadline_exceeded_carries_trailing_metadata():
    """DEADLINE_EXCEEDED delivers trailing metadata the handler set before
    stalling."""
    reg = GrpcServiceRegistry()

    @reg.method('/x/Slow')
    async def h(request, context):
        context.set_trailing_metadata([_DETAILS_BIN])
        await asyncio.sleep(60)
        return b''

    events, send = _collector()
    await serve_grpc(
        reg,
        _grpc_scope('/x/Slow', headers=[(b'grpc-timeout', b'10m')]),
        _receive_with(encode_message(b'')), send)
    tr = _trailing_headers(events)
    assert tr[b'grpc-status'] == str(int(GrpcStatus.DEADLINE_EXCEEDED)).encode()
    assert tr[_DETAILS_BIN[0]] == _DETAILS_BIN[1]


def test_trailing_metadata_getter_returns_copy():
    """GrpcContext.trailing_metadata() exposes what was set (as a copy), so
    helper packages can append rather than clobber."""
    from blackbull.grpc.asgi import GrpcContext
    ctx = GrpcContext(_grpc_scope('/x/Y'))
    assert ctx.trailing_metadata() == []
    ctx.set_trailing_metadata([(b'a', b'1')])
    got = ctx.trailing_metadata()
    assert got == [(b'a', b'1')]
    got.append((b'b', b'2'))
    assert ctx.trailing_metadata() == [(b'a', b'1')]


@pytest.mark.asyncio
async def test_success_trailing_metadata_unchanged():
    """Regression guard: the success path keeps delivering trailing metadata
    exactly as before."""
    reg = GrpcServiceRegistry()

    @reg.method('/x/Ok')
    async def h(request, context):
        context.set_trailing_metadata([(b'x-ok', b'yes')])
        return b'fine'

    events, send = _collector()
    await serve_grpc(reg, _grpc_scope('/x/Ok'),
                     _receive_with(encode_message(b'')), send)
    tr = _trailing_headers(events)
    assert tr[b'grpc-status'] == b'0'
    assert tr[b'x-ok'] == b'yes'

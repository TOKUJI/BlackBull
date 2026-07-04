"""Conformance: the fuller GrpcContext (Sprint 60 G3).

Adds the parts of grpcio's ``ServicerContext`` a raw-bytes transport can honour:
``time_remaining()`` (from ``grpc-timeout``), ``peer()`` (from the ASGI
``client``), ``invocation_metadata()`` (request headers), and
``send_initial_metadata()`` (flush leading response metadata / HEADERS early).

Unit tests construct a context directly; behaviour tests drive ``serve_grpc``
with the in-process collector harness so the response-start machinery is
exercised end to end.
"""
from __future__ import annotations

import pytest

from blackbull.grpc import (
    GrpcServiceRegistry, GrpcStatus, encode_message, decode_messages,
)
from blackbull.grpc.asgi import serve_grpc, GrpcContext


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------

def _grpc_scope(path, headers=None, client=None):
    base = [(b'content-type', b'application/grpc'), (b':method', b'POST')]
    scope = {'type': 'http', 'path': path,
             'headers': headers if headers is not None else base}
    if client is not None:
        scope['client'] = client
    return scope


def _receive_with(body: bytes):
    async def receive():
        return {'type': 'http.request', 'body': body, 'more_body': False}
    return receive


def _collector():
    events = []

    async def send(event):
        events.append(event)
    return events, send


def _start_headers(events) -> dict:
    for e in events:
        if e['type'] == 'http.response.start':
            return dict(e['headers'])
    return {}


def _messages(events) -> list[bytes]:
    out = []
    for e in events:
        if e['type'] == 'http.response.body' and e.get('body'):
            for _c, payload in decode_messages(e['body']):
                out.append(payload)
    return out


def _trailers(events) -> dict:
    return dict(events[-1]['headers'])


# --------------------------------------------------------------------------
# peer()
# --------------------------------------------------------------------------

class TestPeer:
    def test_ipv4_peer(self):
        ctx = GrpcContext(_grpc_scope('/svc/M', client=('127.0.0.1', 55123)))
        assert ctx.peer() == 'ipv4:127.0.0.1:55123'

    def test_ipv6_peer(self):
        ctx = GrpcContext(_grpc_scope('/svc/M', client=('::1', 9000)))
        assert ctx.peer() == 'ipv6:[::1]:9000'

    def test_missing_client_is_empty(self):
        assert GrpcContext(_grpc_scope('/svc/M')).peer() == ''


# --------------------------------------------------------------------------
# invocation_metadata()
# --------------------------------------------------------------------------

class TestInvocationMetadata:
    def test_returns_headers_without_pseudo_headers(self):
        headers = [
            (b':method', b'POST'), (b':path', b'/svc/M'),
            (b'content-type', b'application/grpc'),
            (b'x-token', b'abc'),
        ]
        ctx = GrpcContext(_grpc_scope('/svc/M', headers=headers))
        md = ctx.invocation_metadata()
        assert (b'x-token', b'abc') in md
        assert (b'content-type', b'application/grpc') in md
        assert all(not k.startswith(b':') for k, _ in md)


# --------------------------------------------------------------------------
# time_remaining()
# --------------------------------------------------------------------------

class TestTimeRemaining:
    @pytest.mark.asyncio
    async def test_none_without_deadline(self):
        reg = GrpcServiceRegistry()
        seen = {}

        @reg.method('/svc/M')
        async def m(request, context):
            seen['tr'] = context.time_remaining()
            return b'ok'

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/M'),
                         _receive_with(encode_message(b'')), send)
        assert seen['tr'] is None

    @pytest.mark.asyncio
    async def test_counts_down_from_grpc_timeout(self):
        reg = GrpcServiceRegistry()
        seen = {}

        @reg.method('/svc/M')
        async def m(request, context):
            seen['tr'] = context.time_remaining()
            return b'ok'

        scope = _grpc_scope('/svc/M', headers=[
            (b'content-type', b'application/grpc'), (b'grpc-timeout', b'10S')])
        events, send = _collector()
        await serve_grpc(reg, scope, _receive_with(encode_message(b'')), send)
        # Measured immediately, so almost the full 10 s remains.
        assert 9.0 < seen['tr'] <= 10.0

    @pytest.mark.asyncio
    async def test_never_negative(self):
        # A hand-bound context past its deadline reports 0, not a negative.
        ctx = GrpcContext(_grpc_scope('/svc/M'))
        ctx._bind(send=None, content_type=b'application/grpc',
                  response_encoding=None, deadline=-5.0)
        assert ctx.time_remaining() == 0.0


# --------------------------------------------------------------------------
# send_initial_metadata()
# --------------------------------------------------------------------------

class TestSendInitialMetadata:
    @pytest.mark.asyncio
    async def test_unary_leading_metadata_in_headers(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/M')
        async def m(request, context):
            await context.send_initial_metadata([(b'x-leading', b'yes')])
            return b'ok'

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/M'),
                         _receive_with(encode_message(b'')), send)

        # Exactly one start event, carrying the leading metadata.
        assert sum(e['type'] == 'http.response.start' for e in events) == 1
        assert _start_headers(events)[b'x-leading'] == b'yes'
        assert _messages(events) == [b'ok']
        assert _trailers(events)[b'grpc-status'] == b'0'

    @pytest.mark.asyncio
    async def test_error_after_initial_metadata_rides_trailers(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/M')
        async def m(request, context):
            await context.send_initial_metadata([(b'x-leading', b'1')])
            context.abort(GrpcStatus.INTERNAL, 'boom')  # raises GrpcError

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/M'),
                         _receive_with(encode_message(b'')), send)

        # HEADERS already went out (with the metadata), so the error must ride
        # the trailing HEADERS — NOT a fresh Trailers-Only response.
        assert [e['type'] for e in events] == \
            ['http.response.start', 'http.response.trailers']
        assert _start_headers(events)[b'x-leading'] == b'1'
        assert _trailers(events)[b'grpc-status'] == \
            str(int(GrpcStatus.INTERNAL)).encode()

    @pytest.mark.asyncio
    async def test_second_call_raises_value_error(self):
        ctx = GrpcContext(_grpc_scope('/svc/M'))
        events, send = _collector()
        ctx._bind(send, b'application/grpc', None, None)
        await ctx.send_initial_metadata([(b'a', b'1')])
        with pytest.raises(ValueError):
            await ctx.send_initial_metadata([(b'b', b'2')])

    @pytest.mark.asyncio
    async def test_server_streaming_leading_metadata(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Down')
        async def down(request, context):
            await context.send_initial_metadata([(b'x-stream', b'go')])
            yield b'a'
            yield b'b'

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Down'),
                         _receive_with(encode_message(b'')), send)

        assert sum(e['type'] == 'http.response.start' for e in events) == 1
        assert _start_headers(events)[b'x-stream'] == b'go'
        assert _messages(events) == [b'a', b'b']
        assert _trailers(events)[b'grpc-status'] == b'0'

"""Conformance: server-streaming gRPC over the ASGI bridge (Sprint 58).

Drives ``serve_grpc`` with the in-process ``_collector`` harness (no socket) to
assert the ASGI event shape of a server-streaming response and its edge cases:
empty stream, mid-stream error, error before the first message, per-message
size enforcement, deadline, and generator finalisation on cancellation.  The
real-client (grpcio) wire test lives in ``test_grpc_real_client_h2c.py``.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.grpc import (
    GrpcServiceRegistry, GrpcStatus, GrpcError, encode_message, decode_messages,
)
from blackbull.grpc.asgi import serve_grpc
import blackbull.grpc.asgi as grpc_asgi


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------

def _grpc_scope(path, headers=None):
    base = [(b'content-type', b'application/grpc'), (b':method', b'POST')]
    return {'type': 'http', 'path': path,
            'headers': headers if headers is not None else base}


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


def _messages(events) -> list[bytes]:
    """Decode every response body event into its single message payload."""
    out = []
    for e in events:
        if e['type'] == 'http.response.body' and e.get('body'):
            for _compressed, payload in decode_messages(e['body']):
                out.append(payload)
    return out


def _trailers(events) -> dict:
    return dict(events[-1]['headers'])


# --------------------------------------------------------------------------
# Registry — streaming detection
# --------------------------------------------------------------------------

class TestStreamingDetection:
    def test_async_generator_is_detected_as_streaming(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Stream')
        async def s(request, context):
            yield b'x'

        assert reg.lookup_method('/svc/Stream').streaming is True

    def test_coroutine_is_unary(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Unary')
        async def u(request, context):
            return b'x'

        assert reg.lookup_method('/svc/Unary').streaming is False

    def test_explicit_streaming_override_on_wrapper(self):
        reg = GrpcServiceRegistry()

        # A plain coroutine forced to streaming (e.g. a decorator hid the gen).
        async def not_obviously_a_gen(request, context):
            yield b'x'

        reg.add_method('/svc/Forced', not_obviously_a_gen, streaming=True)
        assert reg.lookup_method('/svc/Forced').streaming is True

    def test_streaming_false_on_async_gen_is_rejected(self):
        reg = GrpcServiceRegistry()

        async def gen(request, context):
            yield b'x'

        with pytest.raises(ValueError, match='async generator'):
            reg.add_method('/svc/Bad', gen, streaming=False)


# --------------------------------------------------------------------------
# Event shape
# --------------------------------------------------------------------------

class TestServerStreamingShape:
    @pytest.mark.asyncio
    async def test_stream_event_sequence(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Stream')
        async def s(request, context):
            for i in range(3):
                yield f'm{i}'.encode()

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Stream'),
                         _receive_with(encode_message(b'')), send)

        types = [e['type'] for e in events]
        # Response-Headers first, trailing HEADERS last, at least one DATA event
        # between.  How many DATA events is an implementation detail: consecutive
        # messages coalesce into one DATA frame (valid — clients frame on the
        # 5-byte gRPC prefix, not DATA boundaries), so assert the shape and the
        # decoded message stream, not the frame count.
        assert types[0] == 'http.response.start'
        assert types[-1] == 'http.response.trailers'
        body_events = [e for e in events if e['type'] == 'http.response.body']
        assert len(body_events) >= 1
        assert all(t == 'http.response.body' for t in types[1:-1])
        # Response-Headers announce trailers and carry no grpc-status themselves.
        assert events[0].get('trailers') is True
        assert b'grpc-status' not in dict(events[0]['headers'])
        # Every DATA event keeps the stream open; all messages arrive, in order.
        assert all(e['more_body'] for e in body_events)
        assert _messages(events) == [b'm0', b'm1', b'm2']
        # Status rides the trailing HEADERS frame.
        assert _trailers(events)[b'grpc-status'] == b'0'

    @pytest.mark.asyncio
    async def test_trailing_metadata_and_code_in_trailers(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Meta')
        async def s(request, context):
            context.set_trailing_metadata([(b'x-count', b'2')])
            yield b'a'
            yield b'b'

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Meta'),
                         _receive_with(encode_message(b'')), send)
        tr = _trailers(events)
        assert tr[b'grpc-status'] == b'0'
        assert tr[b'x-count'] == b'2'

    @pytest.mark.asyncio
    async def test_empty_stream_is_headers_then_trailers(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Empty')
        async def s(request, context):
            return
            yield  # pragma: no cover — makes this an async generator

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Empty'),
                         _receive_with(encode_message(b'')), send)
        types = [e['type'] for e in events]
        assert types == ['http.response.start', 'http.response.trailers']
        assert _trailers(events)[b'grpc-status'] == b'0'


# --------------------------------------------------------------------------
# Error paths
# --------------------------------------------------------------------------

class TestServerStreamingErrors:
    @pytest.mark.asyncio
    async def test_mid_stream_error_reports_status_in_trailers(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Boom')
        async def s(request, context):
            yield b'first'
            raise RuntimeError('mid-stream boom')

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Boom'),
                         _receive_with(encode_message(b'')), send)
        types = [e['type'] for e in events]
        # Headers + the one delivered message + error trailers.
        assert types == ['http.response.start', 'http.response.body',
                         'http.response.trailers']
        assert _messages(events) == [b'first']
        assert _trailers(events)[b'grpc-status'] == \
            str(int(GrpcStatus.INTERNAL)).encode()

    @pytest.mark.asyncio
    async def test_error_before_first_message_is_trailers_only(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Early')
        async def s(request, context):
            raise GrpcError(GrpcStatus.NOT_FOUND, 'gone')
            yield  # pragma: no cover — async generator

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Early'),
                         _receive_with(encode_message(b'')), send)
        types = [e['type'] for e in events]
        assert types == ['http.response.start', 'http.response.trailers']
        tr = _trailers(events)
        assert tr[b'grpc-status'] == str(int(GrpcStatus.NOT_FOUND)).encode()
        assert tr[b'grpc-message'] == b'gone'

    @pytest.mark.asyncio
    async def test_oversized_response_message_is_resource_exhausted(self, monkeypatch):
        monkeypatch.setattr(grpc_asgi, 'MAX_MESSAGE_SIZE', 1024)
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Big')
        async def s(request, context):
            yield b'x' * 2048

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Big'),
                         _receive_with(encode_message(b'')), send)
        # First message rejected before headers → Trailers-Only.
        assert [e['type'] for e in events] == \
            ['http.response.start', 'http.response.trailers']
        assert _trailers(events)[b'grpc-status'] == \
            str(int(GrpcStatus.RESOURCE_EXHAUSTED)).encode()

    @pytest.mark.asyncio
    async def test_deadline_before_first_message(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Slow')
        async def s(request, context):
            await asyncio.sleep(0.2)
            yield b'late'

        events, send = _collector()
        # grpc-timeout 1m = 1 ms; the 200 ms sleep blows it before any message.
        scope = _grpc_scope('/svc/Slow', headers=[
            (b'content-type', b'application/grpc'),
            (b'grpc-timeout', b'1m')])
        await serve_grpc(reg, scope, _receive_with(encode_message(b'')), send)
        assert [e['type'] for e in events] == \
            ['http.response.start', 'http.response.trailers']
        assert _trailers(events)[b'grpc-status'] == \
            str(int(GrpcStatus.DEADLINE_EXCEEDED)).encode()


# --------------------------------------------------------------------------
# Cancellation
# --------------------------------------------------------------------------

class TestServerStreamingCancellation:
    @pytest.mark.asyncio
    async def test_generator_is_finalized_on_cancel(self):
        reg = GrpcServiceRegistry()
        closed = {'v': False}

        @reg.method('/svc/Long')
        async def s(request, context):
            try:
                i = 0
                while True:
                    yield f'm{i}'.encode()
                    i += 1
                    await asyncio.sleep(0.01)
            finally:
                # Runs when serve_grpc aclose()s the generator on cancellation.
                closed['v'] = True

        events, send = _collector()
        task = asyncio.create_task(
            serve_grpc(reg, _grpc_scope('/svc/Long'),
                       _receive_with(encode_message(b'')), send))
        await asyncio.sleep(0.05)  # let a few messages stream
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert closed['v'] is True, 'handler generator was not finalised on cancel'
        # It really did stream before we cancelled.
        assert len(_messages(events)) >= 1

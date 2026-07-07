"""Conformance: client-streaming gRPC over the ASGI bridge (Sprint 60 G1a).

Client-streaming = the *request* is a stream of messages, the *response* is a
single message.  The handler takes an async iterator (``request_iter``) instead
of a single ``request: bytes`` and returns one reply.

These drive ``serve_grpc`` with the in-process ``_collector`` harness (no
socket) to pin the registry detection, the incremental LPM de-framer (messages
split arbitrarily across ``http.request`` events), and the error/deadline paths.
The real-client (grpcio) wire test lives in ``test_grpc_real_client_h2c.py``.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.grpc import (
    GrpcServiceRegistry, GrpcStatus, encode_message, decode_messages,
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


def _receive_chunks(chunks: list[bytes]):
    """Deliver *chunks* as successive ``http.request`` events.

    All but the last carry ``more_body: True``; the empty tail (or the sole
    chunk) closes the stream.  Lets a test split the framed request bytes at
    arbitrary offsets to exercise the de-framer's residual buffer.
    """
    queue = list(chunks) or [b'']

    async def receive():
        if queue:
            chunk = queue.pop(0)
            return {'type': 'http.request', 'body': chunk,
                    'more_body': bool(queue)}
        return {'type': 'http.request', 'body': b'', 'more_body': False}
    return receive


def _collector():
    events = []

    async def send(event):
        events.append(event)
    return events, send


def _messages(events) -> list[bytes]:
    out = []
    for e in events:
        if e['type'] == 'http.response.body' and e.get('body'):
            for _compressed, payload in decode_messages(e['body']):
                out.append(payload)
    return out


def _trailers(events) -> dict:
    return dict(events[-1]['headers'])


# --------------------------------------------------------------------------
# Registry — client-streaming detection
# --------------------------------------------------------------------------

class TestClientStreamingDetection:
    def test_request_iter_param_is_client_streaming(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Collect')
        async def c(request_iter, context) -> bytes:
            return b'x'

        m = reg.lookup_method('/svc/Collect')
        assert m.client_streaming is True
        assert m.streaming is False

    def test_plain_request_param_is_request_unary(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Unary')
        async def u(request, context) -> bytes:
            return b'x'

        m = reg.lookup_method('/svc/Unary')
        assert m.client_streaming is False
        assert m.streaming is False

    def test_explicit_client_streaming_override(self):
        reg = GrpcServiceRegistry()

        async def hidden(req, context) -> bytes:  # name doesn't signal it
            return b'x'

        reg.add_method('/svc/Forced', hidden, client_streaming=True)
        assert reg.lookup_method('/svc/Forced').client_streaming is True

    def test_bidi_is_both_axes(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Chat')
        async def chat(request_iter, context):
            yield b'x'

        m = reg.lookup_method('/svc/Chat')
        assert m.client_streaming is True
        assert m.streaming is True

    def test_server_streaming_stays_request_unary(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Down')
        async def down(request, context):
            yield b'x'

        m = reg.lookup_method('/svc/Down')
        assert m.client_streaming is False
        assert m.streaming is True


# --------------------------------------------------------------------------
# Behaviour — request de-framing + single reply
# --------------------------------------------------------------------------

class TestClientStreamingShape:
    @pytest.mark.asyncio
    async def test_collects_all_request_messages_in_one_event(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Join')
        async def join(request_iter, context) -> bytes:
            return b','.join([m async for m in request_iter])

        body = encode_message(b'aa') + encode_message(b'bbb') + encode_message(b'c')
        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Join'), _receive_chunks([body]), send)

        assert [e['type'] for e in events] == \
            ['http.response.start', 'http.response.body', 'http.response.trailers']
        assert _messages(events) == [b'aa,bbb,c']
        assert _trailers(events)[b'grpc-status'] == b'0'

    @pytest.mark.asyncio
    async def test_messages_split_across_events(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Join')
        async def join(request_iter, context) -> bytes:
            return b','.join([m async for m in request_iter])

        full = encode_message(b'hello') + encode_message(b'world')
        # Split so a single logical message straddles the event boundary.
        mid = len(full) // 2
        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Join'),
                         _receive_chunks([full[:mid], full[mid:]]), send)
        assert _messages(events) == [b'hello,world']
        assert _trailers(events)[b'grpc-status'] == b'0'

    @pytest.mark.asyncio
    async def test_byte_at_a_time_delivery(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Count')
        async def count(request_iter, context) -> bytes:
            n = 0
            async for _ in request_iter:
                n += 1
            return str(n).encode()

        full = b''.join(encode_message(m) for m in (b'a', b'bb', b'ccc'))
        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Count'),
                         _receive_chunks([full[i:i + 1] for i in range(len(full))]),
                         send)
        assert _messages(events) == [b'3']

    @pytest.mark.asyncio
    async def test_empty_request_stream(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Count')
        async def count(request_iter, context) -> bytes:
            return str(len([m async for m in request_iter])).encode()

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Count'), _receive_chunks([b'']), send)
        assert _messages(events) == [b'0']
        assert _trailers(events)[b'grpc-status'] == b'0'


# --------------------------------------------------------------------------
# Error paths
# --------------------------------------------------------------------------

class TestClientStreamingErrors:
    @pytest.mark.asyncio
    async def test_abort_is_trailers_only(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Nope')
        async def nope(request_iter, context) -> bytes:
            context.abort(GrpcStatus.INVALID_ARGUMENT, 'bad')
            return b''  # pragma: no cover

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Nope'),
                         _receive_chunks([encode_message(b'x')]), send)
        assert [e['type'] for e in events] == \
            ['http.response.start', 'http.response.trailers']
        tr = _trailers(events)
        assert tr[b'grpc-status'] == str(int(GrpcStatus.INVALID_ARGUMENT)).encode()
        assert tr[b'grpc-message'] == b'bad'

    @pytest.mark.asyncio
    async def test_malformed_frame_is_internal(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Join')
        async def join(request_iter, context) -> bytes:
            return b','.join([m async for m in request_iter])

        # Truncated LPM prefix (declares 5-byte body, provides 1).
        bad = encode_message(b'ok') + b'\x00\x00\x00\x00\x05z'
        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Join'), _receive_chunks([bad]), send)
        assert _trailers(events)[b'grpc-status'] == \
            str(int(GrpcStatus.INTERNAL)).encode()

    @pytest.mark.asyncio
    async def test_oversized_request_message_is_resource_exhausted(self, monkeypatch):
        monkeypatch.setattr(grpc_asgi, 'MAX_MESSAGE_SIZE', 1024)
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Join')
        async def join(request_iter, context) -> bytes:
            return b','.join([m async for m in request_iter])

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Join'),
                         _receive_chunks([encode_message(b'x' * 2048)]), send)
        assert _trailers(events)[b'grpc-status'] == \
            str(int(GrpcStatus.RESOURCE_EXHAUSTED)).encode()

    @pytest.mark.asyncio
    async def test_deadline_while_draining(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Slow')
        async def slow(request_iter, context) -> bytes:
            async for _ in request_iter:
                await asyncio.sleep(0.2)
            return b'done'

        scope = _grpc_scope('/svc/Slow', headers=[
            (b'content-type', b'application/grpc'), (b'grpc-timeout', b'1m')])
        events, send = _collector()
        await serve_grpc(reg, scope, _receive_chunks([encode_message(b'x')]), send)
        assert _trailers(events)[b'grpc-status'] == \
            str(int(GrpcStatus.DEADLINE_EXCEEDED)).encode()

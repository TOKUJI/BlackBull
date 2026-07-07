"""Conformance: bidirectional-streaming gRPC over the ASGI bridge (Sprint 60 G1b).

Bidi = the request is a stream of messages *and* the response is a stream.  The
handler is an async generator taking an async iterator (``request_iter``) and
``yield``-ing responses; the registry detects it as both request- and
response-streaming.  Dispatch reuses ``_serve_server_streaming`` with the
request iterator (client-streaming's ``_iter_request_messages``), so bidi added
no new writer — these tests pin the request→response mapping and the error /
empty / deadline paths.  The real-client (grpcio ``stream_stream``) wire test,
including the large-both-directions no-deadlock gate, lives in
``test_grpc_real_client_h2c.py``.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.grpc import (
    GrpcServiceRegistry, GrpcStatus, GrpcError, encode_message, decode_messages,
)
from blackbull.grpc.asgi import serve_grpc


def _grpc_scope(path, headers=None):
    base = [(b'content-type', b'application/grpc'), (b':method', b'POST')]
    return {'type': 'http', 'path': path,
            'headers': headers if headers is not None else base}


def _receive_chunks(chunks: list[bytes]):
    queue = list(chunks) or [b'']

    async def receive():
        if queue:
            chunk = queue.pop(0)
            return {'type': 'http.request', 'body': chunk, 'more_body': bool(queue)}
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


def _framed(*messages: bytes) -> bytes:
    return b''.join(encode_message(m) for m in messages)


class TestBidiShape:
    @pytest.mark.asyncio
    async def test_echo_each_request_message(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Chat')
        async def chat(request_iter, context):
            async for msg in request_iter:
                yield b'echo:' + msg

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Chat'),
                         _receive_chunks([_framed(b'a', b'bb', b'ccc')]), send)

        assert events[0]['type'] == 'http.response.start'
        assert events[-1]['type'] == 'http.response.trailers'
        assert _messages(events) == [b'echo:a', b'echo:bb', b'echo:ccc']
        assert _trailers(events)[b'grpc-status'] == b'0'

    @pytest.mark.asyncio
    async def test_response_count_can_differ_from_request_count(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Double')
        async def double(request_iter, context):
            # Two responses per request — response cardinality is independent.
            async for msg in request_iter:
                yield msg
                yield msg[::-1]

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Double'),
                         _receive_chunks([_framed(b'ab', b'cd')]), send)
        assert _messages(events) == [b'ab', b'ba', b'cd', b'dc']

    @pytest.mark.asyncio
    async def test_empty_request_stream_is_headers_then_trailers(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Chat')
        async def chat(request_iter, context):
            async for msg in request_iter:
                yield b'x'  # pragma: no cover — no request messages arrive

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Chat'), _receive_chunks([b'']), send)
        assert [e['type'] for e in events] == \
            ['http.response.start', 'http.response.trailers']
        assert _trailers(events)[b'grpc-status'] == b'0'


class TestBidiErrors:
    @pytest.mark.asyncio
    async def test_mid_stream_error_after_message_is_in_trailers(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Boom')
        async def boom(request_iter, context):
            async for msg in request_iter:
                yield b'ok:' + msg
                raise GrpcError(GrpcStatus.PERMISSION_DENIED, 'stop')

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Boom'),
                         _receive_chunks([_framed(b'a', b'b')]), send)
        assert _messages(events) == [b'ok:a']
        assert _trailers(events)[b'grpc-status'] == \
            str(int(GrpcStatus.PERMISSION_DENIED)).encode()

    @pytest.mark.asyncio
    async def test_malformed_request_frame_before_any_yield_is_trailers_only(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Chat')
        async def chat(request_iter, context):
            async for msg in request_iter:
                yield b'echo:' + msg

        # Truncated LPM prefix — the de-framer raises before the first yield.
        bad = b'\x00\x00\x00\x00\x05z'
        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Chat'), _receive_chunks([bad]), send)
        assert [e['type'] for e in events] == \
            ['http.response.start', 'http.response.trailers']
        assert _trailers(events)[b'grpc-status'] == \
            str(int(GrpcStatus.INTERNAL)).encode()

    @pytest.mark.asyncio
    async def test_deadline_expires_mid_stream(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Slow')
        async def slow(request_iter, context):
            async for msg in request_iter:
                yield b'first:' + msg
                await asyncio.sleep(0.2)

        scope = _grpc_scope('/svc/Slow', headers=[
            (b'content-type', b'application/grpc'), (b'grpc-timeout', b'1m')])
        events, send = _collector()
        await serve_grpc(reg, scope, _receive_chunks([_framed(b'a')]), send)
        # One message got out, then the deadline fired → status in trailers.
        assert _messages(events) == [b'first:a']
        assert _trailers(events)[b'grpc-status'] == \
            str(int(GrpcStatus.DEADLINE_EXCEEDED)).encode()


class TestBidiCancellation:
    @pytest.mark.asyncio
    async def test_generator_finalized_on_cancel(self):
        reg = GrpcServiceRegistry()
        closed = {'v': False}

        @reg.method('/svc/Forever')
        async def forever(request_iter, context):
            try:
                i = 0
                while True:
                    yield f'm{i}'.encode()
                    i += 1
                    await asyncio.sleep(0.01)
            finally:
                closed['v'] = True

        events, send = _collector()
        task = asyncio.create_task(
            serve_grpc(reg, _grpc_scope('/svc/Forever'),
                       _receive_chunks([_framed(b'go')]), send))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed['v'] is True, 'handler generator not finalised on cancel'

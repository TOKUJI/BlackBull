"""Conformance: gRPC message compression (Sprint 60 G2).

gRPC carries a per-message Compressed-Flag in the 5-byte LPM prefix; a set flag
means the body is compressed with the algorithm named in ``grpc-encoding``.
BlackBull implements ``gzip`` (stdlib zlib), advertises ``identity,gzip`` in
``grpc-accept-encoding``, decompresses compressed requests (with a
decompression-bomb guard), and gzip-compresses responses over a size threshold
when the client's ``grpc-accept-encoding`` lists gzip.

These drive the codec directly and ``serve_grpc`` with the in-process collector
harness (no socket).  The real-client (grpcio, ``grpc.Compression.Gzip``) wire
test lives in ``test_grpc_real_client_h2c.py``.
"""
from __future__ import annotations

import struct

import pytest

from blackbull.grpc import (
    GrpcServiceRegistry, GrpcStatus, encode_message, decode_messages,
)
from blackbull.grpc import compression
from blackbull.grpc.asgi import serve_grpc
import blackbull.grpc.asgi as grpc_asgi


# --------------------------------------------------------------------------
# Harness (mirrors test_grpc_client_streaming.py)
# --------------------------------------------------------------------------

def _grpc_scope(path, headers=None):
    base = [(b'content-type', b'application/grpc'), (b':method', b'POST')]
    return {'type': 'http', 'path': path,
            'headers': headers if headers is not None else base}


def _receive_chunks(chunks):
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


def _start_headers(events) -> dict:
    for e in events:
        if e['type'] == 'http.response.start':
            return dict(e['headers'])
    return {}


def _frames(events):
    """Return the raw ``(compressed_flag, payload)`` frames in the response,
    without decompressing — so a test can assert the flag itself."""
    out = []
    for e in events:
        if e['type'] == 'http.response.body' and e.get('body'):
            for compressed, payload in decode_messages(e['body']):
                out.append((compressed, payload))
    return out


def _decoded(events, encoding=b'gzip'):
    """Return response message bodies, gunzipping any compressed frame."""
    out = []
    for compressed, payload in _frames(events):
        if compressed:
            payload = compression.decompress_gzip(payload, 64 * 1024 * 1024)
        out.append(payload)
    return out


def _trailers(events) -> dict:
    return dict(events[-1]['headers'])


# --------------------------------------------------------------------------
# compression module unit tests
# --------------------------------------------------------------------------

class TestCompressionModule:
    def test_gzip_roundtrip(self):
        payload = b'hello world ' * 100
        packed = compression.compress_gzip(payload)
        assert packed != payload
        assert compression.decompress_gzip(packed, len(payload)) == payload

    def test_empty_roundtrip(self):
        packed = compression.compress_gzip(b'')
        assert compression.decompress_gzip(packed, 1024) == b''

    def test_gunzip_interops_with_stdlib_gzip(self):
        import gzip
        payload = b'the quick brown fox' * 50
        # Our gzip stream is decodable by the stdlib gzip module (real format).
        assert gzip.decompress(compression.compress_gzip(payload)) == payload
        # And we can decode what stdlib gzip produced.
        assert compression.decompress_gzip(
            gzip.compress(payload), len(payload)) == payload

    def test_decompression_bomb_is_rejected(self):
        # 1 MiB of zeros compresses to a tiny frame; refuse to inflate past cap.
        packed = compression.compress_gzip(b'\x00' * (1024 * 1024))
        assert len(packed) < 4096
        with pytest.raises(compression.DecompressionBombError):
            compression.decompress_gzip(packed, max_output=4096)

    def test_exactly_at_limit_is_allowed(self):
        payload = b'a' * 1000
        packed = compression.compress_gzip(payload)
        assert compression.decompress_gzip(packed, max_output=1000) == payload

    def test_corrupt_stream_raises_decompression_error(self):
        packed = bytearray(compression.compress_gzip(b'payload' * 20))
        packed[-3] ^= 0xFF  # corrupt the CRC/length trailer
        with pytest.raises(compression.DecompressionError):
            compression.decompress_gzip(bytes(packed), 1024 * 1024)

    def test_supports(self):
        assert compression.supports(b'gzip') is True
        assert compression.supports(b'identity') is False
        assert compression.supports(b'deflate') is False


# --------------------------------------------------------------------------
# Request decompression
# --------------------------------------------------------------------------

def _compressed_frame(payload: bytes) -> bytes:
    """Frame *payload* as a gzip-compressed LPM (Compressed-Flag = 1)."""
    return encode_message(compression.compress_gzip(payload), compressed=True)


class TestRequestDecompression:
    @pytest.mark.asyncio
    async def test_unary_gzip_request_is_decompressed(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Echo')
        async def echo(request, context) -> bytes:
            return request  # handler sees the *decompressed* bytes

        payload = b'compress me ' * 200
        scope = _grpc_scope('/svc/Echo', headers=[
            (b'content-type', b'application/grpc'), (b'grpc-encoding', b'gzip')])
        events, send = _collector()
        await serve_grpc(reg, scope, _receive_chunks([_compressed_frame(payload)]), send)

        assert _trailers(events)[b'grpc-status'] == b'0'
        assert _decoded(events) == [payload]

    @pytest.mark.asyncio
    async def test_client_streaming_gzip_requests_decompressed(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Join')
        async def join(request_iter, context) -> bytes:
            return b'|'.join([m async for m in request_iter])

        body = _compressed_frame(b'aaa' * 100) + _compressed_frame(b'bbb' * 100)
        scope = _grpc_scope('/svc/Join', headers=[
            (b'content-type', b'application/grpc'), (b'grpc-encoding', b'gzip')])
        events, send = _collector()
        await serve_grpc(reg, scope, _receive_chunks([body]), send)

        assert _trailers(events)[b'grpc-status'] == b'0'
        assert _decoded(events) == [b'aaa' * 100 + b'|' + b'bbb' * 100]

    @pytest.mark.asyncio
    async def test_mixed_compressed_and_plain_in_one_stream(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Join')
        async def join(request_iter, context) -> bytes:
            return b'|'.join([m async for m in request_iter])

        # A gzip-negotiated request may still send a message uncompressed
        # (Flag = 0) — the flag, not the header, decides each message.
        body = _compressed_frame(b'zzz' * 100) + encode_message(b'plain')
        scope = _grpc_scope('/svc/Join', headers=[
            (b'content-type', b'application/grpc'), (b'grpc-encoding', b'gzip')])
        events, send = _collector()
        await serve_grpc(reg, scope, _receive_chunks([body]), send)

        assert _decoded(events) == [b'zzz' * 100 + b'|plain']

    @pytest.mark.asyncio
    async def test_unsupported_encoding_is_unimplemented(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Echo')
        async def echo(request, context) -> bytes:
            return request

        # Compressed flag set but grpc-encoding names an unsupported codec.
        frame = struct.pack('>BI', 1, 3) + b'xyz'
        scope = _grpc_scope('/svc/Echo', headers=[
            (b'content-type', b'application/grpc'), (b'grpc-encoding', b'snappy')])
        events, send = _collector()
        await serve_grpc(reg, scope, _receive_chunks([frame]), send)

        assert _trailers(events)[b'grpc-status'] == \
            str(int(GrpcStatus.UNIMPLEMENTED)).encode()
        # Server advertises what it accepts so the client can retry.
        assert _start_headers(events)[b'grpc-accept-encoding'] == b'identity,gzip'

    @pytest.mark.asyncio
    async def test_compressed_flag_without_encoding_header_is_unimplemented(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Echo')
        async def echo(request, context) -> bytes:
            return request

        frame = struct.pack('>BI', 1, 3) + b'xyz'  # Flag set, no grpc-encoding
        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Echo'),
                         _receive_chunks([frame]), send)
        assert _trailers(events)[b'grpc-status'] == \
            str(int(GrpcStatus.UNIMPLEMENTED)).encode()

    @pytest.mark.asyncio
    async def test_request_decompression_bomb_is_resource_exhausted(self, monkeypatch):
        monkeypatch.setattr(grpc_asgi, 'MAX_MESSAGE_SIZE', 4096)
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Echo')
        async def echo(request, context) -> bytes:
            return request

        frame = _compressed_frame(b'\x00' * (1024 * 1024))  # inflates past 4 KiB
        scope = _grpc_scope('/svc/Echo', headers=[
            (b'content-type', b'application/grpc'), (b'grpc-encoding', b'gzip')])
        events, send = _collector()
        await serve_grpc(reg, scope, _receive_chunks([frame]), send)
        assert _trailers(events)[b'grpc-status'] == \
            str(int(GrpcStatus.RESOURCE_EXHAUSTED)).encode()

    @pytest.mark.asyncio
    async def test_corrupt_gzip_request_is_internal(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Echo')
        async def echo(request, context) -> bytes:
            return request

        packed = bytearray(compression.compress_gzip(b'data' * 100))
        packed[-2] ^= 0xFF  # corrupt trailer
        frame = encode_message(bytes(packed), compressed=True)
        scope = _grpc_scope('/svc/Echo', headers=[
            (b'content-type', b'application/grpc'), (b'grpc-encoding', b'gzip')])
        events, send = _collector()
        await serve_grpc(reg, scope, _receive_chunks([frame]), send)
        assert _trailers(events)[b'grpc-status'] == \
            str(int(GrpcStatus.INTERNAL)).encode()


# --------------------------------------------------------------------------
# Response compression
# --------------------------------------------------------------------------

class TestResponseCompression:
    @pytest.mark.asyncio
    async def test_large_response_compressed_when_client_accepts_gzip(self, monkeypatch):
        monkeypatch.setattr(grpc_asgi, '_COMPRESS_MIN_BYTES', 128)
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Big')
        async def big(request, context) -> bytes:
            return b'x' * 4096  # highly compressible, over threshold

        scope = _grpc_scope('/svc/Big', headers=[
            (b'content-type', b'application/grpc'),
            (b'grpc-accept-encoding', b'identity,gzip')])
        events, send = _collector()
        await serve_grpc(reg, scope, _receive_chunks([encode_message(b'go')]), send)

        assert _start_headers(events)[b'grpc-encoding'] == b'gzip'
        (compressed, _payload), = _frames(events)
        assert compressed is True
        assert _decoded(events) == [b'x' * 4096]

    @pytest.mark.asyncio
    async def test_response_not_compressed_when_client_rejects(self, monkeypatch):
        monkeypatch.setattr(grpc_asgi, '_COMPRESS_MIN_BYTES', 128)
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Big')
        async def big(request, context) -> bytes:
            return b'x' * 4096

        # No grpc-accept-encoding → client can't decode gzip → send plain.
        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Big'),
                         _receive_chunks([encode_message(b'go')]), send)

        assert b'grpc-encoding' not in _start_headers(events)
        (compressed, payload), = _frames(events)
        assert compressed is False
        assert payload == b'x' * 4096

    @pytest.mark.asyncio
    async def test_small_response_below_threshold_not_compressed(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Small')
        async def small(request, context) -> bytes:
            return b'tiny'  # below the 1 KiB default threshold

        scope = _grpc_scope('/svc/Small', headers=[
            (b'content-type', b'application/grpc'),
            (b'grpc-accept-encoding', b'gzip')])
        events, send = _collector()
        await serve_grpc(reg, scope, _receive_chunks([encode_message(b'go')]), send)

        # grpc-encoding is still advertised (gzip negotiated) but the message
        # rides uncompressed because it is under the threshold.
        assert _start_headers(events)[b'grpc-encoding'] == b'gzip'
        (compressed, payload), = _frames(events)
        assert compressed is False
        assert payload == b'tiny'

    @pytest.mark.asyncio
    async def test_incompressible_response_sent_uncompressed(self, monkeypatch):
        monkeypatch.setattr(grpc_asgi, '_COMPRESS_MIN_BYTES', 16)
        reg = GrpcServiceRegistry()

        import os
        payload = os.urandom(2048)  # random ≈ incompressible

        @reg.method('/svc/Rand')
        async def rand(request, context) -> bytes:
            return payload

        scope = _grpc_scope('/svc/Rand', headers=[
            (b'content-type', b'application/grpc'),
            (b'grpc-accept-encoding', b'gzip')])
        events, send = _collector()
        await serve_grpc(reg, scope, _receive_chunks([encode_message(b'go')]), send)

        # gzip would grow it, so the framer falls back to Flag = 0.
        (compressed, body), = _frames(events)
        assert compressed is False
        assert body == payload

    @pytest.mark.asyncio
    async def test_server_streaming_responses_compressed(self, monkeypatch):
        monkeypatch.setattr(grpc_asgi, '_COMPRESS_MIN_BYTES', 128)
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Stream')
        async def stream(request, context):
            for _ in range(3):
                yield b'y' * 2048

        scope = _grpc_scope('/svc/Stream', headers=[
            (b'content-type', b'application/grpc'),
            (b'grpc-accept-encoding', b'gzip')])
        events, send = _collector()
        await serve_grpc(reg, scope, _receive_chunks([encode_message(b'go')]), send)

        assert _start_headers(events)[b'grpc-encoding'] == b'gzip'
        frames = _frames(events)
        assert len(frames) == 3
        assert all(compressed for compressed, _ in frames)
        assert _decoded(events) == [b'y' * 2048] * 3

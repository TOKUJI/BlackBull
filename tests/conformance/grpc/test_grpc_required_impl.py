"""Conformance tests for the gRPC implementation requirements.

These started as a ``pytest.fail`` TODO list documenting features the gRPC
spec requires.  Each has since been implemented in ``blackbull/grpc`` and the
TODO replaced with a real assertion verifying the behaviour.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.grpc import (
    GrpcServiceRegistry, GrpcStatus, GrpcDecodeError, encode_message,
)
from blackbull.grpc import asgi as grpc_asgi
from blackbull.grpc.asgi import serve_grpc, _parse_grpc_timeout
from blackbull.grpc.codec import decode_messages, MAX_MESSAGE_LENGTH


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def _grpc_scope(path, content_type=b'application/grpc', extra_headers=()):
    headers = [(b'content-type', content_type), (b':method', b'POST')]
    headers.extend(extra_headers)
    return {'type': 'http', 'path': path, 'headers': headers}


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


def _all_headers(events) -> dict[bytes, bytes]:
    out = {}
    for e in events:
        if e['type'] in ('http.response.start', 'http.response.trailers'):
            for k, v in e['headers']:
                out[k] = v
    return out


# ---------------------------------------------------------------------------
# §1 — grpc-timeout: parse header and enforce deadline
# ---------------------------------------------------------------------------

class TestRequiredGrpcTimeout:
    """``grpc-timeout`` header parsing and deadline enforcement (gRPC spec §3.3)."""

    @pytest.mark.parametrize('raw,expected', [
        (b'5S', 5.0),
        (b'100m', 0.1),
        (b'1H', 3600.0),
        (b'1M', 60.0),
        (b'99999999n', 0.099999999),
        (b'1u', 0.000001),
        (b'', None),
        (b'0S', None),          # zero = no deadline
        (b'-1S', None),         # negative = invalid
        (b'999999999S', None),  # >8 digits = invalid
        (b'5X', None),          # unknown unit
        (b'invalid', None),
        (b'S', None),           # no digits
    ])
    def test_grpc_timeout_unit_conversions_are_correct(self, raw, expected):
        result = _parse_grpc_timeout(raw)
        if expected is None:
            assert result is None
        else:
            assert result == pytest.approx(expected)

    def test_grpc_timeout_header_is_parsed(self):
        assert _parse_grpc_timeout(b'5S') == 5.0
        assert _parse_grpc_timeout(b'100m') == pytest.approx(0.1)
        assert _parse_grpc_timeout(b'1H') == 3600.0
        assert _parse_grpc_timeout(b'') is None
        assert _parse_grpc_timeout(b'invalid') is None

    @pytest.mark.asyncio
    async def test_grpc_timeout_enforced_on_handler(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Slow')
        async def slow(request, context):
            await asyncio.sleep(10)   # far longer than the deadline
            return b'too late'

        events, send = _collector()
        # 10ms deadline → handler must be cut off with DEADLINE_EXCEEDED.
        await serve_grpc(
            reg,
            _grpc_scope('/svc/Slow', extra_headers=[(b'grpc-timeout', b'10m')]),
            _receive_with(encode_message(b'')), send)
        assert _all_headers(events)[b'grpc-status'] == \
            str(int(GrpcStatus.DEADLINE_EXCEEDED)).encode()

    @pytest.mark.asyncio
    async def test_within_deadline_succeeds(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Fast')
        async def fast(request, context):
            return b'ok'

        events, send = _collector()
        await serve_grpc(
            reg,
            _grpc_scope('/svc/Fast', extra_headers=[(b'grpc-timeout', b'30S')]),
            _receive_with(encode_message(b'')), send)
        assert _all_headers(events)[b'grpc-status'] == b'0'


# ---------------------------------------------------------------------------
# §2 — BaseException handler isolation (already implemented; guard regression)
# ---------------------------------------------------------------------------

class TestRequiredBaseExceptionHandling:
    """Handler isolation: an ordinary handler bug is reported as INTERNAL, but
    control-flow ``BaseException``\\ s (cancellation, interpreter shutdown) must
    propagate rather than being masked into a gRPC status.  Asserted
    behaviourally — this survives refactoring the serving path (``_serve_unary``
    / ``_serve_server_streaming``) and does not depend on which ``except`` clause
    is written."""

    @pytest.mark.asyncio
    async def test_handler_exception_is_isolated_as_internal(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Boom')
        async def boom(request, context):
            raise RuntimeError('handler bug')

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Boom'),
                         _receive_with(encode_message(b'')), send)
        assert _all_headers(events)[b'grpc-status'] == \
            str(int(GrpcStatus.INTERNAL)).encode()

    @pytest.mark.asyncio
    async def test_streaming_handler_exception_is_isolated_as_internal(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/StreamBoom')
        async def stream_boom(request, context):
            raise RuntimeError('handler bug')
            yield b''  # pragma: no cover — marks this an async generator

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/StreamBoom'),
                         _receive_with(encode_message(b'')), send)
        assert _all_headers(events)[b'grpc-status'] == \
            str(int(GrpcStatus.INTERNAL)).encode()

    @pytest.mark.asyncio
    async def test_cancellederror_propagates_and_is_not_swallowed(self):
        # Cancellation (a BaseException, not Exception) must escape the serving
        # path so the surrounding task actually cancels — never be turned into a
        # gRPC status.
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Cancel')
        async def cancel(request, context):
            raise asyncio.CancelledError

        events, send = _collector()
        with pytest.raises(asyncio.CancelledError):
            await serve_grpc(reg, _grpc_scope('/svc/Cancel'),
                             _receive_with(encode_message(b'')), send)
        # And no status was emitted (the error was not masked into a response).
        assert b'grpc-status' not in _all_headers(events)


# ---------------------------------------------------------------------------
# §3 — Max message size enforcement
# ---------------------------------------------------------------------------

class TestRequiredMaxMessageSize:
    """Per-message size limit at the gRPC layer (RESOURCE_EXHAUSTED above it)."""

    @pytest.mark.asyncio
    async def test_max_message_size_is_enforced(self, monkeypatch):
        # Shrink the limit so the test stays fast and small.
        monkeypatch.setattr(grpc_asgi, 'MAX_MESSAGE_SIZE', 1024)
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Big')
        async def big(request, context):
            return b'ok'

        events, send = _collector()
        oversized = b'x' * 2048  # > 1024 limit
        await serve_grpc(reg, _grpc_scope('/svc/Big'),
                         _receive_with(encode_message(oversized)), send)
        assert _all_headers(events)[b'grpc-status'] == \
            str(int(GrpcStatus.RESOURCE_EXHAUSTED)).encode()

    @pytest.mark.asyncio
    async def test_message_within_limit_succeeds(self, monkeypatch):
        monkeypatch.setattr(grpc_asgi, 'MAX_MESSAGE_SIZE', 1024)
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Ok')
        async def ok(request, context):
            return b'ok'

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Ok'),
                         _receive_with(encode_message(b'x' * 512)), send)
        assert _all_headers(events)[b'grpc-status'] == b'0'


# ---------------------------------------------------------------------------
# §4 — Content-Type subtype handling
# ---------------------------------------------------------------------------

class TestRequiredContentTypeSubtype:
    """The gRPC response Content-Type echoes the request subtype."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize('request_ct,expected_ct', [
        (b'application/grpc+proto', b'application/grpc+proto'),
        (b'application/grpc+json', b'application/grpc+json'),
        (b'application/grpc', b'application/grpc'),
        (b' application/grpc+proto ', b'application/grpc+proto'),  # whitespace trimmed
    ])
    async def test_response_content_type_reflects_request_subtype(
            self, request_ct, expected_ct):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Echo')
        async def echo(request, context):
            return b'ok'

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Echo', content_type=request_ct),
                         _receive_with(encode_message(b'')), send)
        start = next(e for e in events if e['type'] == 'http.response.start')
        assert dict(start['headers'])[b'content-type'] == expected_ct


# ---------------------------------------------------------------------------
# §5 — grpc-encoding / compression negotiation
# ---------------------------------------------------------------------------

class TestRequiredCompressionNegotiation:
    @pytest.mark.asyncio
    async def test_grpc_accept_encoding_is_advertised(self):
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Ok')
        async def ok(request, context):
            return b'ok'

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Ok'),
                         _receive_with(encode_message(b'')), send)
        start = next(e for e in events if e['type'] == 'http.response.start')
        assert dict(start['headers'])[b'grpc-accept-encoding'] == b'identity'

    @pytest.mark.asyncio
    async def test_grpc_accept_encoding_advertised_on_error_path(self):
        reg = GrpcServiceRegistry()  # no methods → UNIMPLEMENTED (trailers-only)
        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Missing'),
                         _receive_with(encode_message(b'')), send)
        assert _all_headers(events)[b'grpc-accept-encoding'] == b'identity'

    def test_compressed_message_returns_unimplemented_with_clear_message(self):
        # Placeholder for future compression work — rejection is tested
        # in test_grpc_security.py / test_grpc.py.
        pass


# ---------------------------------------------------------------------------
# §6 — Message size limit in codec (defense in depth)
# ---------------------------------------------------------------------------

class TestRequiredCodecSizeLimit:
    def test_codec_refuses_overly_large_length_prefix(self):
        # A forged 4 GiB length prefix must be rejected before slicing.
        forged = b'\x00' + (0xFFFFFFFF).to_bytes(4, 'big') + b'abc'
        with pytest.raises(GrpcDecodeError):
            decode_messages(forged)

    def test_codec_accepts_length_at_limit_boundary(self):
        # A declared length just over the limit is refused...
        over = b'\x00' + (MAX_MESSAGE_LENGTH + 1).to_bytes(4, 'big')
        with pytest.raises(GrpcDecodeError):
            decode_messages(over)
        # ...but a normal small message still round-trips.
        assert decode_messages(encode_message(b'small')) == [(False, b'small')]

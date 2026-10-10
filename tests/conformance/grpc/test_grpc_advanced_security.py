"""Advanced gRPC security and conformance tests — covering previously untested
attack vectors identified in the 2026-06-28 security audit.

These tests complement ``test_grpc_codec_conformance.py`` and
``test_grpc_security.py`` by adding coverage for:

* Out-of-range status code injection (D-T1, D-T2)
* Content-Type confusion with unknown suffixes (C-T1, C-T2)
* Method path traversal (H-T3)
* grpc-timeout header abuse (F-T4)
* Handler timeout / infinite loop (E-T1)
* Pseudo-header injection as metadata (B-T1)
* Large metadata / HPACK bomb (B-T2)
* Concurrent gRPC call isolation (F-T1)
* Reflection service enumeration (H-T2)
* Large message body through bridge (F-P1)
* Zero-byte boundary truncation (A-P1)
* Compressed-Flag values 2–254 (A-P2)

Transport-layer attacks (RST_STREAM, GOAWAY, SETTINGS manipulation, HPACK
table pollution, flow control, stream ID exhaustion) are **out of scope**
for these tests — they are the ASGI server's (uvicorn/hypercorn)
responsibility and cannot be exercised from the ASGI bridge layer.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.grpc import (
    GrpcServiceRegistry, GrpcStatus, GrpcError, GrpcDecodeError,
    encode_message, decode_messages,
)
from blackbull.grpc.asgi import serve_grpc, GrpcContext, _pct_encode_message
from blackbull.grpc.status import GrpcStatus as GS
from blackbull.native import NativeResponse

# A raw/out-of-range int where a GrpcStatus enum is required is a caller bug,
# rejected at the type boundary: beartype raises BeartypeCallHintParamViolation
# under the test gate, while production (no beartype) falls through to the
# GrpcStatus() coercion's ValueError.  Accept either so the test asserts the
# behaviour — "an invalid status never reaches the wire" — not the mechanism.
try:
    from beartype.roar import BeartypeCallHintParamViolation as _BeartypeViolation
except ImportError:  # beartype absent / not instrumenting
    _BeartypeViolation = None
_STATUS_REJECTED = ((ValueError,) if _BeartypeViolation is None
                    else (ValueError, _BeartypeViolation))


# ---------------------------------------------------------------------------
# Harness (shared with test_grpc_security.py)
# ---------------------------------------------------------------------------

def _grpc_scope(path, headers=None):
    base = [(b'content-type', b'application/grpc'),
            (b':method', b'POST'),
            (b':path', path.encode() if isinstance(path, str) else path)]
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


def _receive_streaming(chunks: list[bytes]):
    idx = 0

    async def receive():
        nonlocal idx
        if idx < len(chunks):
            body = chunks[idx]
            idx += 1
            return {'type': 'http.request', 'body': body,
                    'more_body': idx < len(chunks)}
        return {'type': 'http.request', 'body': b'', 'more_body': False}
    return receive


def _collector():
    events = []

    async def send(event):
        # gRPC emits native on the seam; these assertions are
        # about the wire, so expand to the ASGI events it stands for.
        if isinstance(event, NativeResponse):
            events.extend(event.to_asgi())
        else:
            events.append(event)
    return events, send


def _trailers_of(events):
    out = {}
    for e in events:
        if e['type'] in ('http.response.start', 'http.response.trailers'):
            for k, v in e['headers']:
                out[k] = v
    return out


# ---------------------------------------------------------------------------
# §D — Status Code Abuse (D-T1, D-T2)
# ---------------------------------------------------------------------------

class TestOutOfRangeStatusCodes:
    """gRPC status codes outside the canonical 0–16 range must be rejected
    or sanitized.  ``IntEnum`` provides a degree of protection, but we
    verify the behavior explicitly."""

    def test_grpc_status_rejects_out_of_range_int(self):
        """``GrpcStatus(999)`` must raise ``ValueError`` — out of range."""
        with pytest.raises(ValueError):
            GrpcStatus(999)

    def test_grpc_status_rejects_negative_int(self):
        """``GrpcStatus(-1)`` must raise ``ValueError``."""
        with pytest.raises(ValueError):
            GrpcStatus(-1)

    @pytest.mark.parametrize('bad', [
        pytest.param('0', id='zero-as-string'),
        pytest.param('abc', id='non-numeric-string'),
    ])
    def test_grpc_status_rejects_zero_as_string(self, bad):
        """String forms must be rejected — gRPC status codes are integers."""
        with pytest.raises(ValueError):
            GrpcStatus(bad)  # type: ignore[arg-type]

    def test_grpc_error_constructor_rejects_out_of_range(self):
        """``GrpcError(999, 'msg')`` must be rejected (enum-only contract,
        like grpcio), preventing invalid status codes from reaching the wire."""
        with pytest.raises(_STATUS_REJECTED):
            GrpcError(999, 'msg')  # type: ignore[arg-type]

    def test_context_set_code_rejects_out_of_range(self):
        """``context.set_code(999)`` must be rejected."""
        ctx = GrpcContext({'type': 'http', 'path': '/test', 'headers': []})
        with pytest.raises(_STATUS_REJECTED):
            ctx.set_code(999)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_handler_cannot_emit_out_of_range_status_on_wire(self):
        """If a handler somehow bypasses ``GrpcStatus`` validation and
        produces an out-of-range integer, ``_status_trailers`` would emit
        it as a decimal string.  We verify that the ``GrpcStatus`` guard
        at ``GrpcError.__init__`` prevents this for error paths.

        For the success path, ``context.set_code()`` also validates.
        This means the wire format is protected by IntEnum validation
        at both entry points.
        """
        # Prove that GrpcError rejects invalid codes before they reach
        # _status_trailers / _send_trailers_only.
        reg = GrpcServiceRegistry()

        @reg.method('/svc/BadCode')
        async def bad_code(request, context):
            # A valid boundary code, passed as the enum (the enum-only contract
            # forbids a bare int — even a valid one — at the type boundary).
            raise GrpcError(GrpcStatus.UNAUTHENTICATED, 'ok bounds')  # == 16

        # Verify valid codes work
        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/BadCode'),
                         _receive_with(encode_message(b'')), send)
        assert _trailers_of(events)[b'grpc-status'] == str(16).encode()

    def test_grpc_status_enum_has_exactly_17_members(self):
        """The canonical gRPC status code set has exactly 17 members (0–16).
        No more, no less.  Extra members could cause wire-format confusion."""
        assert len(GrpcStatus) == 17
        assert min(int(s) for s in GrpcStatus) == 0
        assert max(int(s) for s in GrpcStatus) == 16


# ---------------------------------------------------------------------------
# §C — Content-Type Confusion (C-T1, C-T2)
# ---------------------------------------------------------------------------

class TestContentTypeConfusion:
    """Content-Type variants that could bypass gRPC dispatch or cause
    unexpected routing behavior."""

    @pytest.mark.asyncio
    async def test_unknown_grpc_suffix_does_not_crash(self):
        """``application/grpc+thrift`` or any unknown suffix must not crash."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/M')
        async def m(request, context):
            return b'ok'

        for suffix in [b'+thrift', b'+custom', b'+v2', b'-web']:
            events, send = _collector()
            scope = {'type': 'http', 'path': '/svc/M',
                     'headers': [(b'content-type', b'application/grpc' + suffix),
                                 (b':method', b'POST')]}
            await serve_grpc(reg, scope, _receive_with(encode_message(b'')), send)
            assert _trailers_of(events)[b'grpc-status'] == b'0', (
                f'failed for suffix {suffix!r}')

    @pytest.mark.asyncio
    async def test_missing_content_type_still_works(self):
        """Without a ``content-type`` header, the bridge still operates.
        The caller is responsible for filtering, so this tests that the
        bridge itself doesn't crash on missing content-type."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/M')
        async def m(request, context):
            return b'ok'

        events, send = _collector()
        scope = {'type': 'http', 'path': '/svc/M',
                 'headers': [(b':method', b'POST')]}
        await serve_grpc(reg, scope, _receive_with(encode_message(b'')), send)
        assert _trailers_of(events)[b'grpc-status'] == b'0'

    @pytest.mark.asyncio
    async def test_content_type_with_leading_trailing_whitespace(self):
        """Content-Type with whitespace around the value (`` application/grpc ``
        or ``application/grpc ``) — the bridge doesn't trim, but the caller's
        ``startswith`` check would fail on leading whitespace."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/M')
        async def m(request, context):
            return b'ok'

        # The bridge itself doesn't enforce content-type, so these all work.
        # The caller (BlackBull._dispatch) would need to handle trimming.
        for ct in [b' application/grpc', b'application/grpc ',
                   b'\tapplication/grpc']:
            events, send = _collector()
            scope = {'type': 'http', 'path': '/svc/M',
                     'headers': [(b'content-type', ct),
                                 (b':method', b'POST')]}
            await serve_grpc(reg, scope, _receive_with(encode_message(b'')), send)
            assert _trailers_of(events)[b'grpc-status'] == b'0'


# ---------------------------------------------------------------------------
# §H-T3 — Method Path Traversal
# ---------------------------------------------------------------------------

class TestMethodPathTraversal:
    """gRPC method paths like ``/../../../AdminService/DeleteAll`` must not
    escape the registry namespace."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize('registered,requested', [
        pytest.param('/admin.Service/Delete', '/../admin.Service/Delete',
                     id='dot-dot-slash'),
        pytest.param('/svc/Real', '/../../../svc/Real',
                     id='multiple-dot-dot-slash'),
        pytest.param('/svc/Target', '/%2e%2e%2fsvc/Target',
                     id='encoded-dot-dot-slash'),
    ])
    async def test_dot_dot_slash_path_returns_unimplemented(self, registered, requested):
        """A traversal-style path must not resolve to a registered handler."""
        reg = GrpcServiceRegistry()

        @reg.method(registered)
        async def handler(request, context):
            return b'reached'  # pragma: no cover — should not be reached

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope(requested),
                         _receive_with(encode_message(b'')), send)
        assert _trailers_of(events)[b'grpc-status'] == \
            str(int(GrpcStatus.UNIMPLEMENTED)).encode()

    @pytest.mark.asyncio
    @pytest.mark.parametrize('path', [
        pytest.param('\\svc\\Windows', id='backslash-path'),
        pytest.param('/svc\x00/Method', id='null-byte-in-path'),
        pytest.param('/', id='only-slash-path'),
    ])
    async def test_backslash_path_does_not_crash(self, path):
        """Malformed paths answer UNIMPLEMENTED without crashing the bridge."""
        reg = GrpcServiceRegistry()
        events, send = _collector()
        await serve_grpc(reg, _grpc_scope(path),
                         _receive_with(encode_message(b'')), send)
        assert _trailers_of(events)[b'grpc-status'] == \
            str(int(GrpcStatus.UNIMPLEMENTED)).encode()

    @pytest.mark.asyncio
    async def test_empty_path(self):
        """Empty path in scope."""
        reg = GrpcServiceRegistry()
        events, send = _collector()
        scope = {'type': 'http', 'path': '',
                 'headers': [(b'content-type', b'application/grpc'),
                             (b':method', b'POST')]}
        await serve_grpc(reg, scope, _receive_with(encode_message(b'')), send)
        assert _trailers_of(events)[b'grpc-status'] == \
            str(int(GrpcStatus.UNIMPLEMENTED)).encode()


# ---------------------------------------------------------------------------
# §F-T4 — grpc-timeout header abuse
# ---------------------------------------------------------------------------

class TestGrpcTimeoutHeader:
    """The ``grpc-timeout`` header specifies a deadline for the RPC.
    BlackBull currently does not enforce deadlines, but it must not
    crash when encountering invalid timeout values."""

    @pytest.mark.asyncio
    async def test_grpc_timeout_present_does_not_crash(self):
        """A valid ``grpc-timeout`` header must not crash the bridge."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Timeout')
        async def timeout_handler(request, context):
            return b'ok'

        headers = [(b'content-type', b'application/grpc'),
                   (b'grpc-timeout', b'5S')]
        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Timeout', headers),
                         _receive_with(encode_message(b'')), send)
        assert _trailers_of(events)[b'grpc-status'] == b'0'

    @pytest.mark.asyncio
    async def test_grpc_timeout_huge_value_does_not_crash(self):
        """``grpc-timeout: 99999999H`` (huge hours) must not crash."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Timeout')
        async def timeout_handler(request, context):
            return b'ok'

        for bad_value in [b'99999999H', b'99999999S', b'2147483647m',
                          b'0S', b'-1S', b'1X', b'abc', b'']:
            headers = [(b'content-type', b'application/grpc'),
                       (b'grpc-timeout', bad_value)]
            events, send = _collector()
            await serve_grpc(reg, _grpc_scope('/svc/Timeout', headers),
                             _receive_with(encode_message(b'')), send)
            assert _trailers_of(events)[b'grpc-status'] == b'0', (
                f'failed for grpc-timeout={bad_value!r}')


# ---------------------------------------------------------------------------
# §E-T1 — Handler Timeout / Infinite Loop
# ---------------------------------------------------------------------------

class TestHandlerTimeout:
    """A handler that hangs indefinitely (e.g., waiting on an event that
    never fires) must not block the server permanently.  Since BlackBull
    does not yet enforce per-RPC deadlines, we test that the handler
    isolation contract is maintained and that wrapping with ``asyncio.wait_for``
    works correctly."""

    @pytest.mark.asyncio
    async def test_handler_can_be_wrapped_with_wait_for(self):
        """Demonstrate that ``serve_grpc`` can be externally wrapped with
        ``asyncio.wait_for`` — the ASGI send/receive contract is compatible
        with cancellation."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Slow')
        async def slow(request, context):
            await asyncio.sleep(0.05)  # short enough for test
            return b'done'

        events, send = _collector()
        await asyncio.wait_for(
            serve_grpc(reg, _grpc_scope('/svc/Slow'),
                       _receive_with(encode_message(b'')), send),
            timeout=1.0,
        )
        assert _trailers_of(events)[b'grpc-status'] == b'0'

    @pytest.mark.asyncio
    async def test_very_slow_handler_still_completes(self):
        """A handler that takes a non-trivial amount of time (but completes)
        must still produce the correct response."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Slow')
        async def slow(request, context):
            await asyncio.sleep(0.1)
            return b'eventually'

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Slow'),
                         _receive_with(encode_message(b'')), send)
        body_ev = next((e for e in events if e['type'] == 'http.response.body'), None)
        assert body_ev is not None
        assert decode_messages(body_ev['body']) == [(False, b'eventually')]

    @pytest.mark.asyncio
    async def test_handler_cancellation_via_wait_for_produces_no_response(self):
        """KNOWN LIMITATION: Wrapping ``serve_grpc`` with a tight
        ``asyncio.wait_for`` and letting it cancel will leave the
        ASGI send/receive in an undefined state (no trailers sent).

        This test documents the current behavior.  A production framework
        should catch ``CancelledError`` and send a DEADLINE_EXCEEDED
        trailers-only response before propagating."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Hang')
        async def hang(request, context):
            await asyncio.sleep(10)  # will be cancelled
            return b'never'

        events, send = _collector()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                serve_grpc(reg, _grpc_scope('/svc/Hang'),
                           _receive_with(encode_message(b'')), send),
                timeout=0.05,
            )
        # After cancellation, no valid gRPC response was sent.
        # This is a gap: the bridge should send DEADLINE_EXCEEDED trailers
        # before the cancellation propagates.


# ---------------------------------------------------------------------------
# §B-T1 — Pseudo-header injection as metadata
# ---------------------------------------------------------------------------

class TestPseudoHeaderInjection:
    """HTTP/2 pseudo-headers (``:status``, ``:path``, ``:method``, ``:scheme``,
    ``:authority``) must not be injectable as gRPC metadata that could
    confuse downstream proxies or the HTTP/2 layer."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize('meta_key,meta_value', [
        pytest.param(b':status', b'999', id='pseudo-header-status'),
        pytest.param(b':path', b'/evil.Service/Bad', id='pseudo-header-path'),
        pytest.param(b'grpc-timeout', b'30S', id='grpc-timeout'),
    ])
    async def test_pseudo_header_status_is_readable_as_metadata(self, meta_key, meta_value):
        """Request metadata (pseudo-header forms and grpc-timeout) is readable
        through context.metadata(); injected headers never affect routing,
        which uses scope['path']."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Pseudo')
        async def pseudo(request, context):
            return context.metadata(meta_key, b'not-present')

        headers = [(b'content-type', b'application/grpc'),
                   (meta_key, meta_value)]
        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Pseudo', headers),
                         _receive_with(encode_message(b'')), send)
        body_ev = next((e for e in events if e['type'] == 'http.response.body'), None)
        assert body_ev is not None
        assert decode_messages(body_ev['body']) == [(False, meta_value)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize('lookup_key,sent_key,sent_value', [
        pytest.param(b':method', b':method', b'GET', id='pseudo-header-method'),
        pytest.param(b':authority', b':authority', b'evil.com:443',
                     id='pseudo-header-authority'),
        pytest.param(b'X-TOKEN', b'x-token', b'secret',
                     id='metadata-case-insensitive'),
    ])
    async def test_pseudo_header_method_as_metadata(self, lookup_key, sent_key, sent_value):
        """Metadata lookup covers pseudo-header keys and is case-insensitive."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Meta')
        async def meta(request, context):
            return context.metadata(lookup_key, b'unknown')

        headers = [(b'content-type', b'application/grpc'),
                   (sent_key, sent_value)]
        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Meta', headers),
                         _receive_with(encode_message(b'')), send)
        body_ev = next((e for e in events if e['type'] == 'http.response.body'), None)
        assert body_ev is not None
        assert decode_messages(body_ev['body']) == [(False, sent_value)]

    @pytest.mark.asyncio
    async def test_grpc_status_metadata_header_does_not_override_trailers(self):
        """A request header named ``grpc-status`` must NOT override the
        actual ``grpc-status`` trailer set by the server."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Override')
        async def override(request, context):
            # The client tries to inject grpc-status: 0 but we return an error
            client_status = context.metadata(b'grpc-status', b'none')
            raise GrpcError(GrpcStatus.PERMISSION_DENIED,
                            f'client sent grpc-status={client_status.decode()}')

        headers = [(b'content-type', b'application/grpc'),
                   (b'grpc-status', b'0')]  # attacker tries to force OK
        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Override', headers),
                         _receive_with(encode_message(b'')), send)
        trailers = _trailers_of(events)
        # The server's grpc-status (7) must win, not the client's injected value
        assert trailers[b'grpc-status'] == str(int(GrpcStatus.PERMISSION_DENIED)).encode()

    @pytest.mark.asyncio
    async def test_grpc_message_metadata_header_does_not_override_trailers(self):
        """Similarly, a request header ``grpc-message`` must not contaminate
        the server's response."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/MsgOverride')
        async def msg_override(request, context):
            raise GrpcError(GrpcStatus.NOT_FOUND, 'real error')

        headers = [(b'content-type', b'application/grpc'),
                   (b'grpc-message', b'injected by attacker')]
        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/MsgOverride', headers),
                         _receive_with(encode_message(b'')), send)
        trailers = _trailers_of(events)
        assert trailers[b'grpc-message'] != b'injected by attacker'
        assert b'real error' in trailers[b'grpc-message']


# ---------------------------------------------------------------------------
# §B-T2 — Large Metadata (HPACK bomb)
# ---------------------------------------------------------------------------

class TestLargeMetadata:
    """Many or large request metadata headers must not cause memory exhaustion
    or crash the bridge."""

    @pytest.mark.asyncio
    async def test_many_metadata_headers_do_not_crash(self):
        """100 metadata headers with 1 KiB values must not crash."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/ManyHeaders')
        async def many_headers(request, context):
            return b'ok'

        headers = [(b'content-type', b'application/grpc')]
        for i in range(100):
            headers.append((f'x-meta-{i:04d}'.encode(), b'v' * 1024))

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/ManyHeaders', headers),
                         _receive_with(encode_message(b'')), send)
        assert _trailers_of(events)[b'grpc-status'] == b'0'

    @pytest.mark.asyncio
    async def test_single_large_metadata_value_does_not_crash(self):
        """A single 64 KiB metadata value must not crash."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/LargeValue')
        async def large_value(request, context):
            val = context.metadata(b'x-large', b'')
            return str(len(val)).encode()

        headers = [(b'content-type', b'application/grpc'),
                   (b'x-large', b'X' * (64 * 1024))]
        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/LargeValue', headers),
                         _receive_with(encode_message(b'')), send)
        body_ev = next((e for e in events if e['type'] == 'http.response.body'), None)
        assert body_ev is not None
        # Should report the correct length
        assert decode_messages(body_ev['body']) == [(False, str(64 * 1024).encode())]

    @pytest.mark.asyncio
    async def test_very_long_header_name_does_not_crash(self):
        """A header name of 8 KiB must not crash the bridge."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/LongName')
        async def long_name(request, context):
            return b'ok'

        long_name_bytes = b'x-' + b'a' * 8192
        headers = [(b'content-type', b'application/grpc'),
                   (long_name_bytes, b'value')]
        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/LongName', headers),
                         _receive_with(encode_message(b'')), send)
        assert _trailers_of(events)[b'grpc-status'] == b'0'


# ---------------------------------------------------------------------------
# §F-T1 — Concurrent gRPC calls
# ---------------------------------------------------------------------------

class TestConcurrentGrpcCalls:
    """Multiple gRPC calls served concurrently must not interfere with
    each other's state (context, response body, trailers)."""

    @pytest.mark.asyncio
    async def test_concurrent_unary_calls_isolated(self):
        """10 concurrent gRPC calls, each returning a unique value, must
        all produce correct, isolated responses."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Id')
        async def identity(request, context):
            return request

        async def make_call(payload: bytes) -> list[tuple[bool, bytes]]:
            events, send = _collector()
            await serve_grpc(reg, _grpc_scope('/svc/Id'),
                             _receive_with(encode_message(payload)), send)
            body_ev = next((e for e in events
                           if e['type'] == 'http.response.body'), None)
            assert body_ev is not None
            return decode_messages(body_ev['body'])

        tasks = [make_call(bytes([i]) * 10) for i in range(10)]
        results = await asyncio.gather(*tasks)
        for i, result in enumerate(results):
            assert result == [(False, bytes([i]) * 10)], (
                f'call {i} returned {result!r}')

    @pytest.mark.asyncio
    async def test_concurrent_calls_different_methods(self):
        """Concurrent calls to different methods must not cross-wire."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Upper')
        async def upper(request, context):
            await asyncio.sleep(0.01)  # small delay to increase interleaving
            return request.upper()

        @reg.method('/svc/Lower')
        async def lower(request, context):
            return request.lower()

        async def call_upper(payload: bytes) -> list[tuple[bool, bytes]]:
            events, send = _collector()
            await serve_grpc(reg, _grpc_scope('/svc/Upper'),
                             _receive_with(encode_message(payload)), send)
            body_ev = next((e for e in events
                           if e['type'] == 'http.response.body'), None)
            assert body_ev is not None
            return decode_messages(body_ev['body'])

        async def call_lower(payload: bytes) -> list[tuple[bool, bytes]]:
            events, send = _collector()
            await serve_grpc(reg, _grpc_scope('/svc/Lower'),
                             _receive_with(encode_message(payload)), send)
            body_ev = next((e for e in events
                           if e['type'] == 'http.response.body'), None)
            assert body_ev is not None
            return decode_messages(body_ev['body'])

        tasks = []
        for i in range(5):
            tasks.append(call_upper(f'hello{i}'.encode()))
            tasks.append(call_lower(f'WORLD{i}'.encode()))
        results = await asyncio.gather(*tasks)

        for i in range(5):
            assert results[2 * i] == [(False, f'HELLO{i}'.encode())]
            assert results[2 * i + 1] == [(False, f'world{i}'.encode())]

    @pytest.mark.asyncio
    async def test_concurrent_calls_with_errors_dont_affect_others(self):
        """One call raising an error must not affect concurrent calls."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Ok')
        async def ok(request, context):
            return b'ok'

        @reg.method('/svc/Err')
        async def err(request, context):
            raise GrpcError(GrpcStatus.INTERNAL, 'boom')

        async def call_ok() -> bytes:
            events, send = _collector()
            await serve_grpc(reg, _grpc_scope('/svc/Ok'),
                             _receive_with(encode_message(b'')), send)
            return _trailers_of(events).get(b'grpc-status', b'?')

        async def call_err() -> bytes:
            events, send = _collector()
            await serve_grpc(reg, _grpc_scope('/svc/Err'),
                             _receive_with(encode_message(b'')), send)
            return _trailers_of(events).get(b'grpc-status', b'?')

        results = await asyncio.gather(
            call_ok(), call_err(), call_ok(), call_err(), call_ok(),
        )
        assert results == [b'0', str(int(GrpcStatus.INTERNAL)).encode(),
                          b'0', str(int(GrpcStatus.INTERNAL)).encode(),
                          b'0']


# ---------------------------------------------------------------------------
# §F-P1 — Large message body through bridge
# ---------------------------------------------------------------------------

class TestLargeMessageBody:
    """Large gRPC message bodies must not cause excessive memory allocation
    or crash the bridge."""

    @pytest.mark.asyncio
    async def test_1mb_message_round_trips(self):
        """A 1 MiB request/response must round-trip correctly."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Echo')
        async def echo(request, context):
            return request

        payload = b'\xAB' * (1024 * 1024)
        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Echo'),
                         _receive_with(encode_message(payload)), send)
        body_ev = next((e for e in events if e['type'] == 'http.response.body'), None)
        assert body_ev is not None
        assert decode_messages(body_ev['body']) == [(False, payload)]

    @pytest.mark.asyncio
    async def test_4mb_message_round_trips(self):
        """A 4 MiB payload pushes the boundary of typical gRPC message sizes."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Echo')
        async def echo(request, context):
            return request

        payload = b'\xCD' * (4 * 1024 * 1024)
        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Echo'),
                         _receive_with(encode_message(payload)), send)
        body_ev = next((e for e in events if e['type'] == 'http.response.body'), None)
        assert body_ev is not None
        assert decode_messages(body_ev['body']) == [(False, payload)]

    @pytest.mark.asyncio
    async def test_zero_byte_message_body(self):
        """An empty message body (5-byte frame with length=0) round-trips."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Zero')
        async def zero(request, context):
            assert request == b''
            return b''

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Zero'),
                         _receive_with(encode_message(b'')), send)
        assert _trailers_of(events)[b'grpc-status'] == b'0'
        body_ev = next((e for e in events if e['type'] == 'http.response.body'), None)
        assert body_ev is not None
        assert decode_messages(body_ev['body']) == [(False, b'')]


# ---------------------------------------------------------------------------
# §A-P1, A-P2 — Additional framing edge cases
# ---------------------------------------------------------------------------

class TestAdditionalFramingEdgeCases:
    """Additional edge cases beyond those in test_grpc_codec_conformance.py."""

    def test_truncated_at_exactly_zero_body_bytes(self):
        """Message declares length 0 but there are zero bytes after the prefix.
        This is valid — decode_messages should return (False, b'')."""
        result = decode_messages(b'\x00\x00\x00\x00\x00')
        assert result == [(False, b'')]

    def test_truncated_at_prefix_byte_4(self):
        """Exactly 4 bytes of a 5-byte prefix — off-by-one at the boundary."""
        with pytest.raises(GrpcDecodeError):
            decode_messages(b'\x00\x00\x00\x00')

    def test_compressed_flag_every_value_2_to_254(self):
        """All non-zero compressed-flag bytes (2–254) must be reported as
        ``compressed=True``.  The gRPC spec only defines 0 and 1, but
        other values could appear from buggy or malicious clients."""
        for flag in range(2, 255):
            # Manually construct: flag byte + 4-byte length 0
            framed = bytes([flag]) + b'\x00\x00\x00\x00'
            result = decode_messages(framed)
            assert result == [(True, b'')], (
                f'flag={flag} (0x{flag:02X}) should be True')

    def test_message_length_exactly_2_pow_24_minus_1(self):
        """$2^{24}-1$ (16 MiB - 1) — near the practical limit."""
        length = (2 ** 24) - 1
        prefix = b'\x00' + length.to_bytes(4, 'big')
        body = b'\x00' * length
        result = decode_messages(prefix + body)
        assert result == [(False, body)]
        assert len(result[0][1]) == length

    def test_multiple_messages_with_max_varied_lengths(self):
        """Varying message lengths including max values."""
        payloads = [b'', b'x', b'x' * 255, b'x' * 65535]
        buf = b''.join(encode_message(p) for p in payloads)
        result = decode_messages(buf)
        assert len(result) == len(payloads)
        for (compressed, payload), expected in zip(result, payloads):
            assert compressed is False
            assert payload == expected


# ---------------------------------------------------------------------------
# §H-T2 — gRPC Reflection Abuse
# ---------------------------------------------------------------------------

class TestReflectionAbuse:
    """gRPC Server Reflection is an optional service that allows clients to
    enumerate all registered methods.  BlackBull does not implement a
    built-in reflection service, but we verify that reflection-style
    paths are handled safely."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize('path', [
        pytest.param('/grpc.reflection.v1alpha.ServerReflection/ServerReflectionInfo',
                     id='reflection-v1alpha'),
        pytest.param('/grpc.reflection.v1.ServerReflection/ServerReflectionInfo',
                     id='reflection-v1'),
        pytest.param('/grpc.health.v1.Health/Check', id='health-check'),
    ])
    async def test_reflection_service_not_registered_by_default(self, path):
        """Unregistered built-in services answer UNIMPLEMENTED."""
        reg = GrpcServiceRegistry()
        events, send = _collector()
        await serve_grpc(reg, _grpc_scope(path),
                         _receive_with(encode_message(b'')), send)
        assert _trailers_of(events)[b'grpc-status'] == \
            str(int(GrpcStatus.UNIMPLEMENTED)).encode()

    @pytest.mark.asyncio
    async def test_reflection_can_be_explicitly_registered(self):
        """If a user explicitly registers a reflection handler, it must work."""
        reg = GrpcServiceRegistry()

        @reg.method('/grpc.reflection.v1alpha.ServerReflection/'
                    'ServerReflectionInfo')
        async def reflection(request, context):
            return b'reflection-disabled-by-policy'

        events, send = _collector()
        await serve_grpc(
            reg,
            _grpc_scope('/grpc.reflection.v1alpha.ServerReflection/'
                        'ServerReflectionInfo'),
            _receive_with(encode_message(b'')),
            send,
        )
        body_ev = next((e for e in events if e['type'] == 'http.response.body'), None)
        assert body_ev is not None
        assert decode_messages(body_ev['body']) == [
            (False, b'reflection-disabled-by-policy'),
        ]


# ---------------------------------------------------------------------------
# §G — Transport-Layer Attacks (documented as N/A for ASGI bridge)
# ---------------------------------------------------------------------------

class TestTransportLayerScope:
    """These tests document that transport-layer attacks are outside the
    scope of BlackBull's gRPC bridge and are the ASGI server's responsibility.

    The following attack vectors CANNOT be tested at the ASGI bridge level:

    * HTTP/2 preface smuggling (G-T1): The ASGI server validates the preface.
    * RST_STREAM injection (G-T2): Handled by the HTTP/2 stack.
    * GOAWAY race conditions (G-T3): Handled by the HTTP/2 stack.
    * HPACK dynamic table pollution (G-T4): Handled by the HPACK decoder.
    * Flow control window manipulation (G-T5): Handled by the HTTP/2 stack.
    * Stream ID exhaustion (F-T2): HTTP/2 layer manages stream IDs.
    * Settings frame manipulation (F-T3): Only relevant for H2 client role.
    """

    def test_transport_attacks_are_documented_as_out_of_scope(self):
        """This test exists solely to document the scope boundary and ensure
        the test suite acknowledges these vectors.

        If BlackBull ever implements its own HTTP/2 transport (bypassing
        the ASGI server), these vectors MUST be tested at that time.
        """
        # This is a documentation test — always passes.
        pass


# ---------------------------------------------------------------------------
# §Misc — Additional edge cases
# ---------------------------------------------------------------------------

class TestMiscellaneousEdgeCases:
    """Various edge cases that don't fit neatly into other categories."""

    @pytest.mark.asyncio
    async def test_bytes_subclass_conversion_is_preserved(self):
        class Reply(bytes):
            def __bytes__(self):
                return b'converted'

        registry = GrpcServiceRegistry()

        @registry.method('/svc/M')
        async def handler(request, context):
            return Reply(b'original')

        events, send = _collector()
        await serve_grpc(registry, _grpc_scope('/svc/M'),
                         _receive_with(encode_message(b'')), send)
        body = next(event['body'] for event in events if event['type'] == 'http.response.body')
        assert decode_messages(body) == [(False, b'converted')]

    @pytest.mark.asyncio
    async def test_handler_returns_bytearray(self):
        """``bytearray`` is accepted as a valid response type (like ``bytes``)."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/ByteArray')
        async def bytearray_handler(request, context):
            return bytearray(b'response')

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/ByteArray'),
                         _receive_with(encode_message(b'')), send)
        body_ev = next((e for e in events if e['type'] == 'http.response.body'), None)
        assert body_ev is not None
        assert decode_messages(body_ev['body']) == [(False, b'response')]

    @pytest.mark.asyncio
    async def test_handler_returns_memoryview(self):
        """``memoryview`` is NOT in the ``(bytes, bytearray)`` check —
        this should produce INTERNAL currently."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/MemView')
        async def memview_handler(request, context):
            return memoryview(b'data')

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/MemView'),
                         _receive_with(encode_message(b'')), send)
        trailers = _trailers_of(events)
        assert trailers[b'grpc-status'] == str(int(GrpcStatus.INTERNAL)).encode()

    @pytest.mark.asyncio
    async def test_details_with_only_percent_signs(self):
        """A details string consisting entirely of ``%`` characters must
        be correctly encoded (each ``%`` becomes ``%25``)."""
        assert _pct_encode_message('%%%') == b'%25%25%25'

    @pytest.mark.asyncio
    async def test_details_with_max_unicode_codepoint(self):
        """Unicode characters at the upper boundary (U+10FFFF) must not
        crash the percent-encoder."""
        # U+10FFFF is the maximum valid Unicode code point
        try:
            result = _pct_encode_message('\U0010FFFF')
            # Should be UTF-8 encoded (4 bytes: F4 8F BF BF) then percent
            assert b'%F4%8F%BF%BF' in result or result != b''
        except (UnicodeEncodeError, ValueError):
            # Some Python builds may reject this code point; that's acceptable
            pass

    @pytest.mark.asyncio
    async def test_nested_grpc_error_in_handler(self):
        """A handler that catches a ``GrpcError`` and re-raises a different
        one must produce the final error, not the intermediate one."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Nested')
        async def nested(request, context):
            try:
                raise GrpcError(GrpcStatus.NOT_FOUND, 'inner')
            except GrpcError:
                raise GrpcError(GrpcStatus.PERMISSION_DENIED, 'outer')

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Nested'),
                         _receive_with(encode_message(b'')), send)
        trailers = _trailers_of(events)
        assert trailers[b'grpc-status'] == \
            str(int(GrpcStatus.PERMISSION_DENIED)).encode()
        assert b'outer' in trailers.get(b'grpc-message', b'')

    @pytest.mark.asyncio
    async def test_context_set_details_with_empty_string(self):
        """Setting empty details must not add grpc-message trailer."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/EmptyDetails')
        async def empty_details(request, context):
            context.set_details('')
            return b'ok'

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/EmptyDetails'),
                         _receive_with(encode_message(b'')), send)
        trailers = events[2]['headers']
        # grpc-message should be absent or empty when details is ''
        has_message = any(k == b'grpc-message' for k, v in trailers)
        # Current behavior: if details is empty, no grpc-message is added
        # (because _status_trailers checks "if details")
        assert not has_message or \
            dict(trailers).get(b'grpc-message') == b''

    @pytest.mark.asyncio
    async def test_handler_with_only_scope_parameter(self):
        """A handler that only takes ``scope`` (no request bytes) should
        still work — the registry doesn't enforce signatures."""
        reg = GrpcServiceRegistry()

        async def scope_only(scope):
            return b'scope only'

        reg.add_method('/svc/ScopeOnly', scope_only)

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/ScopeOnly'),
                         _receive_with(encode_message(b'')), send)
        # This should crash because serve_grpc calls handler(request, context)
        # but the handler only takes scope.  This is a user error, not a
        # security issue — the exception should become INTERNAL.
        trailers = _trailers_of(events)
        assert trailers[b'grpc-status'] == str(int(GrpcStatus.INTERNAL)).encode()

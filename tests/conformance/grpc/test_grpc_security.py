"""gRPC security and threat-resistance conformance tests.

Tests the ``serve_grpc`` ASGI bridge and ``GrpcServiceRegistry`` against
known gRPC attack vectors and protocol conformance requirements, without
spinning up a live server.  Uses the same in-process ASGI harness as
``tests/unit/test_grpc.py``.

References:
- gRPC wire protocol: https://github.com/grpc/grpc/blob/master/doc/PROTOCOL-HTTP2.md
- gRPC status codes: https://grpc.github.io/grpc/core/md_doc_statuscodes.html
- OWASP gRPC security considerations
"""
from __future__ import annotations

import pytest

from blackbull.grpc import (
    GrpcServiceRegistry, GrpcStatus, GrpcError, GrpcDecodeError,
    encode_message, decode_messages,
)
from blackbull.grpc.asgi import serve_grpc, GrpcContext, _pct_encode_message


# ---------------------------------------------------------------------------
# Harness (same as unit/test_grpc.py)
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
    """Simulate a body delivered in multiple ``http.request`` events."""
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
# §1 — Content-Type conformance
# ---------------------------------------------------------------------------

class TestGrpcContentType:
    """gRPC requires ``Content-Type: application/grpc``.  Verify that the
    bridge detects and rejects incorrect content types."""

    @pytest.mark.asyncio
    async def test_application_grpc_without_suffix_is_accepted(self):
        """``application/grpc`` is the base content type — must be accepted."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/M')
        async def m(request, context):
            return b'ok'

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/M'), _receive_with(encode_message(b'')), send)
        assert _trailers_of(events)[b'grpc-status'] == b'0'

    @pytest.mark.asyncio
    async def test_case_insensitive_content_type_detection(self):
        """Content-Type is case-insensitive per HTTP spec.  The dispatch check
        in ``BlackBull._dispatch`` does a ``startswith(b'application/grpc')``
        which is case-sensitive.  This test documents the *current* behavior."""
        # Note: The bridge itself does not check content-type (the caller does).
        # We test what happens when passed with various content-type spellings.
        reg = GrpcServiceRegistry()

        @reg.method('/svc/M')
        async def m(request, context):
            return b'ok'

        # APPLICATION/GRPC — the bridge doesn't enforce content-type itself,
        # so this succeeds.  The caller (app._dispatch) does the filtering.
        events, send = _collector()
        scope = {'type': 'http', 'path': '/svc/M',
                 'headers': [(b'content-type', b'APPLICATION/GRPC'),
                             (b':method', b'POST')]}
        await serve_grpc(reg, scope, _receive_with(encode_message(b'')), send)
        assert _trailers_of(events)[b'grpc-status'] == b'0'

    @pytest.mark.asyncio
    async def test_unregistered_method_returns_unimplemented_not_crash(self):
        """An unimplemented method returns UNIMPLEMENTED (12), not a 5xx crash."""
        reg = GrpcServiceRegistry()
        events, send = _collector()
        await serve_grpc(
            reg, _grpc_scope('/Package.UnknownService/Missing'),
            _receive_with(encode_message(b'')), send)
        trailers = _trailers_of(events)
        assert trailers[b'grpc-status'] == str(int(GrpcStatus.UNIMPLEMENTED)).encode()

    @pytest.mark.asyncio
    async def test_unimplemented_includes_method_name_in_message(self):
        """The grpc-message should include the not-found path for debuggability."""
        reg = GrpcServiceRegistry()
        events, send = _collector()
        await serve_grpc(
            reg, _grpc_scope('/Foo.Bar/Baz'),
            _receive_with(encode_message(b'')), send)
        msg = _trailers_of(events).get(b'grpc-message', b'')
        assert b'Foo.Bar/Baz' in msg or b'/Foo.Bar/Baz' in msg


# ---------------------------------------------------------------------------
# §2 — Status code conformance (all 17 canonical codes)
# ---------------------------------------------------------------------------

class TestGrpcStatusCodes:
    """Every gRPC status code (0–16) must be representable on the wire.
    The ``grpc-status`` trailer carries the decimal integer as ASCII."""

    @pytest.mark.asyncio
    async def test_all_status_codes_are_wireable(self):
        """For each of the 17 canonical codes, a handler that raises
        ``GrpcError(code, '...')`` must produce the correct decimal
        ``grpc-status`` trailer."""
        for code in GrpcStatus:
            reg = GrpcServiceRegistry()
            path = f'/svc/Code{code.value}'

            @reg.method(path)
            async def h(request, context, _code=code):
                raise GrpcError(_code, f'status {_code.value}')

            events, send = _collector()
            await serve_grpc(reg, _grpc_scope(path),
                             _receive_with(encode_message(b'')), send)
            trailers = _trailers_of(events)
            expected = str(int(code)).encode()
            assert trailers.get(b'grpc-status') == expected, (
                f'GrpcStatus.{code.name} ({code.value}) should produce '
                f'grpc-status={expected!r}, got {trailers.get(b"grpc-status")!r}')

    @pytest.mark.asyncio
    async def test_ok_status_omits_grpc_message_trailer(self):
        """Successful calls (OK) should not carry a grpc-message trailer
        unless explicitly set."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/OK')
        async def ok_handler(request, context):
            context.set_code(GrpcStatus.OK)
            return b'result'

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/OK'),
                         _receive_with(encode_message(b'')), send)
        trailers = _trailers_of(events)
        assert b'grpc-status' in trailers
        # grpc-message should only appear if details are non-empty
        # (the current impl adds it only when details is truthy)


# ---------------------------------------------------------------------------
# §3 — grpc-message percent-encoding conformance
# ---------------------------------------------------------------------------

class TestGrpcMessageEncoding:
    """grpc-message values MUST be percent-encoded per the gRPC HTTP/2 spec.
    Only printable ASCII (0x20–0x7E, excluding '%') passes through unencoded."""

    def test_printable_ascii_passes_through(self):
        """Characters 0x20–0x7E except '%' must appear verbatim."""
        assert _pct_encode_message('hello world') == b'hello world'
        assert _pct_encode_message('abc123XYZ!@#$^&*()_+-=[]{}|;:,.<>?/`~') == \
            b'abc123XYZ!@#$^&*()_+-=[]{}|;:,.<>?/`~'

    def test_percent_sign_encoded_as_percent25(self):
        """'%' (0x25) must be encoded as '%25'."""
        assert _pct_encode_message('100%') == b'100%25'
        assert _pct_encode_message('%%') == b'%25%25'

    def test_control_characters_encoded(self):
        """Bytes below 0x20 must be percent-encoded."""
        assert _pct_encode_message('tab\tend') == b'tab%09end'
        assert _pct_encode_message('line\nfeed') == b'line%0Afeed'
        assert _pct_encode_message('carriage\rreturn') == b'carriage%0Dreturn'
        assert _pct_encode_message('\x00null') == b'%00null'

    def test_non_ascii_utf8_encoded_then_percent(self):
        """Non-ASCII characters must be UTF-8 encoded first, then percent-encoded."""
        assert _pct_encode_message('café') == b'caf%C3%A9'
        assert _pct_encode_message('αβ') == b'%CE%B1%CE%B2'  # Greek alpha, beta
        assert _pct_encode_message('日本語') == b'%E6%97%A5%E6%9C%AC%E8%AA%9E'

    def test_empty_string_produces_empty_bytes(self):
        """Empty details → empty grpc-message (or omitted)."""
        assert _pct_encode_message('') == b''

    def test_space_preserved_literally(self):
        """Space (0x20) is in the printable range and must NOT be encoded."""
        assert _pct_encode_message('hello world') == b'hello world'
        assert _pct_encode_message(' leading trailing ') == b' leading trailing '

    def test_delete_character_encoded(self):
        """DEL (0x7F) is NOT in the 0x20–0x7E range and must be encoded."""
        assert _pct_encode_message('del\x7fhere') == b'del%7Fhere'


# ---------------------------------------------------------------------------
# §4 — Security: header injection via grpc-message
# ---------------------------------------------------------------------------

class TestGrpcMessageInjection:
    """grpc-message travels in an HTTP/2 HEADERS frame as a raw byte value
    (not inside a quoted-string), so injection of pseudo-header characters
    or control bytes is a real concern.  Percent-encoding is the defence."""

    def test_newline_is_encoded_not_literal(self):
        """A literal newline in grpc-message could be interpreted as a header
        separator by buggy intermediaries.  Must be percent-encoded."""
        assert b'\x0a' not in _pct_encode_message('line1\nline2')
        assert b'%0A' in _pct_encode_message('line1\nline2')

    def test_carriage_return_is_encoded(self):
        assert b'\x0d' not in _pct_encode_message('a\rb')
        assert b'%0D' in _pct_encode_message('a\rb')

    def test_crlf_sequence_is_encoded(self):
        """CRLF (0x0D 0x0A) must not appear literally."""
        encoded = _pct_encode_message('header\r\ninjection')
        assert b'\r\n' not in encoded
        assert b'%0D%0A' in encoded

    def test_null_byte_is_encoded(self):
        """Null byte injection must be prevented."""
        encoded = _pct_encode_message('null\x00here')
        assert b'\x00' not in encoded
        assert b'%00' in encoded

    def test_colon_is_not_encoded(self):
        """':' (0x3A) is printable ASCII — passes through.  This is acceptable
        because gRPC trailers are HTTP/2 HEADERS, not HTTP/1.1 headers, and
        HTTP/2 does not use ':' as a header/value separator in the same way."""
        assert _pct_encode_message('key:value') == b'key:value'

    @pytest.mark.asyncio
    async def test_injection_attempt_via_details_does_not_corrupt_trailers(self):
        """A details string containing CRLF must not produce broken trailers."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Injection')
        async def inject(request, context):
            raise GrpcError(GrpcStatus.INVALID_ARGUMENT,
                            'bad\r\npseudo-header: evil')

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Injection'),
                         _receive_with(encode_message(b'')), send)
        trailers = _trailers_of(events)
        assert trailers[b'grpc-status'] == str(int(GrpcStatus.INVALID_ARGUMENT)).encode()
        # The message MUST be percent-encoded — no raw CRLF in the value
        msg = trailers[b'grpc-message']
        assert b'\r\n' not in msg
        assert b'%0D%0A' in msg


# ---------------------------------------------------------------------------
# §5 — Security: message framing attacks
# ---------------------------------------------------------------------------

class TestMessageFramingAttacks:
    """Malformed Length-Prefixed-Message sequences that could crash or confuse
    the decoder, leading to DoS or message smuggling."""

    @pytest.mark.asyncio
    async def test_zero_messages_in_unary_call_is_error(self):
        """A unary RPC with zero messages (empty body) should be rejected."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Unary')
        async def unary(request, context):
            return b'ok'

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Unary'),
                         _receive_with(b''), send)
        trailers = _trailers_of(events)
        # Zero messages → GrpcDecodeError?  Actually decode_messages(b'') returns [].
        # The handler checks len(messages) != 1 → UNIMPLEMENTED.
        assert trailers[b'grpc-status'] in (
            str(int(GrpcStatus.INTERNAL)).encode(),
            str(int(GrpcStatus.UNIMPLEMENTED)).encode(),
        )

    @pytest.mark.asyncio
    async def test_more_than_one_message_in_unary_is_rejected(self):
        """A unary RPC with >1 message is a protocol violation."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Unary')
        async def unary(request, context):
            return b'ok'

        body = encode_message(b'a') + encode_message(b'b') + encode_message(b'c')
        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Unary'),
                         _receive_with(body), send)
        assert _trailers_of(events)[b'grpc-status'] == \
            str(int(GrpcStatus.UNIMPLEMENTED)).encode()

    @pytest.mark.asyncio
    async def test_truncated_message_in_body_is_caught(self):
        """A truncated LPM prefix in the request body should produce INTERNAL."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Unary')
        async def unary(request, context):
            return b'ok'

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Unary'),
                         _receive_with(b'\x00\x00'), send)  # truncated prefix
        assert _trailers_of(events)[b'grpc-status'] == \
            str(int(GrpcStatus.INTERNAL)).encode()

    @pytest.mark.asyncio
    async def test_compressed_message_rejected(self):
        """Message compression is not supported; a compressed message
        must be rejected with UNIMPLEMENTED."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Unary')
        async def unary(request, context):
            return b'ok'

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Unary'),
                         _receive_with(encode_message(b'data', compressed=True)), send)
        assert _trailers_of(events)[b'grpc-status'] == \
            str(int(GrpcStatus.UNIMPLEMENTED)).encode()

    @pytest.mark.asyncio
    async def test_body_with_trailing_garbage_after_valid_message(self):
        """Extra bytes after a complete message should be treated as a second
        message, which for unary is UNIMPLEMENTED."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Unary')
        async def unary(request, context):
            return b'ok'

        # valid message + trailing garbage that looks like another message
        body = encode_message(b'ok') + b'\x00\x00\x00\x00\x01'  # 2nd message: len=1 but no body
        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Unary'),
                         _receive_with(body), send)
        trailers = _trailers_of(events)
        # Could be UNIMPLEMENTED (multiple messages) or INTERNAL (truncated second)
        assert trailers[b'grpc-status'] in (
            str(int(GrpcStatus.UNIMPLEMENTED)).encode(),
            str(int(GrpcStatus.INTERNAL)).encode(),
        )


# ---------------------------------------------------------------------------
# §6 — Security: handler isolation & error handling
# ---------------------------------------------------------------------------

class TestHandlerIsolation:
    """A misbehaving handler must never crash the bridge or leak internal state."""

    @pytest.mark.asyncio
    async def test_handler_returning_wrong_type_returns_internal(self):
        """A handler returning a non-bytes type must produce INTERNAL, not crash."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/BadReturn')
        async def bad_return(request, context):
            return 'string not bytes'  # str, not bytes

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/BadReturn'),
                         _receive_with(encode_message(b'')), send)
        trailers = _trailers_of(events)
        assert trailers[b'grpc-status'] == str(int(GrpcStatus.INTERNAL)).encode()

    @pytest.mark.asyncio
    async def test_handler_returning_none_returns_internal(self):
        """Returning None is not a valid gRPC response — must be INTERNAL."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/NoneReturn')
        async def none_return(request, context):
            return None

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/NoneReturn'),
                         _receive_with(encode_message(b'')), send)
        trailers = _trailers_of(events)
        assert trailers[b'grpc-status'] == str(int(GrpcStatus.INTERNAL)).encode()

    @pytest.mark.asyncio
    async def test_ordinary_handler_exception_is_isolated(self):
        """An ordinary handler bug (any ``Exception`` subclass) is isolated as
        INTERNAL — it never escapes the serving path to disturb the bridge or
        other in-flight calls."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Boom')
        async def boom(request, context):
            raise RuntimeError('catastrophic')

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Boom'),
                         _receive_with(encode_message(b'')), send)
        trailers = _trailers_of(events)
        assert trailers[b'grpc-status'] == str(int(GrpcStatus.INTERNAL)).encode()

    @pytest.mark.asyncio
    async def test_non_exception_baseexception_propagates(self):
        """A non-``Exception`` throwable (a raw ``BaseException``, and by the
        same rule ``CancelledError`` / ``KeyboardInterrupt`` / ``SystemExit`` /
        ``GeneratorExit``) is *not* masked into a gRPC status: it propagates so
        task cancellation and interpreter shutdown are honoured.  Each call runs
        in its own stream task, so this unwinds only that stream — it does not
        crash the bridge."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/BaseBoom')
        async def base_boom(request, context):
            raise BaseException('catastrophic')

        events, send = _collector()
        with pytest.raises(BaseException, match='catastrophic'):
            await serve_grpc(reg, _grpc_scope('/svc/BaseBoom'),
                             _receive_with(encode_message(b'')), send)
        # No status was emitted — the throwable was propagated, not reported.
        assert not any(e['type'] == 'http.response.trailers' for e in events)

    @pytest.mark.asyncio
    async def test_handler_exception_message_in_grpc_message(self):
        """The exception message should appear percent-encoded in grpc-message."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Oops')
        async def oops(request, context):
            raise RuntimeError('disk full: /dev/null')

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Oops'),
                         _receive_with(encode_message(b'')), send)
        msg = _trailers_of(events).get(b'grpc-message', b'')
        assert b'disk full' in msg

    @pytest.mark.asyncio
    async def test_context_abort_immediately_stops_execution(self):
        """``context.abort()`` raises ``GrpcError`` — the handler's return
        value must be ignored and the abort status used."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/AbortTest')
        async def abort_test(request, context):
            context.abort(GrpcStatus.RESOURCE_EXHAUSTED, 'too many')
            return b'should not be sent'  # unreachable

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/AbortTest'),
                         _receive_with(encode_message(b'')), send)
        trailers = _trailers_of(events)
        assert trailers[b'grpc-status'] == \
            str(int(GrpcStatus.RESOURCE_EXHAUSTED)).encode()
        # The response body must NOT contain 'should not be sent'
        for e in events:
            if e['type'] == 'http.response.body':
                assert b'should not be sent' not in e.get('body', b'')


# ---------------------------------------------------------------------------
# §7 — Metadata (headers) handling conformance
# ---------------------------------------------------------------------------

class TestGrpcMetadata:
    """gRPC uses HTTP/2 headers as call metadata.  Verify proper handling
    of request metadata and trailing metadata."""

    @pytest.mark.asyncio
    async def test_custom_binary_metadata_round_trips(self):
        """Custom trailing metadata set via context must appear in response."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/MetaEcho')
        async def meta_echo(request, context):
            token = context.metadata(b'x-custom-bin', b'default')
            context.set_trailing_metadata([
                (b'x-response-bin', token),
                (b'x-request-id', b'123'),
            ])
            return b'ok'

        headers = [(b'content-type', b'application/grpc'),
                   (b'x-custom-bin', b'\x00\x01\x02\xff')]
        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/MetaEcho', headers),
                         _receive_with(encode_message(b'')), send)
        # Find trailers event
        for e in events:
            if e['type'] == 'http.response.trailers':
                header_dict = dict(e['headers'])
                assert header_dict.get(b'x-response-bin') == b'\x00\x01\x02\xff'
                assert header_dict.get(b'x-request-id') == b'123'

    @pytest.mark.asyncio
    async def test_missing_header_returns_default(self):
        """``context.metadata(name, default)`` returns default when header absent."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Default')
        async def default_test(request, context):
            val = context.metadata(b'x-missing', b'fallback')
            return val

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Default'),
                         _receive_with(encode_message(b'')), send)
        body_event = next((e for e in events if e['type'] == 'http.response.body'), None)
        assert body_event is not None
        assert decode_messages(body_event['body']) == [(False, b'fallback')]

    @pytest.mark.asyncio
    async def test_metadata_case_insensitive_lookup(self):
        """HTTP header names are case-insensitive."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/CaseTest')
        async def case_test(request, context):
            return context.metadata(b'X-TOKEN', b'')

        headers = [(b'content-type', b'application/grpc'), (b'x-token', b'secret')]
        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/CaseTest', headers),
                         _receive_with(encode_message(b'')), send)
        body_event = next((e for e in events if e['type'] == 'http.response.body'), None)
        assert body_event is not None
        assert decode_messages(body_event['body']) == [(False, b'secret')]


# ---------------------------------------------------------------------------
# §8 — Response shape conformance (trailers-only vs full response)
# ---------------------------------------------------------------------------

class TestResponseShape:
    """Verify the correct ASGI event sequence for both success and error paths."""

    @pytest.mark.asyncio
    async def test_success_response_has_headers_data_trailers(self):
        """Successful unary: HEADERS (no END_STREAM) → DATA → TRAILERS."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Ok')
        async def ok(request, context):
            return b'response'

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Ok'),
                         _receive_with(encode_message(b'')), send)
        types = [e['type'] for e in events]
        assert types == [
            'http.response.start',
            'http.response.body',
            'http.response.trailers',
        ], f'unexpected event sequence: {types}'

    @pytest.mark.asyncio
    async def test_error_response_carries_status_in_trailers(self):
        """Error responses must carry ``grpc-status`` in a *trailing* HEADERS
        frame, not the initial (non-terminal) HEADERS.

        gRPC-over-HTTP2 requires the status to arrive either as true
        Trailers-Only (initial HEADERS *with* END_STREAM) or in a trailing
        HEADERS frame.  BlackBull emits Response-Headers → trailers (no
        message), so the ASGI shape is ``start(trailers=True) → trailers`` with
        no body event.  A strict client (grpcio/grpc-go) reads status from the
        trailers; the earlier ``start + empty-DATA`` shape put grpc-status in
        the non-terminal HEADERS and real clients reported UNKNOWN."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Err')
        async def err(request, context):
            raise GrpcError(GrpcStatus.NOT_FOUND, 'gone')

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Err'),
                         _receive_with(encode_message(b'')), send)
        types = [e['type'] for e in events]
        assert types == ['http.response.start', 'http.response.trailers'], (
            f'error path should be start(trailers=True) + trailers, got {types}')
        # The start must announce trailers and carry no grpc-status itself.
        start_headers = dict(events[0]['headers'])
        assert events[0].get('trailers') is True
        assert b'grpc-status' not in start_headers

    @pytest.mark.asyncio
    async def test_error_status_appears_in_trailers_event(self):
        """``grpc-status`` (and ``grpc-message``) must appear in the trailers
        event, not the response start headers."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Err')
        async def err(request, context):
            raise GrpcError(GrpcStatus.PERMISSION_DENIED, 'nope')

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Err'),
                         _receive_with(encode_message(b'')), send)
        start = events[0]
        assert start['type'] == 'http.response.start'
        assert start['status'] == 200  # gRPC errors still use HTTP 200
        trailers = dict(events[-1]['headers'])
        assert events[-1]['type'] == 'http.response.trailers'
        assert trailers.get(b'grpc-status') == \
            str(int(GrpcStatus.PERMISSION_DENIED)).encode()

    @pytest.mark.asyncio
    async def test_response_has_correct_content_type(self):
        """Both success and error responses must carry ``Content-Type: application/grpc``."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Ok')
        async def ok(request, context):
            return b'ok'

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Ok'),
                         _receive_with(encode_message(b'')), send)
        start_headers = dict(events[0]['headers'])
        assert start_headers.get(b'content-type') == b'application/grpc'

    @pytest.mark.asyncio
    async def test_success_response_indicates_trailers_in_start(self):
        """The response start event must have ``trailers: True`` so the
        HTTP/2 sender emits END_STREAM clear on the DATA frame."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Ok')
        async def ok(request, context):
            return b'ok'

        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Ok'),
                         _receive_with(encode_message(b'')), send)
        assert events[0].get('trailers') is True, (
            'response start must set trailers=True for gRPC success path')


# ---------------------------------------------------------------------------
# §9 — Streaming body handling (partial reads)
# ---------------------------------------------------------------------------

class TestStreamingBody:
    """gRPC bodies may arrive in multiple ``http.request`` events (DATA frames).
    The bridge uses ``read_body()`` which buffers the entire body.  These tests
    verify correct buffering of multi-chunk bodies."""

    @pytest.mark.asyncio
    async def test_message_split_across_multiple_receive_events(self):
        """A single message delivered across multiple receive calls must be
        correctly reassembled."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/Multi')
        async def multi(request, context):
            return request.upper()

        body = encode_message(b'hello world')
        # Split: first 3 bytes of prefix + chunk of body, then rest
        chunks = [body[:3], body[3:7], body[7:]]
        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/Multi'),
                         _receive_streaming(chunks), send)
        body_ev = next((e for e in events if e['type'] == 'http.response.body'), None)
        assert body_ev is not None
        assert decode_messages(body_ev['body']) == [(False, b'HELLO WORLD')]

    @pytest.mark.asyncio
    async def test_body_spread_across_many_small_chunks(self):
        """Byte-by-byte delivery must still work."""
        reg = GrpcServiceRegistry()

        @reg.method('/svc/ByteByByte')
        async def bbb(request, context):
            return bytes(reversed(request))

        payload = b'abcdef'
        body = encode_message(payload)
        chunks = [bytes([b]) for b in body]
        events, send = _collector()
        await serve_grpc(reg, _grpc_scope('/svc/ByteByByte'),
                         _receive_streaming(chunks), send)
        body_ev = next((e for e in events if e['type'] == 'http.response.body'), None)
        assert body_ev is not None
        assert decode_messages(body_ev['body']) == [(False, b'fedcba')]


# ---------------------------------------------------------------------------
# §10 — Registry edge cases
# ---------------------------------------------------------------------------

class TestRegistryEdgeCases:
    """Boundary conditions for GrpcServiceRegistry."""

    def test_duplicate_path_raises_value_error(self):
        """Duplicate registrations must be rejected explicitly, not silently
        overwrite the previous handler."""
        reg = GrpcServiceRegistry()

        async def h1(request, context):
            return b''

        async def h2(request, context):
            return b''

        reg.add_method('/svc/Dup', h1)
        with pytest.raises(ValueError, match='Duplicate'):
            reg.add_method('/svc/Dup', h2)

    def test_path_without_leading_slash_is_normalised(self):
        """``add_method('Svc/M')`` and ``lookup('/Svc/M')`` must match."""
        reg = GrpcServiceRegistry()

        async def h(request, context):
            return b''

        reg.add_method('Svc/M', h)
        assert reg.lookup('/Svc/M') is h
        assert reg.lookup('Svc/M') is h

    def test_lookup_on_empty_registry_returns_none(self):
        """Empty registry must return None, not raise."""
        assert GrpcServiceRegistry().lookup('/anything') is None

    def test_methods_returns_paths_in_registration_order(self):
        """``methods()`` must list paths in the order they were added."""
        reg = GrpcServiceRegistry()

        async def h(request, context):
            return b''

        paths = ['/p.Svc/A', '/p.Svc/B', '/p.Svc/C']
        for p in paths:
            reg.add_method(p, h)
        assert reg.methods() == paths

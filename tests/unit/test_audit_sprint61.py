"""Regression tests for the Sprint 61 HTTP/1.1 correctness batch.

Each test pins a specific bug from the 2026-07-07 comprehensive audit
(``.claude/planning/recommendations/comprehensive-audit-2026-07-07.md``):

- 1.1  chunked request body corrupted by partial reads
- 1.3  a raising ``@app.on_startup`` hook hangs the server forever
- 1.4  HTTP/1.1 writes a second response when a handler raises after completing
- 1.5  any non-websocket ``Upgrade:`` header kills the connection with no reply
- 1.6  unread request body is never drained on keep-alive
- 1.11 ``read_body`` returns a truncated body on client disconnect
- 1.13 mid-path ``{name:path}`` wildcard silently drops trailing segments
- 1.15 WebSocket handshake accepts a missing ``Sec-WebSocket-Key``
- 1.22a fault-injection production guard keyed on the wrong env var
- 1.22b ``make_self_signed_h2_context`` leaks the private key on disk

(Bug 1.12 — malformed dataclass body → 400 — is covered in
``tests/unit/test_router.py::TestDataclassBodyDeserialization``.)
"""
import asyncio
import gc
from http import HTTPStatus
from pathlib import Path

import pytest

from blackbull.asgi import ASGIEvent
from blackbull.request import read_body, read_json, read_text, ClientDisconnected
from blackbull.server.recipient import (
    AbstractReader, HTTP1Recipient, IncompleteReadError,
)
from blackbull.server.sender import AbstractWriter, HTTP1Sender



# ---------------------------------------------------------------------------
# In-process fakes
# ---------------------------------------------------------------------------

class _DribbleReader(AbstractReader):
    """A reader whose ``read(n)`` returns at most one byte per call.

    It deliberately does *not* override ``readexactly`` / ``readuntil`` — the
    ``AbstractReader`` base loops over ``read`` to satisfy those.  This is the
    fragmented-delivery case that the conformance ``_FakeReader`` (whole wire in
    one buffer) can never exercise: with the old ``read(chunk_size)`` the
    recipient would take a one-byte short read as the whole chunk (bug 1.1).
    """

    def __init__(self, data: bytes) -> None:
        self._buf = bytearray(data)

    async def read(self, n: int) -> bytes:
        if not self._buf:
            return b''
        take = min(n, 1)  # dribble: never hand back more than one byte
        chunk = bytes(self._buf[:take])
        del self._buf[:take]
        return chunk


class _RecordingWriter(AbstractWriter):
    """Captures every byte written so tests can inspect the wire."""

    def __init__(self) -> None:
        self.data = bytearray()

    async def write(self, data: bytes) -> None:
        self.data += data


async def _drain_recipient(recipient: HTTP1Recipient) -> bytes:
    """Consume a recipient to completion, concatenating body bytes."""
    body = bytearray()
    while True:
        event = await recipient()
        if event['type'] == ASGIEvent.HTTP_DISCONNECT:
            break
        body += event.get('body', b'')
        if not event.get('more_body', False):
            break
    return bytes(body)


# ---------------------------------------------------------------------------
# 1.1 — chunked body reassembled with readexactly (not up-to-n read)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chunked_body_reassembled_across_fragmented_reads():
    payload = b'x' * 200  # one chunk, larger than the 1-byte dribble
    wire = f'{len(payload):x}\r\n'.encode() + payload + b'\r\n0\r\n\r\n'
    recipient = HTTP1Recipient(
        _DribbleReader(wire),
        {'headers': [(b'transfer-encoding', b'chunked')]},
        chunk_size=64 * 1024,
    )
    body = await _drain_recipient(recipient)
    assert body == payload, 'chunk split across reads must be reassembled whole'


@pytest.mark.asyncio
async def test_chunked_multiple_chunks_fragmented():
    a, b = b'a' * 50, b'b' * 70
    wire = (f'{len(a):x}\r\n'.encode() + a + b'\r\n'
            + f'{len(b):x}\r\n'.encode() + b + b'\r\n0\r\n\r\n')
    recipient = HTTP1Recipient(
        _DribbleReader(wire),
        {'headers': [(b'transfer-encoding', b'chunked')]},
        chunk_size=64 * 1024,
    )
    assert await _drain_recipient(recipient) == a + b


# ---------------------------------------------------------------------------
# 1.6 — unread body is drained on keep-alive (bounded)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_needs_drain_false_for_bodyless_request():
    recipient = HTTP1Recipient(_DribbleReader(b''), {'headers': []})
    assert recipient.needs_drain() is False


@pytest.mark.asyncio
async def test_drain_consumes_unread_content_length_body():
    body = b'z' * 300
    recipient = HTTP1Recipient(
        _DribbleReader(body),
        {'headers': [(b'content-length', str(len(body)).encode())]},
        chunk_size=64 * 1024,
    )
    assert recipient.needs_drain() is True
    assert await recipient.drain(max_bytes=64 * 1024) is True
    assert recipient.needs_drain() is False


@pytest.mark.asyncio
async def test_drain_gives_up_past_bound_so_caller_closes():
    body = b'z' * 5000
    recipient = HTTP1Recipient(
        _DribbleReader(body),
        {'headers': [(b'content-length', str(len(body)).encode())]},
        chunk_size=64,
    )
    # A body larger than the drain bound returns False → caller closes the
    # connection instead of spending bandwidth draining it.
    assert await recipient.drain(max_bytes=1024) is False


# ---------------------------------------------------------------------------
# 1.4 — HTTP1Sender drops a second response once one has completed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sender_drops_second_bytes_response():
    writer = _RecordingWriter()
    sender = HTTP1Sender(writer)
    await sender(b'first', HTTPStatus.OK)
    boundary = len(writer.data)
    await sender(b'second', HTTPStatus.INTERNAL_SERVER_ERROR)  # must be dropped
    assert len(writer.data) == boundary, 'a completed response must not be followed by a second'
    assert b'first' in bytes(writer.data)
    assert b'second' not in bytes(writer.data)


@pytest.mark.asyncio
async def test_sender_drops_streamed_response_then_error():
    writer = _RecordingWriter()
    sender = HTTP1Sender(writer)
    await sender({'type': ASGIEvent.HTTP_RESPONSE_START, 'status': 200, 'headers': []})
    await sender({'type': ASGIEvent.HTTP_RESPONSE_BODY, 'body': b'done', 'more_body': False})
    boundary = len(writer.data)
    # Handler raised after completing → error handler emits a 2nd response.
    await sender({'type': ASGIEvent.HTTP_RESPONSE_START, 'status': 500, 'headers': []})
    await sender({'type': ASGIEvent.HTTP_RESPONSE_BODY, 'body': b'oops', 'more_body': False})
    assert len(writer.data) == boundary
    assert b'oops' not in bytes(writer.data)


@pytest.mark.asyncio
async def test_sender_reset_allows_next_keepalive_response():
    writer = _RecordingWriter()
    sender = HTTP1Sender(writer)
    await sender(b'first', HTTPStatus.OK)
    sender.reset_per_request_state()  # next keep-alive request
    await sender(b'second', HTTPStatus.OK)
    assert b'second' in bytes(writer.data)


# ---------------------------------------------------------------------------
# 1.11 — read_body surfaces a mid-body disconnect instead of truncating
# ---------------------------------------------------------------------------

def _receive_seq(events):
    it = iter(events)

    async def receive():
        return next(it)
    return receive


@pytest.mark.asyncio
async def test_read_body_raises_on_mid_body_disconnect():
    receive = _receive_seq([
        {'type': ASGIEvent.HTTP_REQUEST, 'body': b'par', 'more_body': True},
        {'type': ASGIEvent.HTTP_DISCONNECT},
    ])
    with pytest.raises(ClientDisconnected) as exc_info:
        await read_body(receive)
    assert exc_info.value.partial == b'par', 'the partial bytes are attached'


@pytest.mark.asyncio
async def test_read_body_complete_body_unaffected():
    receive = _receive_seq([
        {'type': ASGIEvent.HTTP_REQUEST, 'body': b'whole', 'more_body': False},
    ])
    assert await read_body(receive) == b'whole'


@pytest.mark.asyncio
async def test_read_json_and_text_propagate_disconnect():
    for reader in (read_json, read_text):
        receive = _receive_seq([
            {'type': ASGIEvent.HTTP_REQUEST, 'body': b'{', 'more_body': True},
            {'type': ASGIEvent.HTTP_DISCONNECT},
        ])
        with pytest.raises(ClientDisconnected):
            await reader(receive)


# ---------------------------------------------------------------------------
# 1.5 & 1.15 — Upgrade handling and WebSocket-key validation (via HTTP1Actor)
# ---------------------------------------------------------------------------

async def _noop_app(scope, receive, send):  # pragma: no cover - never dispatched here
    return None


def _make_actor(writer=None):
    from blackbull.server.http1_actor import HTTP1Actor
    return HTTP1Actor(
        _DribbleReader(b''), writer or _RecordingWriter(),
        app=_noop_app, aggregator=None,
    )


def test_upgrade_h2c_is_ignored_not_fatal():
    actor = _make_actor()
    request = (b'GET / HTTP/1.1\r\n'
               b'Host: example.com\r\n'
               b'Connection: Upgrade\r\n'
               b'Upgrade: h2c\r\n\r\n')
    scope = actor._parse(request)
    # A non-websocket Upgrade token is ignored: the request stays plain HTTP
    # rather than becoming scope['type']='h2c' and crashing dispatch (bug 1.5).
    assert scope['type'] == 'http'


def test_upgrade_websocket_still_switches_scope():
    actor = _make_actor()
    request = (b'GET /ws HTTP/1.1\r\n'
               b'Host: example.com\r\n'
               b'Connection: Upgrade\r\n'
               b'Upgrade: websocket\r\n\r\n')
    scope = actor._parse(request)
    assert scope['type'] == 'websocket'
    assert scope['scheme'] == 'ws'


@pytest.mark.asyncio
async def test_ws_handshake_rejects_missing_key():
    writer = _RecordingWriter()
    actor = _make_actor(writer)
    scope = {'headers': __import__('blackbull.headers', fromlist=['Headers']).Headers([
        (b'sec-websocket-version', b'13'),
    ])}
    ok = await actor._do_ws_handshake(scope)
    assert ok is False
    assert b'400' in bytes(writer.data), 'missing Sec-WebSocket-Key must 400'


@pytest.mark.asyncio
async def test_ws_handshake_rejects_malformed_key():
    from blackbull.headers import Headers
    writer = _RecordingWriter()
    actor = _make_actor(writer)
    scope = {'headers': Headers([
        (b'sec-websocket-key', b'not-16-bytes-base64!!'),
        (b'sec-websocket-version', b'13'),
    ])}
    assert await actor._do_ws_handshake(scope) is False
    assert b'400' in bytes(writer.data)


@pytest.mark.asyncio
async def test_ws_handshake_accepts_valid_key():
    from base64 import b64encode
    from blackbull.headers import Headers
    writer = _RecordingWriter()
    actor = _make_actor(writer)
    scope = {'headers': Headers([
        (b'sec-websocket-key', b64encode(b'0123456789abcdef')),  # 16 bytes
        (b'sec-websocket-version', b'13'),
    ])}
    assert await actor._do_ws_handshake(scope) is True


# ---------------------------------------------------------------------------
# 1.13 — mid-path {name:path} rejected at registration
# ---------------------------------------------------------------------------

def test_midpath_path_converter_rejected_at_registration():
    from blackbull.router import _RouteTrie, ConfigurationError
    trie = _RouteTrie()
    with pytest.raises(ConfigurationError, match='last segment'):
        trie.insert('/a/{p:path}/b', (), None, object())


def test_final_path_converter_allowed():
    from blackbull.router import _RouteTrie
    trie = _RouteTrie()
    trie.insert('/a/{p:path}', (), None, object())  # no raise


# ---------------------------------------------------------------------------
# 1.3 — a raising app_startup hook fails the lifespan instead of hanging
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_raising_startup_hook_fails_lifespan_without_hanging():
    from blackbull import BlackBull
    from blackbull.server.server import LifespanManager

    app = BlackBull()

    async def boom(event):
        raise RuntimeError('startup blew up')
    app._dispatcher.intercept('app_startup', boom)

    mgr = LifespanManager(app)
    # The bug hangs here forever; wait_for turns a regression into a failure.
    with pytest.raises(RuntimeError, match='startup blew up'):
        await asyncio.wait_for(mgr.__aenter__(), timeout=3.0)
    await mgr.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_clean_startup_still_completes():
    from blackbull import BlackBull
    from blackbull.server.server import LifespanManager

    app = BlackBull()
    started = {}

    async def note(event):
        started['ok'] = True
    app._dispatcher.intercept('app_startup', note)

    mgr = LifespanManager(app)
    await asyncio.wait_for(mgr.__aenter__(), timeout=3.0)
    assert started.get('ok') is True
    await mgr.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# 1.22a — fault-injection production guard keys on the real signal
# ---------------------------------------------------------------------------

def test_fault_server_guard_trips_on_blackbull_env_production(monkeypatch):
    from blackbull.env import reset_settings_cache
    from blackbull.fault_injection.h2_server import (
        _refuse_in_production, H2FaultServerError,
    )
    monkeypatch.delenv('BB_PRODUCTION', raising=False)
    monkeypatch.setenv('BLACKBULL_ENV', 'production')
    reset_settings_cache()
    with pytest.raises(H2FaultServerError):
        _refuse_in_production()


def test_fault_server_guard_trips_on_bb_production_override(monkeypatch):
    from blackbull.env import reset_settings_cache
    from blackbull.fault_injection.h2_server import (
        _refuse_in_production, H2FaultServerError,
    )
    monkeypatch.setenv('BLACKBULL_ENV', 'development')
    monkeypatch.setenv('BB_PRODUCTION', '1')
    reset_settings_cache()
    with pytest.raises(H2FaultServerError):
        _refuse_in_production()


def test_fault_server_guard_allows_development(monkeypatch):
    from blackbull.env import reset_settings_cache
    from blackbull.fault_injection.h2_server import _refuse_in_production
    monkeypatch.delenv('BB_PRODUCTION', raising=False)
    monkeypatch.setenv('BLACKBULL_ENV', 'development')
    reset_settings_cache()
    _refuse_in_production()  # no raise


# ---------------------------------------------------------------------------
# 1.22b — self-signed TLS context removes its keydir when collected
# ---------------------------------------------------------------------------

def test_self_signed_tls_context_cleans_up_keydir():
    pytest.importorskip('cryptography')
    from blackbull.fault_injection._tls import make_self_signed_h2_context

    ctx = make_self_signed_h2_context()
    keydir = Path(ctx.bb_ca_cert_path).parent
    assert keydir.exists()
    del ctx
    gc.collect()
    assert not keydir.exists(), 'the private-key tempdir must be removed on finalize'

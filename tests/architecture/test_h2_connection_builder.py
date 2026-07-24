"""Sprint 80 alloc hygiene (`.claude/planning/proposals/connection-alloc-hygiene.md`
Phase 2, 2026-07-24) — architecture guards for the H/2 lean ``Connection``
builder.

``blackbull/server/parser.py::_build_h2_connection`` bypasses the
dataclass-generated ``Connection.__init__`` via ``object.__new__`` + explicit
slot stores, to cut construction from ~549 ns to ~208 ns/req (measured in
``bench/results/h2-alloc-cpu-ab/20260723-161201Z-conn-decomp/``). Hand-writing
every slot store duplicates ``Connection``'s field list — a future field added
to the dataclass would otherwise leave a slot unset on the builder's output
(``AttributeError`` at first read, possibly deep in dispatch/middleware, far
from the actual bug). Two guards:

1. **Completeness + value parity** — the builder's output must set every
   declared field, with the same values a dataclass-constructed twin would
   produce (not just the ``compare=True`` subset ``Connection.__eq__``
   checks — several internal fields are ``compare=False``).
2. **Sentinel never escapes** — ``_build_h2_connection`` hands the plain-HTTP
   branch a shared ``_EMPTY_H2_EXTENSIONS`` dict for ``conn.extensions``
   (never allocated fresh per request). This is only safe because
   ``HTTP2Actor._apply_priority_and_extensions`` unconditionally replaces it
   with a fresh per-stream dict before the app or any middleware sees the
   Connection. Drives a real request through ``HTTP2Actor.run()`` and checks
   the handler's ``conn.extensions`` is not the sentinel by identity — once
   via a single HEADERS frame (``_on_headers_frame``'s complete-in-one-frame
   branch) and once via a HEADERS+CONTINUATION split (``_on_continuation_
   frame``'s independent call to the same replacement helper).
3. **Empty ``:method`` fallback is pinned** — ``parse_headers``'s
   ``effective_method = method or 'HEAD'`` preserves a pre-refactor
   placeholder quirk for a spec-illegal empty ``:method``, confirmed live
   (not dead code) by direct observation. Pinned so a future change to
   either side (the frame parser starting to reject it, or the fallback
   value changing) is a deliberate decision, not silent drift.
"""
import asyncio
import dataclasses
from unittest.mock import AsyncMock

import pytest
from hpack import Encoder

from blackbull.connection import Connection
from blackbull.event_aggregator import EventAggregator
from blackbull.headers import Headers
from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import FrameTypes, HeaderFrameFlags


def _dispatched_extensions(target):
    """``extensions`` of what the actor dispatched — a native ``Connection``
    (attribute) on the default lane, or an ASGI scope dict (key) under
    ``BB_FORCE_ASGI_SCOPE``. The sentinel must not escape via either shape."""
    return target.extensions if isinstance(target, Connection) else target.get('extensions')


def _dispatched_method_path(target):
    """``(method, path)`` of the dispatched target, lane-agnostically."""
    if isinstance(target, Connection):
        return target.method, target.path
    return target.get('method'), target.get('path')
from blackbull.server import parser as parser_mod
from blackbull.server.http2_actor import HTTP2Actor
from blackbull.server.recipient import AbstractReader
from blackbull.server.sender import AbstractWriter


# ---------------------------------------------------------------------------
# Guard 1 — field completeness + value parity vs the dataclass constructor
# ---------------------------------------------------------------------------

def test_builder_covers_every_connection_field_with_matching_values():
    headers = Headers([(b'host', b'example.com')])
    built = parser_mod._build_h2_connection(
        'GET', '/x', b'/x', b'q=1', headers, 'https')
    twin = Connection(
        method='GET', path='/x', raw_path=b'/x', query_string=b'q=1',
        headers=headers, http_version='2', scheme='https')

    for f in dataclasses.fields(Connection):
        # getattr alone proves completeness: an unset __slots__ attribute
        # raises AttributeError here, not silently returning None.
        built_value = getattr(built, f.name)
        twin_value = getattr(twin, f.name)
        assert built_value == twin_value, (
            f'field {f.name!r}: builder={built_value!r} != '
            f'dataclass={twin_value!r}')

    # And the identity-sensitive one explicitly: the builder intentionally
    # diverges from the twin here (shared sentinel vs a fresh {}), which is
    # exactly what Guard 2 exists to make safe.
    assert built.extensions is parser_mod._EMPTY_H2_EXTENSIONS
    assert twin.extensions is not parser_mod._EMPTY_H2_EXTENSIONS


def test_builder_output_equals_dataclass_twin():
    """Belt-and-suspenders: Connection's generated __eq__ (compare=True
    fields) agrees too — extensions is compare=True but {} == {} regardless
    of identity, so this doesn't overlap with the sentinel-identity check."""
    headers = Headers([(b'host', b'example.com')])
    built = parser_mod._build_h2_connection(
        'POST', '/y', b'/y', b'', headers, 'http')
    twin = Connection(
        method='POST', path='/y', raw_path=b'/y', query_string=b'',
        headers=headers, http_version='2', scheme='http')
    assert built == twin


# ---------------------------------------------------------------------------
# Guard 2 — the extensions sentinel never reaches app/middleware code
# ---------------------------------------------------------------------------

def _make_get_headers_frame(stream_id: int = 1) -> bytes:
    encoder = Encoder()
    block = encoder.encode([
        (b':method', b'GET'), (b':path', b'/'), (b':scheme', b'https'),
        (b':authority', b'example.com'),
    ])
    flags = HeaderFrameFlags.END_HEADERS | HeaderFrameFlags.END_STREAM
    return (len(block).to_bytes(3, 'big') + FrameTypes.HEADERS
            + bytes([flags]) + stream_id.to_bytes(4, 'big') + block)


class _Reader(AbstractReader):
    def __init__(self, data: bytes):
        self._buf = bytearray(data)

    async def read(self, n: int) -> bytes:
        chunk = bytes(self._buf[:n]); del self._buf[:n]; return chunk

    async def readuntil(self, sep: bytes) -> bytes:
        idx = self._buf.find(sep)
        if idx == -1:
            raise asyncio.IncompleteReadError(bytes(self._buf), None)
        chunk = bytes(self._buf[:idx + len(sep)]); del self._buf[:idx + len(sep)]
        return chunk

    async def readexactly(self, n: int) -> bytes:
        if len(self._buf) < n:
            raise asyncio.IncompleteReadError(bytes(self._buf), n)
        chunk = bytes(self._buf[:n]); del self._buf[:n]; return chunk


class _Writer(AbstractWriter):
    def __init__(self):
        self.written = bytearray()

    async def write(self, data: bytes) -> None:
        self.written += data


@pytest.mark.asyncio
async def test_extensions_sentinel_replaced_before_app_sees_connection():
    seen = {}

    async def capturing_app(conn, receive, send):
        seen['conn'] = conn
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})

    reader = _Reader(_make_get_headers_frame())
    writer = _Writer()
    aggregator = AsyncMock(spec=EventAggregator)
    actor = HTTP2Actor(reader, writer, capturing_app, aggregator)
    await actor.run()

    assert 'conn' in seen, 'app was never dispatched'
    assert _dispatched_extensions(seen['conn']) is not parser_mod._EMPTY_H2_EXTENSIONS, (
        'the shared empty-extensions sentinel reached application code — '
        '_apply_priority_and_extensions must replace it before dispatch')


def _make_split_get_frames(stream_id: int = 1) -> bytes:
    """A single logical GET request whose HPACK header block is split across
    a HEADERS frame (no END_HEADERS) and a CONTINUATION frame (END_HEADERS) —
    the sprint-80 follow-up: the single-frame test above only exercises
    ``_on_headers_frame``'s complete-in-one-frame branch, but
    ``_apply_priority_and_extensions`` (the thing that makes the shared
    sentinel safe) is called independently from ``_on_continuation_frame``
    too — separate code that has diverged from its HEADERS-path twin before
    (the P2/P3 access-log gating had to be applied to each individually)."""
    block = Encoder().encode([
        (b':method', b'GET'), (b':path', b'/'), (b':scheme', b'https'),
        (b':authority', b'example.com'),
    ])
    split = len(block) // 2 + 1
    part1, part2 = block[:split], block[split:]
    h_frame = (len(part1).to_bytes(3, 'big') + FrameTypes.HEADERS
              + bytes([0]) + stream_id.to_bytes(4, 'big') + part1)
    c_frame = (len(part2).to_bytes(3, 'big') + FrameTypes.CONTINUATION
              + bytes([HeaderFrameFlags.END_HEADERS])
              + stream_id.to_bytes(4, 'big') + part2)
    return h_frame + c_frame


@pytest.mark.asyncio
async def test_extensions_sentinel_replaced_before_app_sees_connection_via_continuation():
    """Same guarantee as the test above, but the header block completes via
    CONTINUATION (``_on_continuation_frame``), not a single HEADERS frame."""
    seen = {}

    async def capturing_app(conn, receive, send):
        seen['conn'] = conn
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})

    reader = _Reader(_make_split_get_frames())
    writer = _Writer()
    aggregator = AsyncMock(spec=EventAggregator)
    actor = HTTP2Actor(reader, writer, capturing_app, aggregator)
    await actor.run()

    assert 'conn' in seen, 'app was never dispatched (CONTINUATION path)'
    method, path = _dispatched_method_path(seen['conn'])
    assert method == 'GET' and path == '/'  # sanity: real request, not a stub
    assert _dispatched_extensions(seen['conn']) is not parser_mod._EMPTY_H2_EXTENSIONS, (
        'the shared empty-extensions sentinel reached application code via '
        'the CONTINUATION completion path — _apply_priority_and_extensions '
        'must replace it there too, independently of the HEADERS path')


# ---------------------------------------------------------------------------
# Guard 3 — the empty-`:method` fallback quirk is pinned, not just documented
# ---------------------------------------------------------------------------

def test_empty_method_pseudo_header_is_not_rejected_and_falls_back_to_head():
    """``parse_headers``'s ``effective_method = method or 'HEAD'``
    (blackbull/server/parser.py) deliberately preserves a pre-refactor
    placeholder quirk for a spec-illegal empty ``:method`` value, rather than
    silently changing behaviour as a side effect of the Phase 1 restructure
    (`.claude/planning/proposals/connection-alloc-hygiene.md`). Confirmed live
    (not dead code): the frame-level parser does not reject an empty
    ``:method`` the way it rejects an empty ``:path`` (see the adjacent
    ``path_pseudo == ''`` check in ``parse_headers``), so this branch is
    reachable from the wire today.

    This is itself a latent RFC 9113 §8.3.1 conformance gap — an empty
    pseudo-header value should arguably be rejected like an empty ``:path``
    is — but fixing it is a conformance decision out of scope for the alloc
    hygiene work; this test only pins today's observed behaviour so a future
    change to either side is deliberate, not accidental.
    """
    encoder = Encoder()
    block = encoder.encode([
        (b':method', b''), (b':path', b'/'), (b':scheme', b'https'),
        (b':authority', b'example.com'),
    ])
    flags = HeaderFrameFlags.END_HEADERS | HeaderFrameFlags.END_STREAM
    raw = (len(block).to_bytes(3, 'big') + FrameTypes.HEADERS
           + bytes([flags]) + (1).to_bytes(4, 'big') + block)
    frame = FrameFactory().load(raw)

    conn = parser_mod.parse_headers(frame)

    assert not frame.malformed, (
        'an empty :method is not currently rejected at the frame level — '
        'if this now fails, either the conformance gap was closed (update '
        'this test to assert frame.malformed and conn is None) or something '
        'regressed')
    assert conn is not None
    assert conn.method == 'HEAD', (
        "parse_headers's empty-:method fallback changed — this must be a "
        "deliberate decision (see the docstring), not an accidental drift")

"""Unit tests for the unified protocol registry (Sprint 50/51)."""
import pytest

from blackbull.server.protocol_registry import (
    Http1Binding, Http2Binding, ProtocolDetector, ProtocolRegistry, RawBinding,
    _HTTP2_PREFACE_FIRST_LINE,
)
from blackbull.server.recipient import AbstractReader
from blackbull.server.sender import AbstractWriter


async def _noop(reader, writer, ctx):  # pragma: no cover - never invoked here
    pass


async def _noop_app(scope, receive, send):  # pragma: no cover - never invoked
    pass


class _StubReader(AbstractReader):
    """Concrete AbstractReader so ConnectionActor's beartyped __init__ accepts
    it.  Buffer-backed (detection peeks via ``read``); records whether the
    framing reads were called so a port-bound test can assert detection never
    touched the stream."""

    def __init__(self, first_line: bytes = b''):
        self._buf = bytearray(first_line)
        self.readuntil_called = False
        self.readexactly_called = False

    async def read(self, n: int) -> bytes:
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    async def readuntil(self, sep: bytes) -> bytes:
        self.readuntil_called = True
        idx = self._buf.find(sep)
        end = (idx + len(sep)) if idx != -1 else len(self._buf)
        chunk = bytes(self._buf[:end])
        del self._buf[:end]
        return chunk

    async def readexactly(self, n: int) -> bytes:
        self.readexactly_called = True
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    def at_eof(self) -> bool:
        return not self._buf


class _StubWriter(AbstractWriter):
    async def write(self, data: bytes) -> None:
        pass


def test_builtin_http_bindings_present_and_ordered():
    r = ProtocolRegistry()
    names = [b.name for b in r.cleartext_bindings]
    # http2 (preface) must precede http1; http1 is the catch-all fallback.
    assert names == ['http2', 'http1']
    assert isinstance(r.cleartext_bindings[0], Http2Binding)
    assert isinstance(r.cleartext_bindings[-1], Http1Binding)


def test_http1_is_fallback_http2_matches_only_preface():
    r = ProtocolRegistry()
    http2, http1 = r.cleartext_bindings
    assert http2.matches_cleartext(_HTTP2_PREFACE_FIRST_LINE) is True
    assert http2.matches_cleartext(b'GET / HTTP/1.1\r\n') is False
    # http1 claims any first line.
    assert http1.matches_cleartext(b'GET / HTTP/1.1\r\n') is True
    assert http1.matches_cleartext(b'anything') is True


def test_by_alpn():
    r = ProtocolRegistry()
    assert r.by_alpn('h2').name == 'http2'
    assert r.by_alpn('http/1.1') is None
    assert r.by_alpn(None) is None


def test_claims_unified_predicate():
    """claims() is the Stage-1 selection seam: built-ins mirror
    matches_cleartext; a RawBinding claims via its detector."""
    r = ProtocolRegistry()
    http2, http1 = r.cleartext_bindings
    assert http2.claims(_HTTP2_PREFACE_FIRST_LINE, None) is True
    assert http2.claims(b'GET / HTTP/1.1\r\n', None) is False
    assert http1.claims(b'anything', None) is True

    class _FirstByte(ProtocolDetector):
        def detect(self, first_bytes: bytes, alpn) -> bool:
            return bool(first_bytes) and first_bytes[0] == 0x10
        @property
        def protocol_name(self) -> str:
            return 'fake'

    detected = RawBinding('fake', _noop, detector=_FirstByte())
    assert detected.claims(b'\x10\x00', None) is True
    assert detected.claims(b'GET', None) is False
    # A port-bound raw binding (no detector) never claims via detection.
    assert RawBinding('bound', _noop, port=9000).claims(b'\x10', None) is False


def test_register_raw_binding():
    r = ProtocolRegistry()
    binding = r.register('echo', _noop, port=9000)
    assert isinstance(binding, RawBinding)
    assert r.port_bindings == {9000: binding}
    assert r.ports == [9000]
    assert r.has_port_bindings() is True
    assert bool(r) is True
    assert 'echo' in r.raw_bindings


def test_register_duplicate_name_raises():
    r = ProtocolRegistry()
    r.register('echo', _noop, port=9000)
    with pytest.raises(ValueError, match='already registered'):
        r.register('echo', _noop, port=9001)
    # A name colliding with a built-in HTTP binding is also rejected.
    with pytest.raises(ValueError, match='already registered'):
        r.register('http1', _noop, port=9002)


def test_empty_registry_is_dormant():
    r = ProtocolRegistry()
    assert bool(r) is False
    assert r.has_port_bindings() is False
    assert r.ports == []
    assert r.port_bindings == {}


def test_raw_binding_without_port_is_not_a_port_binding():
    r = ProtocolRegistry()
    r.register('detect-only', _noop)  # no port
    assert r.ports == []
    assert r.has_port_bindings() is False
    # Still truthy: a non-HTTP protocol is registered.
    assert bool(r) is True


# ---------------------------------------------------------------------------
# ConnectionActor dispatch — bound-binding and ProtocolDetector paths (R7)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bound_binding_calls_serve_skips_detection():
    """ConnectionActor(bound_binding=…) hands the connection to the binding's
    serve without touching the reader — no byte sniffing, no ALPN check."""
    from blackbull.server.connection_actor import ConnectionActor

    reader = _StubReader()
    writer = _StubWriter()

    served = []

    async def _serve(conn):
        served.append(conn)

    binding = RawBinding('echo', _noop, port=9000)
    binding.serve = _serve

    actor = ConnectionActor(reader, writer, app=_noop_app, aggregator=None,
                            bound_binding=binding)
    await actor._dispatch()

    assert len(served) == 1
    assert reader.readexactly_called is False
    assert reader.readuntil_called is False


@pytest.mark.asyncio
async def test_protocol_detector_claims_connection_before_http1():
    """A RawBinding whose ProtocolDetector matches the first bytes takes the
    connection before the http1 fallback runs."""
    from blackbull.server.connection_actor import ConnectionActor

    class _EchoDetector(ProtocolDetector):
        def detect(self, first_bytes, alpn):
            return first_bytes.startswith(b'ECHO')

        @property
        def protocol_name(self):
            return 'echo'

    reader = _StubReader(first_line=b'ECHO hello\r\n')
    writer = _StubWriter()

    served = []

    async def _serve(conn):
        served.append(conn)

    registry = ProtocolRegistry()
    binding = registry.register('echo', _noop, detector=_EchoDetector())
    binding.serve = _serve

    actor = ConnectionActor(reader, writer, app=_noop_app, aggregator=None,
                            registry=registry)
    await actor._dispatch()

    assert len(served) == 1


@pytest.mark.asyncio
async def test_non_matching_detector_falls_through_to_http1():
    """A RawBinding whose ProtocolDetector returns False does not intercept;
    the http1 fallback still handles the connection."""
    from unittest.mock import AsyncMock, patch
    from blackbull.server.connection_actor import ConnectionActor

    class _NeverMatches(ProtocolDetector):
        def detect(self, first_bytes, alpn):
            return False

        @property
        def protocol_name(self):
            return 'none'

    reader = _StubReader(first_line=b'GET / HTTP/1.1\r\nHost: x\r\n\r\n')
    writer = _StubWriter()

    http1_served = []

    registry = ProtocolRegistry()
    binding = registry.register('none', _noop, detector=_NeverMatches())

    async def _record(conn):
        # The http1 binding now reads its own request line from the replayed
        # stream — assert it sees the intact line through the PrefixReader.
        http1_served.append(await conn.reader.readuntil(b'\r\n'))

    with patch(
        'blackbull.server.protocol_registry.Http1Binding.serve',
        new=AsyncMock(side_effect=_record),
    ):
        actor = ConnectionActor(reader, writer, app=_noop_app, aggregator=None,
                                registry=registry)
        await actor._dispatch()

    assert http1_served == [b'GET / HTTP/1.1\r\n']

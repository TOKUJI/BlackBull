"""Sprint 72 audit follow-up — protocol-uniformity contracts.

F.1b: the same authority must reach the application identically whether it
arrives as an HTTP/1.1 absolute-form request-target (RFC 9112 §3.2.2) or as
an HTTP/2 ``:authority`` pseudo-header (RFC 9113 §8.3.1 / ASGI host
mapping).  A handler reading ``scope['headers'].get(b'host')`` must not be
able to tell the transports apart.
"""
from __future__ import annotations

from hpack import Encoder

from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import FrameTypes, HeaderFrameFlags
from blackbull.server.parser import parse_headers
from blackbull.server.recipient import AbstractReader
from blackbull.server.sender import AbstractWriter


class _BufReader(AbstractReader):
    """Serves a fixed wire buffer; EOF (empty bytes) once drained."""

    def __init__(self, data: bytes) -> None:
        self._buf = bytearray(data)

    async def read(self, n: int) -> bytes:
        if not self._buf:
            return b''
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk


class _RecordingWriter(AbstractWriter):
    def __init__(self) -> None:
        self.data = bytearray()

    async def write(self, data: bytes) -> None:
        self.data += data


async def _noop_app(scope, receive, send):
    pass


def _h1_scope(raw: bytes) -> dict:
    from blackbull.server.http1_actor import HTTP1Actor
    actor = HTTP1Actor(_BufReader(b''), _RecordingWriter(),
                       app=_noop_app, aggregator=None)
    return actor._parse(raw)


def _h2_scope(fields: list[tuple[bytes, bytes]]) -> dict:
    block = Encoder().encode(fields)
    flags = HeaderFrameFlags.END_HEADERS | HeaderFrameFlags.END_STREAM
    raw = (len(block).to_bytes(3, 'big') + FrameTypes.HEADERS
           + bytes([flags]) + (1).to_bytes(4, 'big') + block)
    frame = FrameFactory().load(raw)
    scope = parse_headers(frame)
    assert not frame.malformed
    return scope


class TestH1H2HostUniformity:
    def test_h1_h2_host_uniformity(self):
        h1 = _h1_scope(b'GET http://real.example/ HTTP/1.1\r\n'
                       b'Host: spoofed.example\r\n\r\n')
        h2 = _h2_scope([(b':method', b'GET'), (b':path', b'/'),
                        (b':scheme', b'http'),
                        (b':authority', b'real.example'),
                        (b'host', b'spoofed.example')])
        assert h1.headers.get(b'host') == b'real.example'
        assert h1.headers.get(b'host') == h2['headers'].get(b'host')

    def test_h1_h2_host_uniformity_with_port(self):
        h1 = _h1_scope(b'GET / HTTP/1.1\r\nHost: example.com:8443\r\n\r\n')
        h2 = _h2_scope([(b':method', b'GET'), (b':path', b'/'),
                        (b':scheme', b'https'),
                        (b':authority', b'example.com:8443')])
        assert h1.headers.get(b'host') == b'example.com:8443'
        assert h1.headers.get(b'host') == h2['headers'].get(b'host')

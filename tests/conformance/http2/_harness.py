"""Shared H2 wire-format builders and readers for the H2 test packages.

`_make_h2_frame` / `_make_headers_frame` / `_BufferReader` were private to
`tests.conformance.http2.test_rfc9113_gaps` and imported across module
boundaries (tests/properties and tests/architecture); they live here so the
frame builders have one home.  (Other test modules' local `_make_h2_frame`
copies predate this and can migrate to it.)
"""
from __future__ import annotations

from hpack import Encoder

from blackbull.protocol.frame_types import FrameTypes, HeaderFrameFlags
from blackbull.server.recipient import AbstractReader, IncompleteReadError


def _make_h2_frame(type_byte: FrameTypes, flags: int = 0,
                   stream_id: int = 0, payload: bytes = b'') -> bytes:
    length = len(payload)
    return (length.to_bytes(3, 'big') + type_byte
            + bytes([flags]) + stream_id.to_bytes(4, 'big') + payload)


def _make_headers_frame(stream_id: int = 1, end_stream: bool = False,
                        end_headers: bool = True,
                        fields: list[tuple[bytes, bytes]] | None = None) -> bytes:
    encoder = Encoder()
    if fields is None:
        fields = [(b':method', b'GET'), (b':path', b'/'), (b':scheme', b'https'), (b':authority', b'example.com')]
    block = encoder.encode(fields)
    flags = HeaderFrameFlags.END_HEADERS if end_headers else 0
    if end_stream:
        flags |= HeaderFrameFlags.END_STREAM
    return _make_h2_frame(FrameTypes.HEADERS, flags, stream_id, block)


class _BufferReader(AbstractReader):
    """Reader that drains a byte buffer frame by frame."""
    def __init__(self, data: bytes):
        self._buf = bytearray(data)

    async def read(self, n: int) -> bytes:
        if not self._buf:
            return b''
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    async def readuntil(self, sep: bytes) -> bytes:
        result = bytearray()
        while True:
            if not self._buf:
                raise IncompleteReadError()
            result.append(self._buf[0])
            del self._buf[:1]
            if bytes(result).endswith(sep):
                return bytes(result)

    async def readexactly(self, n: int) -> bytes:
        if len(self._buf) < n:
            raise IncompleteReadError()
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

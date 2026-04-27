"""HTTP/1.1 client (RFC 7230).

Provides ``HTTP1Client`` plus the lower-level ``HTTP1RequestSender`` /
``HTTP1ResponseRecipient`` helpers that frame and unframe HTTP/1.1 messages
on the wire.

Symmetric with the server-side ``HTTP1Sender`` / ``HTTP1Recipient`` in
:mod:`blackbull.server.sender` and :mod:`blackbull.server.recipient`,
but reversed: the client *writes* request lines + request headers + request
body, and *reads* status lines + response headers + response body.
"""
import asyncio
import ssl as _ssl
from collections.abc import AsyncIterable, AsyncIterator
from http import HTTPMethod
from typing import Union

import logging
from ..server.headers import Headers, HeaderList
from ..server.recipient import (AbstractReader, AsyncioReader,
                                IncompleteReadError)
from ..server.sender import AbstractWriter, AsyncioWriter
from .exceptions import ConnectionError, ProtocolError
from .http2 import ClientResponse  # shared dataclass

logger = logging.getLogger(__name__)


# Type for request bodies — either a complete byte string or an async iterable
# of byte chunks (the streaming case, encoded as Transfer-Encoding: chunked).
RequestBody = Union[bytes, bytearray, memoryview, AsyncIterable[bytes]]

# CRLF as used throughout RFC 7230.
_CRLF = b'\r\n'


class HTTP1RequestSender:
    """Writes an HTTP/1.1 request — request line, headers, body — to an ``AbstractWriter``.

    Adds ``Content-Length`` automatically for fixed-size byte bodies; switches
    to ``Transfer-Encoding: chunked`` for ``AsyncIterable`` bodies.  The
    ``Host`` header MUST be present (RFC 7230 §5.4) — the helper raises
    ``ProtocolError`` if it is not.
    """

    def __init__(self, writer: AbstractWriter) -> None:
        self._writer = writer

    async def send(self, method: str | HTTPMethod, path: str,
                   headers: Headers, body: RequestBody = b'') -> None:
        if b'host' not in headers:
            raise ProtocolError('HTTP/1.1 request requires a Host header')

        if isinstance(body, (bytes, bytearray, memoryview)):
            await self._send_fixed(str(method), path, headers, bytes(body))
        else:
            await self._send_chunked(str(method), path, headers, body)

    async def _send_fixed(self, method: str, path: str, headers: Headers,
                          body: bytes) -> None:
        if body and b'content-length' not in headers:
            headers.append(b'content-length', str(len(body)).encode())
        await self._write_start(method, path, headers)
        if body:
            await self._writer.write(body)

    async def _send_chunked(self, method: str, path: str, headers: Headers,
                            body: AsyncIterable[bytes]) -> None:
        if b'transfer-encoding' not in headers:
            headers.append(b'transfer-encoding', b'chunked')
        await self._write_start(method, path, headers)
        async for chunk in body:
            if chunk:
                await self._writer.write(
                    f'{len(chunk):x}'.encode() + _CRLF + chunk + _CRLF)
        await self._writer.write(b'0' + _CRLF + _CRLF)

    async def _write_start(self, method: str, path: str, headers: Headers) -> None:
        chunks: list[bytes] = [f'{method} {path} HTTP/1.1'.encode() + _CRLF]
        for k, v in headers:
            chunks.append(k + b': ' + v + _CRLF)
        chunks.append(_CRLF)
        await self._writer.write(b''.join(chunks))


class HTTP1ResponseRecipient:
    """Reads an HTTP/1.1 response from an ``AbstractReader``.

    Decodes both ``Content-Length``-bound and ``Transfer-Encoding: chunked``
    bodies.  Returns a ``ClientResponse``; ``stream()`` returns an async
    iterator of body chunks instead, so large responses don't have to fit
    in memory.
    """

    async def receive(self, reader: AbstractReader) -> ClientResponse:
        status, headers = await self._read_start(reader)
        body = await self._read_body(reader, headers)
        return ClientResponse(status=status, headers=headers, body=body)

    async def stream(self, reader: AbstractReader) -> AsyncIterator[bytes]:
        # Body-only streaming: callers that need status/headers should use
        # ``receive``.  Yielding the start-line as the first item would force
        # callers to special-case the iterator's first element.
        _, headers = await self._read_start(reader)
        async for chunk in self._stream_body(reader, headers):
            yield chunk

    async def _read_start(self, reader: AbstractReader) -> tuple[int, Headers]:
        try:
            status_line = await reader.readuntil(_CRLF)
        except IncompleteReadError as exc:
            raise ConnectionError('connection closed before response') from exc
        # "HTTP/1.1 200 OK\r\n" — split into version, status, reason.
        parts = status_line.rstrip(_CRLF).split(b' ', 2)
        if len(parts) < 2:
            raise ProtocolError(f'malformed status line: {status_line!r}')
        try:
            status = int(parts[1])
        except ValueError as exc:
            raise ProtocolError(f'invalid status code: {parts[1]!r}') from exc

        pairs: list[tuple[bytes, bytes]] = []
        while True:
            line = await reader.readuntil(_CRLF)
            if line == _CRLF:
                break
            name, _, value = line.rstrip(_CRLF).partition(b':')
            pairs.append((name.strip().lower(), value.strip()))
        return status, Headers(pairs)

    async def _read_body(self, reader: AbstractReader, headers: Headers) -> bytes:
        te = headers.get(b'transfer-encoding', b'').lower()
        if te == b'chunked':
            return b''.join([c async for c in self._read_chunked(reader)])
        cl_raw = headers.get(b'content-length')
        if cl_raw is not None:
            return await reader.readexactly(int(cl_raw))
        # No Content-Length and no chunked: caller (e.g. HEAD, 204, 304) must
        # interpret as empty body.  We don't read-until-EOF here because that
        # would break keep-alive.
        return b''

    async def _stream_body(self, reader: AbstractReader,
                           headers: Headers) -> AsyncIterator[bytes]:
        te = headers.get(b'transfer-encoding', b'').lower()
        if te == b'chunked':
            async for chunk in self._read_chunked(reader):
                yield chunk
            return
        cl_raw = headers.get(b'content-length')
        if cl_raw is not None:
            yield await reader.readexactly(int(cl_raw))

    async def _read_chunked(self, reader: AbstractReader) -> AsyncIterator[bytes]:
        while True:
            size_line = await reader.readuntil(_CRLF)
            try:
                size = int(size_line.rstrip(_CRLF).split(b';', 1)[0], 16)
            except ValueError as exc:
                raise ProtocolError(f'invalid chunk size: {size_line!r}') from exc
            if size == 0:
                # Read trailing CRLF after the terminator chunk; ignore any
                # trailers (RFC 7230 §4.1.2) — they are rarely used.
                await reader.readuntil(_CRLF)
                return
            chunk = await reader.readexactly(size)
            await reader.readuntil(_CRLF)  # consume CRLF after chunk data
            yield chunk


class HTTP1Client:
    """Async HTTP/1.1 client.

    Use as an async context manager::

        async with HTTP1Client('localhost', 8000) as c:
            res = await c.request(HTTPMethod.GET, '/path')

    The connection persists across multiple ``request()`` calls (HTTP/1.1
    persistent connections, RFC 7230 §6.3) until ``__aexit__`` closes it.
    Pass ``ssl=`` to use TLS.

    The ``Host`` header is injected automatically when the caller omits it.
    """

    def __init__(self, host: str, port: int, *,
                 ssl: _ssl.SSLContext | None = None) -> None:
        self._host = host
        self._port = port
        self._ssl = ssl
        self._reader: AbstractReader | None = None
        self._writer: AbstractWriter | None = None
        self._raw_writer: asyncio.StreamWriter | None = None

    # ---- async context manager -------------------------------------------

    async def __aenter__(self) -> 'HTTP1Client':
        if self._raw_writer is None:
            r, w = await asyncio.open_connection(self._host, self._port, ssl=self._ssl)
            self._raw_writer = w
            self._reader = AsyncioReader(r)
            self._writer = AsyncioWriter(w)
        await self._start()
        return self

    @classmethod
    def _adopt(cls, host: str, port: int,
               reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
               *, ssl: _ssl.SSLContext | None = None) -> 'HTTP1Client':
        """Wrap an already-open ``(reader, writer)`` pair as an HTTP1Client.

        Used by ``Client`` (the ALPN dispatcher) to hand off a TLS-handshaken
        connection without re-opening the transport.
        """
        c = cls(host, port, ssl=ssl)
        c._raw_writer = writer
        c._reader = AsyncioReader(reader)
        c._writer = AsyncioWriter(writer)
        return c

    async def _start(self) -> None:
        """Per-protocol post-connect initialisation hook.

        HTTP/1.1 has no preface or persistent loop, so this is a no-op; it
        exists for API symmetry with ``HTTP2Client._start``.
        """
        return None

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._raw_writer is not None:
            try:
                self._raw_writer.close()
                await self._raw_writer.wait_closed()
            except Exception:
                pass

    # ---- public API ------------------------------------------------------

    async def request(self, method: str | HTTPMethod, path: str, *,
                      headers: HeaderList = (),
                      body: RequestBody = b'') -> ClientResponse:
        assert self._writer is not None and self._reader is not None
        h = self._headers_with_host(headers)
        await HTTP1RequestSender(self._writer).send(method, path, h, body)
        return await HTTP1ResponseRecipient().receive(self._reader)

    async def stream(self, method: str | HTTPMethod, path: str, *,
                     headers: HeaderList = (),
                     body: RequestBody = b'') -> AsyncIterator[bytes]:
        """Send a request and yield body chunks lazily.

        Unlike ``request()`` this does not buffer the response body, so
        gigabyte-sized responses do not need to fit in memory.  Status and
        headers are not exposed by this method; use ``request()`` if you
        need them.
        """
        assert self._writer is not None and self._reader is not None
        h = self._headers_with_host(headers)
        await HTTP1RequestSender(self._writer).send(method, path, h, body)
        recipient = HTTP1ResponseRecipient()
        async for chunk in recipient.stream(self._reader):
            yield chunk

    def _headers_with_host(self, headers: HeaderList) -> Headers:
        h = Headers(list(headers))
        if b'host' not in h:
            h.append(b'host', f'{self._host}:{self._port}'.encode())
        return h

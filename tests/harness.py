"""Raw bytes to BlackBull's real server over loopback, and the bytes it answers."""
from __future__ import annotations

import asyncio
import pathlib
import ssl
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from blackbull import BlackBull
from blackbull.server import Server
from blackbull.server.connection_protocol import ConnectionProtocol
from blackbull.server.listener import Listener, Tcp

TIMEOUT = 5.0
_HERE = pathlib.Path(__file__).parent


@dataclass
class Response:
    status: int
    headers: list[tuple[bytes, bytes]]
    body: bytes = b''
    trailers: list[tuple[bytes, bytes]] = field(default_factory=list)

    def header(self, name: bytes) -> bytes | None:
        """The first value of *name* (lowercase), or None."""
        return next((v for n, v in self.headers if n == name), None)

    def all(self, name: bytes) -> list[bytes]:
        return [v for n, v in self.headers if n == name]


def _fields(block: bytes) -> list[tuple[bytes, bytes]]:
    out = []
    for line in block.split(b'\r\n'):
        if line:
            name, _, value = line.partition(b':')
            out.append((name.lower(), value.strip(b' \t')))
    return out


async def read_h2_frame(reader: asyncio.StreamReader) -> tuple[int, int, int, bytes]:
    async with asyncio.timeout(TIMEOUT):
        head = await reader.readexactly(9)
        payload = await reader.readexactly(int.from_bytes(head[:3], 'big'))
    return head[3], head[4], int.from_bytes(head[5:], 'big') & 0x7fffffff, payload


class Wire:
    """One client connection: send anything, read what comes back."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer

    async def send(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    def half_close(self) -> None:
        self.writer.write_eof()

    async def recv_until(self, sep: bytes = b'\r\n\r\n') -> bytes:
        return await asyncio.wait_for(self.reader.readuntil(sep), TIMEOUT)

    async def recv_exactly(self, n: int) -> bytes:
        return await asyncio.wait_for(self.reader.readexactly(n), TIMEOUT)

    async def recv_all(self) -> bytes:
        """Everything until the server closes."""
        return await asyncio.wait_for(self.reader.read(), TIMEOUT)

    async def closed(self) -> bool:
        """True once the server has closed (EOF or reset), without data first."""
        try:
            return await asyncio.wait_for(self.reader.read(1), TIMEOUT) == b''
        except ConnectionError:
            return True

    async def response(self, *, head: bool = False) -> Response:
        """Read one HTTP/1.1 response; interim 1xx responses are skipped."""
        while True:
            status_line, _, block = (await self.recv_until()).partition(b'\r\n')
            status = int(status_line.split(b' ', 2)[1])
            resp = Response(status, _fields(block))
            if status >= 200 or status == 101:
                break
        if head or status in (101, 204, 304):
            return resp
        if resp.header(b'transfer-encoding') == b'chunked':
            body = bytearray()
            while size := int((await self.recv_until(b'\r\n')).split(b';')[0], 16):
                body += (await self.recv_exactly(size + 2))[:-2]
            trailer = bytearray()
            while (line := await self.recv_until(b'\r\n')) != b'\r\n':
                trailer += line
            resp.body, resp.trailers = bytes(body), _fields(bytes(trailer))
        elif (length := resp.header(b'content-length')) is not None:
            resp.body = await self.recv_exactly(int(length))
        else:
            resp.body = await self.recv_all()
        return resp

    async def aclose(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except ConnectionError:
            pass


def _http_only(app):
    async def wrapped(conn, receive, send):
        if not (isinstance(conn, dict) and conn.get('type') == 'lifespan'):
            await app(conn, receive, send)
    return wrapped


class Served:
    """A running server; ``wire()`` opens a raw connection to it."""

    def __init__(self, server: Server, tls: bool):
        self.server = server
        self.tls = tls
        self.wires: list[Wire] = []

    async def wire(self, *, alpn: list[str] | None = None) -> Wire:
        ctx = None
        if self.tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
            ctx.set_alpn_protocols(alpn or ['http/1.1'])
        w = Wire(*await asyncio.open_connection('127.0.0.1', self.server.port, ssl=ctx))
        self.wires.append(w)
        return w


@asynccontextmanager
async def served(app, *, tls: bool = False, **server_kwargs):
    """Serve *app* through ``Server.run`` on a loopback port for the block.

    A plain ASGI callable never sees the lifespan scope.
    """
    if not isinstance(app, BlackBull):
        app = _http_only(app)
    ctx = None
    if tls:
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.set_alpn_protocols(['h2', 'http/1.1'])
        ctx.load_cert_chain(_HERE / 'cert.pem', _HERE / 'key.pem')
    server = Server(app, listeners=[Listener(Tcp(0, '127.0.0.1'), tls=ctx)],
                    **server_kwargs)
    server.open_socket()
    running = asyncio.create_task(server.run())
    s = Served(server, tls)
    try:
        yield s
    finally:
        for w in s.wires:
            await w.aclose()
        await server.stop(drain_timeout=TIMEOUT)
        await running


async def exchange(app, *requests: bytes, **server_kwargs) -> list[Response]:
    """Send each request on one connection and read one response for each."""
    async with served(app, **server_kwargs) as server:
        w = await server.wire()
        out = []
        for raw in requests:
            await w.send(raw)
            out.append(await w.response(head=raw.startswith(b'HEAD ')))
        return out


@asynccontextmanager
async def peer(handler):
    """A loopback origin whose every connection runs *handler(reader, writer)*.

    Yields the port.  The block ends once each handler has seen its client go.
    """
    async def serve(reader, writer):
        try:
            await handler(reader, writer)
        finally:
            # Server.wait_closed waits for every connection (3.12+).
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    server = await asyncio.start_server(serve, '127.0.0.1', 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await asyncio.wait_for(server.wait_closed(), TIMEOUT)


def feed(proto: ConnectionProtocol, data: bytes) -> None:
    """Deliver *data* the way a transport does."""
    while data:
        buf = proto.get_buffer(len(data))
        n = min(len(buf), len(data))
        buf[:n] = data[:n]
        proto.buffer_updated(n)
        data = data[n:]


def fed(*chunks: bytes, eof: bool = True) -> ConnectionProtocol:
    """A connection with no socket, already holding *chunks*; its ``.reader`` is the production reader."""
    proto = ConnectionProtocol()
    for chunk in chunks:
        feed(proto, chunk)
    if eof:
        proto.eof_received()
    return proto


class _OnDemand(ConnectionProtocol):
    def __init__(self, script, pause) -> None:
        super().__init__()
        self.script, self.pause = iter(script), pause

    async def wait_for_arrival(self) -> None:
        step = next(self.script, None)
        if step is None:
            self.eof_received()
            return
        await self.pause(step[0])
        feed(self, step[1])


def paced(script, pause=asyncio.sleep):
    """A production reader whose peer sends the next ``(gap, data)`` of the
    *script* iterable each time the reader waits, after ``await pause(gap)``;
    EOF once it is exhausted."""
    return _OnDemand(script, pause).reader

"""A TCP proxy that delivers legal bytes in hostile shapes.

Put between BlackBull's client and a server, it exercises the server with a
real HPACK and stream stack under transport faults no scripted scenario
reaches: every octet a separate write, writes held back and coalesced, or a
connection reset part-way through the request.
"""
from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass

_PREFACE_LENGTH = 24
_HEADERS = 0x1


@dataclass(frozen=True)
class Perturbation:
    name: str
    #: Forward each octet as its own write, in both directions.
    byte_per_write: bool = False
    #: Hold client bytes this long, then forward them as one write.
    coalesce_s: float = 0.0
    #: Reset both sides part-way through the client's first HEADERS frame.
    reset_in_first_headers: bool = False


PERTURBATIONS = (
    Perturbation('byte_per_write', byte_per_write=True),
    Perturbation('coalesced_writes', coalesce_s=0.05),
    Perturbation('reset_mid_request', reset_in_first_headers=True),
)


def first_headers_cut(stream: bytes) -> int | None:
    """Where half of the first HEADERS frame's payload has been sent; None until known."""
    i = _PREFACE_LENGTH
    while i + 9 <= len(stream):
        length = int.from_bytes(stream[i:i + 3], 'big')
        if stream[i + 3] == _HEADERS:
            return i + 9 + max(1, length // 2)
        i += 9 + length
    return None


class PerturbingProxy:
    """``async with PerturbingProxy(port, perturbation) as proxy``: dial ``proxy.port``."""

    def __init__(self, upstream_port: int, perturbation: Perturbation) -> None:
        self.upstream_port = upstream_port
        self.perturbation = perturbation
        self.writes_upstream = 0

    async def __aenter__(self) -> PerturbingProxy:
        self.server = await asyncio.start_server(self._serve, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        self.server.close()
        await self.server.wait_closed()

    async def _serve(self, client_r, client_w) -> None:
        try:
            up_r, up_w = await asyncio.open_connection('127.0.0.1', self.upstream_port)
        except OSError:
            client_w.transport.abort()
            return
        for writer in (client_w, up_w):
            # Without it the kernel coalesces the one-octet writes again.
            writer.transport.get_extra_info('socket').setsockopt(
                socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        pumps = (asyncio.ensure_future(self._pump(client_r, up_w, upstream=True,
                                                  peer=client_w)),
                 asyncio.ensure_future(self._pump(up_r, client_w, upstream=False, peer=up_w)))
        await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        for pump in pumps:
            pump.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)
        for writer in (client_w, up_w):
            writer.transport.abort()

    async def _pump(self, reader, writer, *, upstream: bool, peer) -> None:
        p, forwarded = self.perturbation, bytearray()
        while True:
            data = await reader.read(65536)
            if not data:
                return
            if upstream and p.coalesce_s:
                await asyncio.sleep(p.coalesce_s)
                while (more := await _read_ready(reader)):
                    data += more
            if upstream and p.reset_in_first_headers:
                cut = first_headers_cut(bytes(forwarded + data))
                if cut is not None and cut <= len(forwarded) + len(data):
                    writer.write(data[:cut - len(forwarded)])
                    await writer.drain()
                    writer.transport.abort()
                    peer.transport.abort()
                    return
            chunks = [data[i:i + 1] for i in range(len(data))] if p.byte_per_write else [data]
            for chunk in chunks:
                writer.write(chunk)
                await writer.drain()
                if upstream:
                    self.writes_upstream += 1
                if p.byte_per_write:
                    await asyncio.sleep(0)
            forwarded += data


async def _read_ready(reader) -> bytes:
    """Bytes already buffered or arriving at once; empty when none come."""
    try:
        return await asyncio.wait_for(reader.read(65536), 0.001)
    except TimeoutError:
        return b''

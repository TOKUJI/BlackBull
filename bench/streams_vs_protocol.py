"""Isolate the asyncio-streams layer from the loop.

Two minimal HTTP/1.1 responders, identical wire behaviour, differing only in
how bytes reach them: ``asyncio.start_server`` (StreamReader/StreamWriter) vs a
bare ``asyncio.Protocol``.  Neither parses beyond locating the head, so what is
left between the two numbers is the streams layer alone -- the thing uvloop
does *not* replace, and the thing Sanic does not have.

    python streams_vs_protocol.py streams|protocol [port]
"""
import asyncio
import sys

RESP = (b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n'
        b'Content-Length: 4\r\nConnection: keep-alive\r\n\r\npong')
END = b'\r\n\r\n'


async def _streams(port: int) -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                await reader.readuntil(END)
                writer.write(RESP)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()
    server = await asyncio.start_server(handle, '127.0.0.1', port)
    async with server:
        await server.serve_forever()


class _Proto(asyncio.Protocol):
    __slots__ = ('_transport', '_buf')

    def connection_made(self, transport) -> None:
        transport.set_write_buffer_limits(0)
        self._transport = transport
        self._buf = b''

    def data_received(self, data: bytes) -> None:
        buf = self._buf + data if self._buf else data
        n = 0
        while True:
            i = buf.find(END, n)
            if i < 0:
                break
            n = i + 4
        if n:
            self._buf = buf[n:]
            # one write per read, however many heads it carried
            self._transport.write(RESP * (n // 1 and buf.count(END, 0, n)))
        else:
            self._buf = buf


async def _protocol(port: int) -> None:
    loop = asyncio.get_running_loop()
    server = await loop.create_server(_Proto, '127.0.0.1', port)
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    mode = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8899
    from blackbull.env import apply_event_loop_policy
    apply_event_loop_policy()
    asyncio.run((_streams if mode == 'streams' else _protocol)(port))

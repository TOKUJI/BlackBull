"""A/B the two H/1.1 read front ends by *transport reads per request*.

Run: ``uv run python bench/h1_frontend_ab.py``

Deterministic and machine-independent: it counts trips to the transport rather
than timing a local throughput number that would mostly measure the laptop.
Each underlying ``read``/``readuntil`` is one trip, i.e. at minimum one chance
for the loop to suspend, and fewer of them is the whole claim behind
``BB_H1_PROTOCOL``.

What it established (Sprint 84): the per-line header loop runs only for the
*first* request on a connection — ``run()`` already reads each subsequent head
with a single ``readuntil(b'\r\n\r\n')`` — so the buffered front end wins on
pipelined arrival (~1.1 → ~0.04 trips/req) and not on serialized keep-alive
(~1.1 → ~1.02), which is the shape the baseline profile actually has.

Caveat when reading the output: the fake reader's ``readuntil`` searches its
whole byte string instead of respecting the simulated chunk boundary, so the
*streams* rows are identical across both arrival shapes.  The buffered rows are
faithful; the claim about where the per-line loop runs is read off the code.
"""
import asyncio
import os

N = 50  # pipelined requests on one connection

REQ = (b'GET /baseline11?a=13&b=42 HTTP/1.1\r\n'
       b'Host: localhost\r\n'
       b'User-Agent: probe/1.0\r\n'
       b'Accept: */*\r\n'
       b'\r\n')


class CountingReader:
    """Serves a fixed byte stream, counting every transport touch."""

    def __init__(self, data: bytes, chunk: int = 65536):
        self._d = data
        self._pos = 0
        self._chunk = chunk
        self.reads = 0
        self.readuntils = 0

    async def read(self, n: int = -1) -> bytes:
        self.reads += 1
        if self._pos >= len(self._d):
            return b''
        n = self._chunk if n < 0 else min(n, self._chunk)
        out = self._d[self._pos:self._pos + n]
        self._pos += len(out)
        return out

    async def readuntil(self, sep: bytes = b'\n') -> bytes:
        self.readuntils += 1
        idx = self._d.find(sep, self._pos)
        if idx == -1:
            from blackbull.server.recipient import IncompleteReadError
            rest, self._pos = self._d[self._pos:], len(self._d)
            raise IncompleteReadError(rest)
        end = idx + len(sep)
        out = self._d[self._pos:end]
        self._pos = end
        return out

    async def readexactly(self, n: int) -> bytes:
        from blackbull.server.recipient import IncompleteReadError
        out = self._d[self._pos:self._pos + n]
        if len(out) < n:
            self._pos = len(self._d)
            raise IncompleteReadError(out)
        self._pos += n
        return out


class NullWriter:
    def write(self, data): pass
    async def drain(self): pass
    def close(self): pass
    async def wait_closed(self): pass
    def get_extra_info(self, name, default=None): return default
    def is_closing(self): return False
    def can_write_eof(self): return False


async def run_arm(flag: str, chunk: int = 65536) -> tuple[int, int, int]:
    os.environ['BB_H1_PROTOCOL'] = flag
    from blackbull import env
    env.get_settings.cache_clear() if hasattr(env.get_settings, 'cache_clear') else None
    from blackbull.server.http1_actor import HTTP1Actor

    calls = 0

    async def app(conn, receive, send):
        nonlocal calls
        calls += 1
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'text/plain')]})
        await send({'type': 'http.response.body', 'body': b'ok'})

    reader = CountingReader(REQ * N, chunk)
    actor = HTTP1Actor(reader, NullWriter(), app, None)
    await actor.run()
    return calls, reader.reads, reader.readuntils


async def main():
    # chunk = how many bytes the transport hands over per read.  65536 models
    # a pipelined client whose batch lands in one segment; len(REQ) models a
    # serialized keep-alive client, one request per delivery.
    for chunk, shape in ((65536, 'pipelined (batch in one segment)'),
                         (len(REQ), 'serialized keep-alive')):
        print(f'--- {shape} ---')
        for flag, label in (('0', 'streams (default)'), ('1', 'BB_H1_PROTOCOL=1')):
            calls, reads, readuntils = await run_arm(flag, chunk)
            total = reads + readuntils
            print(f'  {label:22} requests={calls:3d}  read={reads:4d}  '
                  f'readuntil={readuntils:4d}  total={total:4d}  '
                  f'per-request={total / max(calls, 1):.2f}')


asyncio.run(main())

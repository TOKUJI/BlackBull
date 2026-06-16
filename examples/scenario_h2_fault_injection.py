"""Drive an HTTP/2 client through one of the canned misbehaviour scenarios.

This example shows the public API for testing your HTTP/2 client (or
proxy, or middleware) against a deliberately misbehaving server.  It
walks through every catalogue entry, prints what the server emitted,
and reports whether your client handled it gracefully.

Run::

    python examples/scenario_h2_fault_injection.py

The synthetic client below speaks just enough HTTP/2 to send a preface
+ initial SETTINGS so the server's scenario can run end-to-end.  A
real test would substitute httpx / h2 / hyper-h2 / your own client.
"""
from __future__ import annotations

import asyncio

from blackbull.fault_injection import CLIENT_PREFACE, H2FaultServer
from blackbull.fault_injection.catalogue import CATALOGUE


CLIENT_SETTINGS_FRAME = b'\x00\x00\x00\x04\x00\x00\x00\x00\x00'
# Minimal client HEADERS frame on stream 1, flagged END_STREAM +
# END_HEADERS, payload is HPACK static-table :status 200 (1 byte).
# Catalogue scenarios that block on WaitForClientFrame look for
# this frame to advance.
CLIENT_HEADERS_S1 = (
    b'\x00\x00\x01'         # length 1
    b'\x01'                  # type HEADERS
    b'\x05'                  # flags END_STREAM | END_HEADERS
    b'\x00\x00\x00\x01'      # stream id 1
    b'\x88'                  # payload (HPACK index 8 = :status 200)
)


async def synthetic_client(url: str, *, read_timeout: float = 3.0) -> bytes:
    """Open the connection, send preface + SETTINGS + HEADERS, read until EOF."""
    host, port = url.split('//')[1].rstrip('/').split(':')
    reader, writer = await asyncio.open_connection(host, int(port))
    try:
        writer.write(CLIENT_PREFACE)
        writer.write(CLIENT_SETTINGS_FRAME)
        writer.write(CLIENT_HEADERS_S1)
        await writer.drain()
        try:
            return await asyncio.wait_for(reader.read(8192),
                                          timeout=read_timeout)
        except asyncio.TimeoutError:
            return b''
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def run_one(name: str) -> None:
    scenario = CATALOGUE[name]()
    async with H2FaultServer(scenario=scenario) as server:
        print(f'--- {name} ---')
        print(f'  bound at:        {server.url}')
        wire = await synthetic_client(server.url)
        await server.wait_for_connection_done(timeout=10.0)
        result = server.last_result
        print(f'  server emitted:  {len(wire)} bytes')
        print(f'  steps completed: {result.steps_completed}')
        print(f'  wait timed out:  {result.wait_timed_out}')
        print(f'  terminated:      {result.terminated}')
        print(f'  elapsed:         {result.elapsed_s:.3f}s')


async def main() -> None:
    print(f'Running {len(CATALOGUE)} catalogue scenarios.\n')
    for name in CATALOGUE:
        await run_one(name)
        print()


if __name__ == '__main__':
    asyncio.run(main())

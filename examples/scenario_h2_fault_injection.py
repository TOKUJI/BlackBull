"""Drive a real HTTP/2 client (httpx) through each catalogue scenario.

This example shows the public API for testing your HTTP/2 client (or
proxy, or middleware) against a deliberately misbehaving server.  Each
named catalogue entry is paired with a fresh ``H2FaultServer`` bound
to a random localhost port over TLS (ALPN ``h2``); ``httpx`` connects
to it as a real-world client would, and the example prints both what
the server emitted on the wire and how httpx reacted.

Run::

    pip install 'httpx[http2]'
    python examples/scenario_h2_fault_injection.py

httpx only negotiates HTTP/2 via ALPN over TLS, which is why this
example spins up a self-signed TLS context — over plain ``http://``
the client would silently downgrade to HTTP/1.1 and never speak the
H/2 preface the fault server expects.  See
``docs/guide/fault_injection.md`` for a tutorial.
"""
from __future__ import annotations

import asyncio

import httpx

from blackbull.fault_injection import H2FaultServer, make_self_signed_h2_context
from blackbull.fault_injection.catalogue import CATALOGUE


PER_REQUEST_TIMEOUT_S = 4.0


async def drive_one(name: str) -> None:
    scenario = CATALOGUE[name]()
    ssl_ctx = make_self_signed_h2_context()
    async with H2FaultServer(scenario=scenario, ssl_context=ssl_ctx) as server:
        assert server.url is not None  # set by __aenter__
        print(f'--- {name} ---')
        print(f'  bound at:        {server.url}')

        async with httpx.AsyncClient(
            http2=True, verify=False, timeout=PER_REQUEST_TIMEOUT_S,
        ) as client:
            try:
                resp = await client.get(server.url)
                print(f'  client outcome:  HTTP {resp.status_code} '
                      f'(version {resp.http_version})')
            except httpx.HTTPError as exc:
                print(f'  client outcome:  {type(exc).__name__}: '
                      f'{str(exc) or "<no message>"}')

        await server.wait_for_connection_done(timeout=10.0)
        result = server.last_result
        assert result is not None  # set after wait_for_connection_done
        print(f'  server bytes:    {result.server_bytes_sent} sent / '
              f'{result.client_bytes_received} received')
        print(f'  steps completed: {result.steps_completed}')
        print(f'  wait timed out:  {result.wait_timed_out}')
        print(f'  terminated:      {result.terminated}')
        print(f'  elapsed:         {result.elapsed_s:.3f}s')


async def main() -> None:
    print(f'Running {len(CATALOGUE)} catalogue scenarios against httpx.\n')
    for name in CATALOGUE:
        await drive_one(name)
        print()


if __name__ == '__main__':
    asyncio.run(main())

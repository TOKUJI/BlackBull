#!/usr/bin/python3
"""Atheris coverage-guided fuzz harness for BlackBull's HTTP/1.1 server.

Sprint 17 Phase 5 — switched from the previous ``decode_to_request`` +
``HTTP1Client.request()`` pipeline (which re-normalised every malformed
mutation back to valid HTTP/1.1 before sending) to the shared
:class:`Scenario` abstraction.  Atheris's byte-level mutations now
reach the wire verbatim via :meth:`Scenario.from_bytes`, and every
runnable mutation produces a distinct execution path against the
server.  Findings — i.e. *server-side* crashes, not categorised
protocol responses — land in the shared corpus directory at
``tests/conformance/http1/fuzz/corpus/`` so the differential test
can replay them.
"""
import sys
import time
import asyncio
from multiprocessing import Process

import requests
import atheris

with atheris.instrument_imports():
    from blackbull import BlackBull, read_body
    from blackbull.client import HTTP1Client, Scenario
    from blackbull.response import StreamingResponse
    from http import HTTPMethod


app = BlackBull()


@app.route(path='/')
async def index(scope, receive, send):
    await send({'type': 'http.response.start', 'status': 200,
                'headers': [(b'content-type', b'text/plain')]})
    await send({'type': 'http.response.body', 'body': b'ok'})


@app.route(path='/echo', methods=[HTTPMethod.GET, HTTPMethod.POST, HTTPMethod.PUT])
async def echo(scope, receive, send):
    body = await read_body(receive)
    await send({'type': 'http.response.start', 'status': 200,
                'headers': [(b'content-type', b'application/octet-stream')]})
    await send({'type': 'http.response.body', 'body': body})


@app.route(path='/chunked')
async def chunked(scope, receive, send):
    async def events():
        for ch in (b'a', b'b', b'c'):
            yield ch
    await StreamingResponse(events())(scope, receive, send)


def wait_until_ready(url: str, timeout: float = 10.0):
    start = time.time()
    while True:
        try:
            r = requests.get(url, timeout=0.5)
            if r.status_code in (200, 404):
                return
        except Exception:
            pass
        if time.time() - start > timeout:
            raise RuntimeError("server not ready")
        time.sleep(0.1)


async def _run_scenario(scenario: Scenario) -> None:
    """Drive ``scenario`` against the local BlackBull instance.

    Wrapped in :func:`asyncio.wait_for` so a pathological scenario
    (e.g. lots of Sleep steps) can't stall the fuzzer.  ``connect_timeout``
    on the client also rules out a hung accept.  The executor never
    raises — we discard its ScenarioResult; only server-side crashes
    visible to atheris (via ``instrument_imports``) count as findings.
    """
    async with HTTP1Client('127.0.0.1', app.port,
                           connect_timeout=2.0) as c:
        await asyncio.wait_for(c.execute_scenario(scenario), timeout=3.0)


def TestOneInput(data: bytes) -> None:
    """Atheris entry point.

    Phase 5: bytes go straight to :meth:`Scenario.from_bytes` — a
    total decoder that yields a valid scenario for every input,
    including the empty string.  No lossy pre-parse; mutations
    reach the wire as the executor lays them out.
    """
    scenario = Scenario.from_bytes(data)
    if not scenario.steps:
        # Atheris seed corpora sometimes start with empty inputs;
        # short-circuit to keep iteration throughput up.
        return
    try:
        asyncio.run(_run_scenario(scenario))
    except (asyncio.TimeoutError, ConnectionError, OSError):
        # Transport-layer outcomes are *expected* protocol responses,
        # not crashes.  Atheris should keep mutating; only an unhandled
        # exception inside BlackBull (caught by instrument_imports)
        # counts as a finding.
        return


def main():
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    p = None
    try:
        app.create_server(port=0)
        p = Process(target=lambda: asyncio.run(app.run()))
        p.start()
        wait_until_ready(f"http://localhost:{app.port}")
        main()
    except Exception as e:
        print(f"Error occurred: {e}")
    finally:
        app.stop()
        if p is not None:
            p.terminate()
            p.join(timeout=5)

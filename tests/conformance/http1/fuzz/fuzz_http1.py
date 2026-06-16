#!/usr/bin/python3
"""Atheris coverage-guided fuzz harness for BlackBull's HTTP/1.1 server.

Sprint 17 Phase 5 — switched from the previous ``decode_to_request`` +
``HTTP1Client.request()`` pipeline to the shared :class:`Scenario`
abstraction.  Atheris's byte-level mutations now reach the wire
verbatim via :meth:`Scenario.from_bytes`.

Sprint 18 Phase 3 — when the env vars ``BB_FUZZ_NGINX_HOST`` and
``BB_FUZZ_NGINX_PORT`` are set, ``TestOneInput`` runs the same
scenario against nginx and BlackBull and *fails* (raises
``AssertionError``) on any non-accepted category — exactly the
oracle check the Hypothesis differential test does.  Without nginx
configured the harness falls back to single-server fuzz: useful for
catching client-side crashes but blind to protocol divergence.

Run::

    # single-server (legacy):
    python fuzz_http1.py -max_total_time=60 corpus/

    # differential (Sprint 18):
    BB_FUZZ_NGINX_HOST=127.0.0.1 BB_FUZZ_NGINX_PORT=8080 \\
        python fuzz_http1.py -max_total_time=60 corpus/

Findings (atheris-discovered crashes or differential failures) land
in the cwd as ``crash-<sha1>`` files and — when the differential
oracle is enabled — also get serialised as
``user-corpus/diff_<sha1>.{jsonl,meta.json}`` so
``test_corpus_replay`` picks them up next run.
"""
import os
import sys
import time
import asyncio
from multiprocessing import Process
from pathlib import Path

import requests
import atheris

with atheris.instrument_imports():
    from blackbull import BlackBull, read_body
    from blackbull.server import ASGIServer
    from blackbull.client import HTTP1Client, Scenario
    from blackbull.fault_injection import (
        ACCEPTED_CATEGORIES,
        Category,
        categorize,
        run_scenario,
    )
    from http import HTTPMethod, HTTPStatus


# Differential mode pairs this app against an nginx whose
# ``location /`` returns 200 "ok" for any method, any path.  To avoid
# every atheris-generated random path firing 404 (BlackBull) vs 200
# (nginx) — which would flood the fuzz with false-positive
# STATUS_DIFFER — the app below mirrors that nginx semantics via
# ``on_error`` handlers (same pattern as ``_make_diff_app`` in
# test_http1_differential.py).
app = BlackBull()


@app.route(path='/echo', methods=[HTTPMethod.GET, HTTPMethod.POST,
                                  HTTPMethod.PUT, HTTPMethod.DELETE,
                                  HTTPMethod.OPTIONS])
async def echo(scope, receive, send):
    body = await read_body(receive)
    await send({'type': 'http.response.start', 'status': 200,
                'headers': [(b'content-type', b'application/octet-stream')]})
    await send({'type': 'http.response.body', 'body': body})


@app.on_error(HTTPStatus.NOT_FOUND)
async def _not_found(scope, receive, send):
    await read_body(receive)
    await send({'type': 'http.response.start', 'status': 200,
                'headers': [(b'content-type', b'text/plain')]})
    await send({'type': 'http.response.body', 'body': b'ok'})


@app.on_error(HTTPStatus.METHOD_NOT_ALLOWED)
async def _method_not_allowed(scope, receive, send):
    await read_body(receive)
    await send({'type': 'http.response.start', 'status': 200,
                'headers': [(b'content-type', b'text/plain')]})
    await send({'type': 'http.response.body', 'body': b'ok'})


# ---------------------------------------------------------------------------
# Differential-oracle configuration
# ---------------------------------------------------------------------------

_NGINX_HOST = os.environ.get('BB_FUZZ_NGINX_HOST')
_NGINX_PORT_STR = os.environ.get('BB_FUZZ_NGINX_PORT')
_NGINX_PORT = int(_NGINX_PORT_STR) if _NGINX_PORT_STR else 0
_DIFFERENTIAL = bool(_NGINX_HOST and _NGINX_PORT)

# Captured findings land under user-corpus/ so test_corpus_replay
# picks them up.  The path is relative to this file so the harness
# can be invoked from any cwd.
_CORPUS_DIR = Path(__file__).parent / 'user-corpus'


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


async def _single_server(scenario: Scenario) -> None:
    """Legacy single-server path — drive scenario against BlackBull only.

    Wrapped in :func:`asyncio.wait_for` so a pathological scenario
    (e.g. lots of Sleep steps) can't stall the fuzzer.  The executor
    never raises; transport-layer outcomes are *expected* protocol
    responses, not crashes.
    """
    async with HTTP1Client('127.0.0.1', app.port,
                           connect_timeout=2.0) as c:
        await asyncio.wait_for(c.execute_scenario(scenario), timeout=3.0)


async def _differential(scenario: Scenario) -> Category:
    """Sprint 18 Phase 3 path — feed *scenario* to both servers,
    return the categoriser verdict.  Raises is delegated to
    :func:`TestOneInput`; this helper itself never raises."""
    # _DIFFERENTIAL gates the caller, so both are populated here.
    assert _NGINX_HOST is not None and _NGINX_PORT > 0
    ng, _ = await run_scenario(_NGINX_HOST, _NGINX_PORT, scenario)
    bb, _ = await run_scenario('127.0.0.1', app.port, scenario)
    return categorize(ng, bb)


def _maybe_dump_corpus(scenario: Scenario, category: Category) -> None:
    """Serialise a non-accepted scenario to user-corpus/ so
    test_corpus_replay can replay it.  Hash-only filename — same
    scenario overwrites in place."""
    import hashlib
    import json
    digest = hashlib.sha1(scenario.to_json().encode('utf-8')).hexdigest()[:16]
    _CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    base = _CORPUS_DIR / f'diff_{digest}'
    base.with_suffix('.jsonl').write_text(scenario.to_json() + '\n')
    meta = {
        'category': category.value,
        'captured_at_unix': int(time.time()),
        'source': 'fuzz_http1',
    }
    base.with_suffix('.meta.json').write_text(json.dumps(meta, indent=2))


def TestOneInput(data: bytes) -> None:
    """Atheris entry point.

    Bytes → :meth:`Scenario.from_bytes` (a total decoder) → either:

      * single-server mode: run against BlackBull and swallow
        transport-layer outcomes (we only care about server crashes
        atheris can observe through instrumentation);
      * differential mode (BB_FUZZ_NGINX_HOST + _PORT set): run
        against both nginx and BlackBull, categorise, and raise
        ``AssertionError`` on any non-accepted category — atheris's
        coverage-guided mutation has actual signal to drive on.
    """
    scenario = Scenario.from_bytes(data)
    if not scenario.steps:
        # Atheris seed corpora sometimes start with empty inputs;
        # short-circuit to keep iteration throughput up.
        return
    if _DIFFERENTIAL:
        try:
            cat = asyncio.run(_differential(scenario))
        except Exception:  # noqa: BLE001
            # Oracle plumbing failure (e.g. nginx not actually up).
            # Don't turn that into a fuzz finding — but do let the
            # user see it once.  Subsequent failures fall through to
            # the same swallow.
            return
        if cat not in ACCEPTED_CATEGORIES:
            _maybe_dump_corpus(scenario, cat)
            raise AssertionError(
                f'differential divergence: {cat.value}; scenario hash dumped '
                f'under user-corpus/diff_*'
            )
        return
    # Legacy single-server path.
    try:
        asyncio.run(_single_server(scenario))
    except (asyncio.TimeoutError, ConnectionError, OSError):
        return


def main():
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    p = None
    server = ASGIServer(app)
    try:
        server.open_socket(0)
        p = Process(target=lambda: asyncio.run(server.run()))
        p.start()
        wait_until_ready(f"http://localhost:{server.port}")
        if _DIFFERENTIAL:
            print(f'[fuzz] differential mode: nginx {_NGINX_HOST}:{_NGINX_PORT}',
                  file=sys.stderr)
        else:
            print('[fuzz] single-server mode (set BB_FUZZ_NGINX_HOST + '
                  'BB_FUZZ_NGINX_PORT for differential)', file=sys.stderr)
        main()
    except Exception as e:
        print(f"Error occurred: {e}")
    finally:
        server.close()
        if p is not None:
            p.terminate()
            p.join(timeout=5)

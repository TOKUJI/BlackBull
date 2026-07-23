"""
helloworld-cache.py
===================
Demonstrates the built-in ``Cache`` (per-worker in-memory LRU).

The app exposes a route that pretends to be expensive (``asyncio.sleep``
to fake an upstream query) and a counter route so you can see how often
the handler actually runs.  Two consecutive requests to the cached
route hit the handler exactly once; subsequent requests come straight
from the cache until the entry expires.

How the demo works
------------------
* ``GET /slow``  — sleeps 0.5 s on the first call, then returns a JSON
                   blob.  Cached for ``--ttl`` seconds (default 10).
                   Subsequent requests within the TTL return instantly.
* ``GET /counter`` — returns the number of times ``/slow`` has actually
                     run.  Doesn't tick on cache hits.
* ``GET /nostore`` — the handler emits ``Cache-Control: no-store``;
                     the middleware skips storage and runs the handler
                     every time.
* ``GET /private/<n>`` — path parameter shows how query / path variation
                         creates separate cache entries.

ETag round-trip
---------------
The middleware auto-generates a weak ETag (``W/"<sha256-prefix>"``) and
honours ``If-None-Match``::

    $ curl -i http://localhost:8000/slow
    HTTP/1.1 200 OK
    etag: W/"a1b2c3d4e5f60718"
    ...

    $ curl -i -H 'If-None-Match: W/"a1b2c3d4e5f60718"' http://localhost:8000/slow
    HTTP/1.1 304 Not Modified
    etag: W/"a1b2c3d4e5f60718"

Run::

    python helloworld-cache.py             # default 10-second TTL
    python helloworld-cache.py --ttl 60    # 60-second TTL

Try::

    time curl -s http://localhost:8000/slow   # ~0.5 s on the first call
    time curl -s http://localhost:8000/slow   # cached — instant

    curl -s http://localhost:8000/counter     # shows the handler ran once
    curl -s http://localhost:8000/nostore     # not cached
    curl -s http://localhost:8000/nostore
    curl -s http://localhost:8000/counter     # incremented twice
"""
import argparse
import asyncio
import time

from blackbull import BlackBull, JSONResponse
from blackbull.middleware.cache import Cache


app = BlackBull()
state = {'slow_calls': 0, 'nostore_calls': 0}


@app.route(path='/slow')
async def slow(conn, receive, send):
    """Pretend-expensive route — sleeps then returns a payload.

    Cached by the middleware; subsequent calls within the TTL don't
    re-enter this handler.
    """
    await asyncio.sleep(0.5)
    state['slow_calls'] += 1
    payload = {
        'message': 'this was slow',
        'served_at': time.time(),
        'handler_invocations': state['slow_calls'],
    }
    await send(JSONResponse(payload))


@app.route(path='/nostore')
async def nostore(conn, receive, send):
    """Same payload, but ``Cache-Control: no-store`` skips the cache."""
    state['nostore_calls'] += 1
    payload = {
        'message': 'always fresh',
        'served_at': time.time(),
        'handler_invocations': state['nostore_calls'],
    }
    await send(JSONResponse(
        payload,
        headers=[(b'cache-control', b'no-store')],
    ))


@app.route(path='/private/{n}')
async def private_n(conn, receive, send):   # full form: `conn` is a native Connection
    """Each distinct path parameter is a separate cache key."""
    n = conn.path_params.get('n', '?')
    await send(JSONResponse({'n': n, 'served_at': time.time()}))


@app.route(path='/counter')
async def counter(conn, receive, send):
    """How many times each cached/uncached handler has actually run."""
    await send(JSONResponse(state))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='BlackBull cache-middleware demo')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--ttl', type=int, default=10,
                        help='cache TTL in seconds (default 10)')
    args = parser.parse_args()

    app.use(Cache(max_age=args.ttl))

    print(f'Listening on http://localhost:{args.port}')
    print(f'Cache: max_age={args.ttl}s, max_entries=1024 (default)')
    print('Routes: /slow  /nostore  /private/{n}  /counter')
    print()
    print('Try:')
    print(f'  time curl -s http://localhost:{args.port}/slow   # first call: ~0.5s')
    print(f'  time curl -s http://localhost:{args.port}/slow   # cached: instant')
    print(f'  curl -s http://localhost:{args.port}/counter     # handler ran once')

    app.run(port=args.port)

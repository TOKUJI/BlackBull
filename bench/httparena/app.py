"""BlackBull entrypoint for HttpArena benchmark profiles.

Implements the endpoint contract documented at
https://www.http-arena.com/docs/add-framework/ for the H1 + WebSocket
profiles BlackBull supports today:

  GET  /pipeline                              → text/plain "ok"
  GET  /baseline11?<int=int>&…                → text/plain sum of query ints
  POST /baseline11?<int=int>&…  body=<int>    → text/plain sum
  GET  /json/{count}?m=<float>                → JSON {items, count}
  GET  /json-comp/{count}?m=<float>           → JSON, may be gzipped
  POST /upload          body                  → text/plain byte count
  GET  /ws (Upgrade)                          → echoes frames
  GET  /async-db?min&max&limit                → JSON {items, count} (Postgres)
  GET  /crud/items?category&page&limit        → JSON {items, total, page, limit}
  POST /crud/items      body=Item             → 201 (upsert)
  GET  /crud/items/{id}                       → JSON Item (Redis-cached)
  PUT  /crud/items/{id} body                  → 200 (update + cache invalidate)
  unary gRPC benchmark.BenchmarkService/GetSum → SumReply{a+b}

Dataset is read from $DATASET_PATH (default /data/dataset.json — the
read-only mount HttpArena's harness provides).  The ``async-db`` and ``crud``
profiles use the seeded Postgres (and ``crud`` the Redis cache) sidecars the
HttpArena harness provides; see ``db.py`` for the env vars.  ``api-4`` /
``api-16`` are load-generator CPU-budget profiles over ``/baseline11`` (plus
json / async-db) and need no dedicated endpoint.

The gRPC service exposes both ``GetSum`` (unary; ``unary-grpc`` /
``unary-grpc-tls``) and ``StreamSum`` (server-streaming; ``stream-grpc`` /
``stream-grpc-tls``) via ``app.enable_grpc(build_registry())`` — see
``grpc_bench.py``.

Profiles intentionally NOT implemented:
  - *-h3              (no HTTP/3 transport)
  - production-stack / gateway / fortunes

The container starts four BlackBull processes via ``launcher.py``:
cleartext on :8080, h2c on :8082, TLS HTTP/1.1 on :8081, TLS HTTP/2
on :8443.  Cleartext also serves h2c via prior-knowledge — BlackBull
negotiates HTTP/2 on first preface bytes.
"""
import argparse
import json
import os
import sys
from http import HTTPMethod
from urllib.parse import parse_qs

# Scheme.websocket is the BlackBull marker used by `@app.route` to
# register the `echo-ws` HttpArena profile handler.
from blackbull.utils import Scheme

# Ensure the BlackBull source tree is importable when the Docker image
# vendors it at /src/BlackBull/.  Local runs use `pip install -e .` so
# this is a no-op then.
_repo_root = os.environ.get('BLACKBULL_SRC', '/src/BlackBull')
if os.path.isdir(_repo_root) and _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from blackbull import BlackBull, Connection, Depends, JSONResponse, Response
from blackbull.middleware.compression import Compression

import db
# gRPC support is optional — older BlackBull versions (≤v0.33.x) and
# minimal installs don't ship the gRPC bridge.  When unavailable the
# four gRPC profiles (unary-grpc, unary-grpc-tls, stream-grpc,
# stream-grpc-tls) are silently absent.
try:
    from grpc_bench import build_registry
except ImportError:
    build_registry = None


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

DATASET_PATH = os.environ.get('DATASET_PATH', '/data/dataset.json')
try:
    with open(DATASET_PATH, 'r') as f:
        DATASET_ITEMS = json.load(f)
except (OSError, ValueError):
    DATASET_ITEMS = []


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = BlackBull()

# HttpArena's json-comp profile expects Accept-Encoding-driven compression.
# BlackBull's Compression middleware picks br > zstd > gzip from the codecs
# the container has installed.  Bodies below min_size (default 100 bytes)
# pass through, so /baseline11 + /pipeline aren't affected.
app.use(Compression())

# HttpArena's static profile expects /static/<asset> to serve files from
# /data/static/.  app.static() registers a StaticFiles middleware with the
# URL prefix and source directory; missing files (e.g. when /data/static/
# is unpopulated in a sandbox run) return 404 without breaking other routes.
app.static('/static', os.environ.get('STATIC_DIR', '/data/static/'))

_PIPELINE_BODY = b'ok'
_NO_DATASET = b'No dataset'
_PLAIN = 'text/plain; charset=utf-8'


def _qs(conn: Connection):
    """Parse query string from a Connection into a multi-value dict."""
    raw = conn.query_string or b''
    return parse_qs(raw.decode('latin-1'), keep_blank_values=True)


@app.route(path='/pipeline', methods=[HTTPMethod.GET])
async def pipeline():
    return Response(_PIPELINE_BODY, content_type=_PLAIN)


async def _baseline_handler(conn: Connection):
    """Shared body for /baseline11 (H/1.1) and /baseline2 (H/2).

    HttpArena uses path-suffix to distinguish the two profiles, but
    the semantics are identical: sum integer query params, add posted
    body if integer, return as text/plain.

    Sprint 79: uses ``Connection`` instead of the raw ASGI triplet.
    """
    total = 0
    for vals in _qs(conn).values():
        for v in vals:
            try:
                total += int(v)
            except ValueError:
                pass
    if conn.method == 'POST':
        body = await conn.body()
        if body:
            try:
                total += int(body.strip())
            except ValueError:
                pass
    return Response(str(total).encode(), content_type=_PLAIN)


@app.route(path='/baseline11', methods=[HTTPMethod.GET, HTTPMethod.POST])
async def baseline11(conn: Connection):
    return await _baseline_handler(conn)


# HttpArena's H/2 baseline profile uses /baseline2 (path suffix
# disambiguates from the H/1.1 /baseline11).  Same semantics.
@app.route(path='/baseline2', methods=[HTTPMethod.GET, HTTPMethod.POST])
async def baseline2(conn: Connection):
    return await _baseline_handler(conn)


def _json_payload(count: int, m: float):
    items = []
    for idx, ds in enumerate(DATASET_ITEMS):
        if idx >= count:
            break
        item = dict(ds)
        item['total'] = ds['price'] * ds['quantity'] * m
        items.append(item)
    return {'items': items, 'count': len(items)}


@app.route(path='/json/{count:int}', methods=[HTTPMethod.GET])
async def json_endpoint(count: int, conn: Connection):
    if not DATASET_ITEMS:
        return Response(_NO_DATASET, status=500, content_type=_PLAIN)
    try:
        m = float(_qs(conn).get('m', ['0'])[0])
    except ValueError:
        m = 0.0
    return JSONResponse(_json_payload(count, m))


@app.route(path='/json-comp/{count:int}', methods=[HTTPMethod.GET])
async def json_comp_endpoint(count: int, conn: Connection):
    # Same payload as /json; the Compression middleware registered
    # at module top wraps the response with gzip / brotli / zstd per
    # the client's Accept-Encoding.
    if not DATASET_ITEMS:
        return Response(_NO_DATASET, status=500, content_type=_PLAIN)
    try:
        m = float(_qs(req).get('m', ['0'])[0])
    except ValueError:
        m = 0.0
    return JSONResponse(_json_payload(count, m))


@app.route(path='/upload', methods=[HTTPMethod.POST])
async def upload_endpoint(conn: Connection):
    # Sprint 80: stream the body with ``Connection.stream()`` — the profile only
    # needs the byte count, so we never materialize the (up to 20 MB) payload.
    # ``conn.body()`` would ``b''.join`` the whole upload (~4-12x the CPU and a
    # multiple of the throughput under load); streaming keeps a one-chunk working
    # set, matching the HttpArena "small read buffers" fast path.
    size = 0
    async for chunk in conn.stream():
        size += len(chunk)
    return Response(str(size).encode(), content_type=_PLAIN)


# Liveness for ``launcher.py``'s readiness probe.
@app.route(path='/healthz', methods=[HTTPMethod.GET])
async def healthz():
    return Response(b'ok', content_type=_PLAIN)


# HttpArena `echo-ws` profile — RFC 6455 WebSocket echo.  First
# message after accept is the receive loop; text frames echo as text,
# binary frames echo as bytes.
@app.route(path='/ws', methods=[HTTPMethod.GET], scheme=Scheme.websocket)
async def ws_echo(scope, receive, send):
    event = await receive()
    if event.get('type') != 'websocket.connect':
        return
    await send({'type': 'websocket.accept'})
    while True:
        event = await receive()
        t = event.get('type', '')
        if t == 'websocket.disconnect':
            break
        if t != 'websocket.receive':
            continue
        text = event.get('text')
        if text is not None:
            await send({'type': 'websocket.send', 'text': text})
        else:
            await send({'type': 'websocket.send',
                        'bytes': event.get('bytes') or b''})


# ---------------------------------------------------------------------------
# async-db profile — Postgres price-range query (db.py degrades to empty
# when no database is configured, per the HttpArena contract).
# ---------------------------------------------------------------------------

def _int_qs(conn: Connection, name, default):
    """Read an integer query param from a Connection."""
    try:
        return int(_qs(conn).get(name, [str(default)])[0])
    except (ValueError, IndexError):
        return default


@app.route(path='/async-db', methods=[HTTPMethod.GET])
async def async_db_endpoint(req: Connection, db_conn=Depends(db.get_db_conn)):
    # Sprint 79: ``req`` is the BlackBull Connection; ``conn`` is the db handle.
    min_price = _int_qs(req, 'min', 10)
    max_price = _int_qs(req, 'max', 50)
    limit = max(1, min(_int_qs(req, 'limit', 50), 50))
    items = await db.async_db(db_conn, min_price, max_price, limit)
    return JSONResponse({'items': items, 'count': len(items)})


# ---------------------------------------------------------------------------
# crud profile — paginated list, upsert, cached get-by-id, update+invalidate.
# ---------------------------------------------------------------------------

@app.route(path='/crud/items', methods=[HTTPMethod.GET])
async def crud_items_list(req: Connection, db_conn=Depends(db.get_db_conn)):
    # Sprint 79: ``req`` is the BlackBull Connection; ``conn`` is the db handle.
    qs = _qs(req)
    category = qs.get('category', [None])[0]
    page = _int_qs(req, 'page', 1)
    limit = max(1, min(_int_qs(req, 'limit', 10), 50))
    items = await db.crud_list(db_conn, category, page, limit)
    # Load-more semantics: total == items in this response (per the contract).
    return JSONResponse({'items': items, 'total': len(items),
                         'page': page, 'limit': limit})


@app.route(path='/crud/items', methods=[HTTPMethod.POST])
async def crud_items_create(body: bytes, db_conn=Depends(db.get_db_conn)):
    try:
        data = json.loads(body)
    except ValueError:
        return Response(b'invalid JSON', status=400, content_type=_PLAIN)
    ok = await db.crud_create(db_conn, data)
    if not ok:
        return Response(b'unavailable', status=503, content_type=_PLAIN)
    return JSONResponse({'id': data.get('id')}, status=201)


# get-by-id is cache-first: inject the *pool* (plain-async provider), not a
# per-request connection — a Redis hit must not check a connection out.
@app.route(path='/crud/items/{item_id:int}', methods=[HTTPMethod.GET])
async def crud_items_get(item_id: int, pool=Depends(db.get_pool)):
    item = await db.crud_get(pool, item_id)
    if item is None:
        return Response(b'not found', status=404, content_type=_PLAIN)
    return JSONResponse(item)


@app.route(path='/crud/items/{item_id:int}', methods=[HTTPMethod.PUT])
async def crud_items_update(item_id: int, body: bytes, db_conn=Depends(db.get_db_conn)):
    try:
        data = json.loads(body)
    except ValueError:
        return Response(b'invalid JSON', status=400, content_type=_PLAIN)
    ok = await db.crud_update(db_conn, item_id, data)
    if not ok:
        return Response(b'not found', status=404, content_type=_PLAIN)
    return JSONResponse({'id': item_id})


# gRPC profiles (unary-grpc, unary-grpc-tls, stream-grpc, stream-grpc-tls).
# Only enabled when both the gRPC bridge and grpc_bench are available.
if build_registry is not None and hasattr(app, 'enable_grpc'):
    app.enable_grpc(build_registry())


# ---------------------------------------------------------------------------
# Entry point — invoked by launcher.py once per listener port.
# ---------------------------------------------------------------------------

def _parse_args():
    p = argparse.ArgumentParser(description='BlackBull on HttpArena')
    p.add_argument('--port', type=int, required=True)
    p.add_argument('--cert')
    p.add_argument('--key')
    p.add_argument('--workers', type=int, default=None)
    return p.parse_args()


if __name__ == '__main__':
    args = _parse_args()
    # Match peer benchmark posture: access log off (apples-to-apples).
    os.environ.setdefault('BB_ACCESS_LOG', '0')
    # Access logging goes through BlackBull's PRODUCTION async path — NOT a
    # synchronous FileHandler.  When BB_ACCESS_LOG=1 the framework's per-worker
    # setup_async_logging (worker.py, post-fork) installs the deferred-format
    # QueueHandler on the 'blackbull' logger and a listener thread that writes
    # to BB_LOG_FILE (injected by install_docker_shim.sh → /results/…, on the
    # mounted volume).  Batching (O2) composes via BB_LOG_BATCH_SIZE.  So the
    # event loop only does a queue put per request — exactly what ships — and
    # the bench measures that, not format()+write()+flush() on the loop.
    #
    # The one thing the framework does not set is the access logger LEVEL, so
    # do it here (pre-fork; inherited by every worker) — otherwise the default
    # WARNING root level would drop the INFO access records before they enqueue.
    # The legacy logging_access.ini (synchronous FileHandler) is intentionally
    # no longer loaded.
    if os.environ.get('BB_ACCESS_LOG', '0') not in ('0', '', 'false', 'False'):
        import logging as _logging
        _logging.getLogger('blackbull.access').setLevel(_logging.INFO)
    if args.cert and args.key:
        app.run(port=args.port, certfile=args.cert, keyfile=args.key,
                workers=args.workers)
    else:
        app.run(port=args.port, workers=args.workers)

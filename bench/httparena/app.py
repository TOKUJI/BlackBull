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

from blackbull import BlackBull, JSONResponse, Response, read_body
from blackbull.middleware.compression import Compression

import db
from grpc_bench import build_registry


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
#
# Diagnostic toggle: BB_NO_COMPRESSION=1 skips registering Compression
# entirely.  Useful for isolating the cost of on-the-fly brotli encoding
# on already-compressed payloads (e.g. .woff2 fonts) that lack a
# precompressed sibling.  Not for benchmark publication — disabling a
# default-on feature breaks the apples-to-apples convention.
if os.environ.get('BB_NO_COMPRESSION', '0') != '1':
    app.use(Compression())

# HttpArena's static profile expects /static/<asset> to serve files from
# /data/static/.  app.static() registers a StaticFiles middleware with the
# URL prefix and source directory; missing files (e.g. when /data/static/
# is unpopulated in a sandbox run) return 404 without breaking other routes.
app.static('/static', os.environ.get('STATIC_DIR', '/data/static/'))

_PIPELINE_BODY = b'ok'
_NO_DATASET = b'No dataset'
_PLAIN = 'text/plain; charset=utf-8'


def _qs(scope):
    raw = scope.get('query_string') or b''
    return parse_qs(raw.decode('latin-1'), keep_blank_values=True)


@app.route(path='/pipeline', methods=[HTTPMethod.GET])
async def pipeline():
    return Response(_PIPELINE_BODY, content_type=_PLAIN)


async def _baseline_handler(scope, receive, send):
    """Shared body for /baseline11 (H/1.1) and /baseline2 (H/2).

    HttpArena uses path-suffix to distinguish the two profiles, but
    the semantics are identical: sum integer query params, add posted
    body if integer, return as text/plain.
    """
    total = 0
    for vals in _qs(scope).values():
        for v in vals:
            try:
                total += int(v)
            except ValueError:
                pass
    if scope['method'] == 'POST':
        body = await read_body(receive)
        if body:
            try:
                total += int(body.strip())
            except ValueError:
                pass
    payload = str(total).encode()
    await send({'type': 'http.response.start', 'status': 200,
                'headers': [(b'content-type', _PLAIN.encode())]})
    await send({'type': 'http.response.body', 'body': payload})


@app.route(path='/baseline11', methods=[HTTPMethod.GET, HTTPMethod.POST])
async def baseline11(scope, receive, send):
    await _baseline_handler(scope, receive, send)


# HttpArena's H/2 baseline profile uses /baseline2 (path suffix
# disambiguates from the H/1.1 /baseline11).  Same semantics.
@app.route(path='/baseline2', methods=[HTTPMethod.GET, HTTPMethod.POST])
async def baseline2(scope, receive, send):
    await _baseline_handler(scope, receive, send)


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
async def json_endpoint(count: int, scope):
    if not DATASET_ITEMS:
        return Response(_NO_DATASET, status=500, content_type=_PLAIN)
    try:
        m = float(_qs(scope).get('m', ['0'])[0])
    except ValueError:
        m = 0.0
    return JSONResponse(_json_payload(count, m))


@app.route(path='/json-comp/{count:int}', methods=[HTTPMethod.GET])
async def json_comp_endpoint(count: int, scope):
    # Same payload as /json; the Compression middleware registered
    # at module top wraps the response with gzip / brotli / zstd per
    # the client's Accept-Encoding.
    if not DATASET_ITEMS:
        return Response(_NO_DATASET, status=500, content_type=_PLAIN)
    try:
        m = float(_qs(scope).get('m', ['0'])[0])
    except ValueError:
        m = 0.0
    return JSONResponse(_json_payload(count, m))


@app.route(path='/upload', methods=[HTTPMethod.POST])
async def upload_endpoint(scope, receive, send):
    size = 0
    while True:
        msg = await receive()
        if msg['type'] != 'http.request':
            break
        size += len(msg.get('body') or b'')
        if not msg.get('more_body', False):
            break
    payload = str(size).encode()
    await send({'type': 'http.response.start', 'status': 200,
                'headers': [(b'content-type', _PLAIN.encode())]})
    await send({'type': 'http.response.body', 'body': payload})


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

def _int_qs(scope, name, default):
    try:
        return int(_qs(scope).get(name, [str(default)])[0])
    except (ValueError, IndexError):
        return default


@app.route(path='/async-db', methods=[HTTPMethod.GET])
async def async_db_endpoint(scope):
    min_price = _int_qs(scope, 'min', 10)
    max_price = _int_qs(scope, 'max', 50)
    limit = max(1, min(_int_qs(scope, 'limit', 50), 50))
    items = await db.async_db(min_price, max_price, limit)
    return JSONResponse({'items': items, 'count': len(items)})


# ---------------------------------------------------------------------------
# crud profile — paginated list, upsert, cached get-by-id, update+invalidate.
# ---------------------------------------------------------------------------

@app.route(path='/crud/items', methods=[HTTPMethod.GET])
async def crud_items_list(scope):
    qs = _qs(scope)
    category = qs.get('category', [None])[0]
    page = _int_qs(scope, 'page', 1)
    limit = max(1, min(_int_qs(scope, 'limit', 10), 50))
    items = await db.crud_list(category, page, limit)
    # Load-more semantics: total == items in this response (per the contract).
    return JSONResponse({'items': items, 'total': len(items),
                         'page': page, 'limit': limit})


@app.route(path='/crud/items', methods=[HTTPMethod.POST])
async def crud_items_create(body: bytes):
    try:
        data = json.loads(body)
    except ValueError:
        return Response(b'invalid JSON', status=400, content_type=_PLAIN)
    ok = await db.crud_create(data)
    if not ok:
        return Response(b'unavailable', status=503, content_type=_PLAIN)
    return JSONResponse({'id': data.get('id')}, status=201)


@app.route(path='/crud/items/{item_id:int}', methods=[HTTPMethod.GET])
async def crud_items_get(item_id: int):
    item = await db.crud_get(item_id)
    if item is None:
        return Response(b'not found', status=404, content_type=_PLAIN)
    return JSONResponse(item)


@app.route(path='/crud/items/{item_id:int}', methods=[HTTPMethod.PUT])
async def crud_items_update(item_id: int, body: bytes):
    try:
        data = json.loads(body)
    except ValueError:
        return Response(b'invalid JSON', status=400, content_type=_PLAIN)
    ok = await db.crud_update(item_id, data)
    if not ok:
        return Response(b'not found', status=404, content_type=_PLAIN)
    return JSONResponse({'id': item_id})


# unary-grpc profile — benchmark.BenchmarkService/GetSum over h2c/h2.
app.enable_grpc(build_registry())


# ---------------------------------------------------------------------------
# Cold-start self-warm-up (opt-in via BB_GRPC_WARMUP=<seconds>).
#
# Registered as a core ``@app.on_warmup`` hook: it runs ONCE in the master,
# before the listening socket is created and before workers fork, so every
# worker is born warm via copy-on-write (see ``BlackBull.on_warmup``).  This
# replaces the previous per-worker ``@app.on_startup`` + loopback warm-up, which
# ran AFTER fork and AFTER the socket already accepted, raced the benchmark, and
# fixed only the lightest profile.
#
# The hook drives the real ASGI dispatch + gRPC codec paths in-process via
# ``app.drive_asgi`` (no socket): it faults in code pages and trips PEP 659
# specialization on the GetSum (unary) and StreamSum (streaming) branches, which
# then survive fork.  The TLS handshake path is primed automatically by the
# framework (``warm_tls``) when the listener terminates TLS, so the hook itself
# stays transport-agnostic.
_GRPC_WARMUP_SECS = float(os.environ.get('BB_GRPC_WARMUP', '0') or 0)

if _GRPC_WARMUP_SECS > 0:
    import time as _time
    from grpc_bench import _STREAMSUM_PATH, _GETSUM_PATH, _write_varint as _wv
    from blackbull.grpc import encode_message as _encode_message

    def _enc_stream_req(a: int, b: int, count: int) -> bytes:
        # StreamRequest{a=1,b=2,count=3} → gRPC length-prefixed frame.
        return _encode_message(b'\x08' + _wv(a) + b'\x10' + _wv(b)
                               + b'\x18' + _wv(count))

    def _enc_sum_req(a: int, b: int) -> bytes:
        # SumRequest{a=1,b=2} → gRPC length-prefixed frame.
        return _encode_message(b'\x08' + _wv(a) + b'\x10' + _wv(b))

    def _grpc_scope(path: str) -> dict:
        return {'type': 'http', 'method': 'POST', 'path': path,
                'headers': [(b'content-type', b'application/grpc'),
                            (b'te', b'trailers')]}

    @app.on_warmup
    async def _grpc_warmup(app) -> None:
        import asyncio  # noqa: PLC0415
        print(f'grpc warm-up: starting {_GRPC_WARMUP_SECS}s budget '
              f'(pid={os.getpid()})', file=sys.stderr, flush=True)
        # Warm BOTH gRPC dispatch paths: the streaming path (StreamSum, for the
        # stream-grpc profiles) AND the unary path (GetSum, for the unary-grpc
        # profiles).  The unary serve branch + coalescing sender are distinct
        # code from streaming, so a StreamSum-only warm-up leaves unary cold.
        stream_scope = _grpc_scope(_STREAMSUM_PATH)
        unary_scope = _grpc_scope(_GETSUM_PATH)
        stream_body = _enc_stream_req(13, 42, 5000)   # match benchmark shape
        unary_body = _enc_sum_req(1, 2)
        conc = int(os.environ.get('BB_GRPC_WARMUP_CONC', '64') or 64)

        deadline = _time.monotonic() + _GRPC_WARMUP_SECS
        n_stream = n_unary = 0
        while _time.monotonic() < deadline:
            await asyncio.gather(*[
                app.drive_asgi(stream_scope, body=stream_body, n=1)
                for _ in range(conc)])
            n_stream += conc
            # Unary calls are ~5000× lighter than a StreamSum call, so drive
            # many more per pass to settle the unary path's allocations at a
            # volume closer to what h2load will impose.
            await asyncio.gather(*[
                app.drive_asgi(unary_scope, body=unary_body, n=1)
                for _ in range(conc * 8)])
            n_unary += conc * 8

        print(f'grpc warm-up: done {n_stream} StreamSum + {n_unary} GetSum '
              f'in-process in {_GRPC_WARMUP_SECS}s (pid={os.getpid()})',
              file=sys.stderr, flush=True)


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
    # If logging_access.ini is present (placed by httparena_compare.sh when
    # BB_ACCESS_LOG=1), apply it now via the standard logging.config mechanism.
    # This is the single, declarative place that enables the blackbull.access
    # logger — no handler setup is scattered across launcher.py or library code.
    _logging_ini = os.path.join(os.path.dirname(__file__), 'logging_access.ini')
    if os.path.isfile(_logging_ini):
        import logging.config as _logging_config
        _logging_config.fileConfig(_logging_ini)
    if args.cert and args.key:
        app.run(port=args.port, certfile=args.cert, keyfile=args.key,
                workers=args.workers)
    else:
        app.run(port=args.port, workers=args.workers)

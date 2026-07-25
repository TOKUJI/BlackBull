"""Sanic entrypoint for HttpArena benchmark profiles — BlackBull-equivalent.
Compression middleware + asyncpg pool + Redis, same endpoints.
"""
import argparse, gzip as _gzip, json as _json, os, sys
from sanic import Sanic
from sanic.response import raw, text, json as json_resp, empty

app = Sanic("httparena-bench")

# ── Compression (matching BlackBull's Compression middleware) ──────────────
# Sanic core has no built-in response compression (RESPONSE_AUTO_COMPRESS is
# not a real config key), so we negotiate gzip in a response middleware — only
# when the client advertises Accept-Encoding: gzip, mirroring BlackBull.
_COMPRESS_MIN_LEN = 100
_COMPRESSIBLE_CT = ("text/", "application/json", "application/javascript",
                    "image/svg+xml")

@app.middleware("response")
async def _compress(request, response):
    body = getattr(response, "body", None)
    if not body or len(body) < _COMPRESS_MIN_LEN:
        return
    if "gzip" not in (request.headers.get("accept-encoding") or ""):
        return
    if response.headers.get("content-encoding"):
        return
    if not (response.content_type or "").startswith(_COMPRESSIBLE_CT):
        return
    response.body = _gzip.compress(body)
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = str(len(response.body))

# ── Dataset ────────────────────────────────────────────────────────────────
DATASET_PATH = os.environ.get("DATASET_PATH", "/data/dataset.json")
try:
    with open(DATASET_PATH) as f: DATASET_ITEMS = _json.load(f)
except (OSError, ValueError): DATASET_ITEMS = []

_PIPELINE_BODY = b"ok"; _NO_DATASET = b"No dataset"
_PLAIN_CT = "text/plain; charset=utf-8"; _OCTET_CT = "application/octet-stream"

# ── Static file cache ──────────────────────────────────────────────────────
STATIC_DIR = os.environ.get("STATIC_DIR", "/data/static/")
_STATIC_CACHE = {}
_STATIC_FILES = [
    "reset.css","layout.css","theme.css","components.css","utilities.css",
    "analytics.js","helpers.js","app.js","vendor.js","router.js",
    "header.html","footer.html","regular.woff2","bold.woff2",
    "logo.svg","icon-sprite.svg","hero.webp","thumb1.webp","thumb2.webp","manifest.json",
]
for fn in _STATIC_FILES:
    try:
        with open(os.path.join(STATIC_DIR, fn), "rb") as f: _STATIC_CACHE[fn] = f.read()
    except (OSError, ValueError): pass

_MIME = {".css":"text/css",".js":"application/javascript",".html":"text/html; charset=utf-8",
         ".woff2":"font/woff2",".svg":"image/svg+xml",".webp":"image/webp",".json":"application/json"}
def _mime(fn):
    for ext,mt in _MIME.items():
        if fn.endswith(ext): return mt
    return "application/octet-stream"

# ── Database pool + Redis (HttpArena env, mirrors bench/httparena/db.py) ──────
# HttpArena hands framework containers DATABASE_URL / REDIS_URL (NOT DB_HOST/…),
# and DATABASE_MAX_CONN. Pool is sized from that budget split across workers so
# workers × per_worker stays under the Postgres sidecar's max_connections=256 —
# sizing at the full budget per worker starves the server and collapses latency.
DATABASE_URL = os.environ.get("DATABASE_URL") or None
REDIS_URL = os.environ.get("REDIS_URL") or None
try: DATABASE_MAX_CONN = int(os.environ.get("DATABASE_MAX_CONN", "256"))
except ValueError: DATABASE_MAX_CONN = 256
_CACHE_TTL = 30  # seconds; get-by-id cache entries

def _worker_count():
    env = os.environ.get("WEB_WORKERS", "").strip()
    if env:
        try: return max(1, int(env))
        except ValueError: pass
    try:
        import multiprocessing
        return max(1, multiprocessing.cpu_count())
    except NotImplementedError:
        return 1

POOL_MAX_SIZE = max(1, min(DATABASE_MAX_CONN, 240) // _worker_count())

@app.listener('before_server_start')
async def setup_db(app):
    app.ctx.db_pool = None
    app.ctx.redis = None
    if DATABASE_URL:
        try:
            import asyncpg
            app.ctx.db_pool = await asyncpg.create_pool(
                dsn=DATABASE_URL, min_size=1, max_size=POOL_MAX_SIZE)
        except Exception: app.ctx.db_pool = None
    if REDIS_URL:
        try:
            import redis.asyncio as aioredis
            app.ctx.redis = aioredis.from_url(REDIS_URL, decode_responses=False)
        except Exception: app.ctx.redis = None

@app.listener('after_server_stop')
async def close_db(app):
    if getattr(app.ctx, 'db_pool', None): await app.ctx.db_pool.close()
    if getattr(app.ctx, 'redis', None):
        try: await app.ctx.redis.aclose()
        except Exception: pass

# Items row → wire shape: nest rating_{score,count} → rating{}, decode the tags
# TEXT column (a JSON-encoded array string) back into a list.
_ITEM_COLS = ("id, name, category, price, quantity, active, tags, "
              "rating_score, rating_count")
def _row_to_item(row):
    item = dict(row)
    item["rating"] = {"score": item.pop("rating_score", None),
                      "count": item.pop("rating_count", None)}
    tags = item.get("tags")
    if isinstance(tags, str):
        try: item["tags"] = _json.loads(tags)
        except (ValueError, TypeError): item["tags"] = []
    return item

# ── Routes ──────────────────────────────────────────────────────────────────
@app.get("/pipeline")
async def pipeline(_): return raw(_PIPELINE_BODY, content_type=_PLAIN_CT)

@app.get("/healthz")
async def healthz(_): return raw(b"ok", content_type=_PLAIN_CT)

def _baseline_qs(request):
    total = 0
    for vals in (request.args or {}).values():
        for v in vals:
            try: total += int(v)
            except ValueError: pass
    if request.method == "POST":
        body = request.body or b""
        if body:
            try: total += int(body.strip())
            except ValueError: pass
    return total

@app.route("/baseline11", methods=["GET","POST"])
async def baseline11(request): return text(str(_baseline_qs(request)))

@app.route("/baseline2", methods=["GET","POST"])
async def baseline2(request): return text(str(_baseline_qs(request)))

def _json_payload(count, m):
    items = []
    for idx, ds in enumerate(DATASET_ITEMS):
        if idx >= count: break
        item = dict(ds); item["total"] = ds["price"] * ds["quantity"] * m
        items.append(item)
    return {"items": items, "count": len(items)}

@app.get("/json/<count:int>")
async def json_endpoint(request, count):
    if not DATASET_ITEMS: return raw(_NO_DATASET, status=500, content_type=_PLAIN_CT)
    try: m = float((request.args or {}).get("m", "0"))
    except (ValueError, TypeError): m = 0.0
    return json_resp(_json_payload(count, m))

@app.get("/json-comp/<count:int>")
async def json_comp_endpoint(request, count):
    if not DATASET_ITEMS: return raw(_NO_DATASET, status=500, content_type=_PLAIN_CT)
    try: m = float((request.args or {}).get("m", "0"))
    except (ValueError, TypeError): m = 0.0
    return json_resp(_json_payload(count, m))

@app.post("/upload")
async def upload_endpoint(request): return text(str(len(request.body or b"")))

@app.get("/static/<filename:path>")
async def static_file(_, filename):
    fn = filename.lstrip("/")
    body = _STATIC_CACHE.get(fn)
    if body is None:
        try:
            with open(os.path.join(STATIC_DIR, fn), "rb") as f: body = f.read()
            _STATIC_CACHE[fn] = body
        except (OSError, ValueError): return empty(status=404)
    return raw(body, content_type=_mime(fn))

@app.websocket("/ws")
async def ws_echo(_, ws):
    while True:
        data = await ws.recv()
        await ws.send(data)

# ── async-db ────────────────────────────────────────────────────────────────
@app.get("/async-db")
async def async_db_endpoint(request):
    pool = request.app.ctx.db_pool
    if pool is None: return json_resp({"items":[],"count":0})
    args = request.args or {}
    try:
        lo = int(args.get("min", "10"))
        hi = int(args.get("max", "50"))
        lim = max(1, min(int(args.get("limit", "50")), 50))
    except (ValueError, TypeError): return json_resp({"items":[],"count":0})
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {_ITEM_COLS} FROM items "
                "WHERE price BETWEEN $1 AND $2 LIMIT $3", lo, hi, lim)
        items = [_row_to_item(r) for r in rows]
        return json_resp({"items": items, "count": len(items)})
    except Exception: return json_resp({"items":[],"count":0})

# ── crud ────────────────────────────────────────────────────────────────────
@app.get("/crud/items")
async def crud_items_list(request):
    pool = request.app.ctx.db_pool
    args = request.args or {}
    cat = args.get("category")
    try:
        pg = max(1, int(args.get("page", "1")))
        lim = max(1, min(int(args.get("limit", "10")), 50))
    except (ValueError, TypeError): pg, lim = 1, 10
    if pool is None: return json_resp({"items":[],"total":0,"page":pg,"limit":lim})
    off = (pg - 1) * lim
    try:
        async with pool.acquire() as conn:
            if cat:
                rows = await conn.fetch(
                    f"SELECT {_ITEM_COLS} FROM items WHERE category=$1 "
                    "ORDER BY id LIMIT $2 OFFSET $3", cat, lim, off)
            else:
                rows = await conn.fetch(
                    f"SELECT {_ITEM_COLS} FROM items "
                    "ORDER BY id LIMIT $1 OFFSET $2", lim, off)
        items = [_row_to_item(r) for r in rows]
        return json_resp({"items": items, "total": len(items), "page": pg, "limit": lim})
    except Exception: return json_resp({"items":[],"total":0,"page":pg,"limit":lim})

@app.post("/crud/items")
async def crud_items_create(request):
    pool = request.app.ctx.db_pool
    try: data = request.json
    except Exception: return text("invalid JSON", status=400)
    if not isinstance(data, dict): return text("invalid JSON", status=400)
    if pool is None: return text("unavailable", status=503)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO items (id, name, category, price, quantity) "
                "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (id) DO UPDATE SET "
                "name = EXCLUDED.name, category = EXCLUDED.category, "
                "price = EXCLUDED.price, quantity = EXCLUDED.quantity",
                int(data["id"]), data["name"], data["category"],
                int(data["price"]), int(data["quantity"]))
        if request.app.ctx.redis:
            try: await request.app.ctx.redis.delete(f"item:{int(data['id'])}")
            except Exception: pass
        return json_resp({"id": data.get("id")}, status=201)
    except Exception: return text("unavailable", status=503)

@app.get("/crud/items/<item_id:int>")
async def crud_items_get(request, item_id):
    rds = request.app.ctx.redis
    key = f"item:{item_id}"
    if rds is not None:
        try:
            cached = await rds.get(key)
            if cached is not None: return json_resp(_json.loads(cached))
        except Exception: pass
    pool = request.app.ctx.db_pool
    if pool is None: return text("not found", status=404)
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_ITEM_COLS} FROM items WHERE id=$1", item_id)
    except Exception: return text("not found", status=404)
    if row is None: return text("not found", status=404)
    item = _row_to_item(row)
    if rds is not None:
        try: await rds.set(key, _json.dumps(item).encode(), ex=_CACHE_TTL)
        except Exception: pass
    return json_resp(item)

@app.put("/crud/items/<item_id:int>")
async def crud_items_update(request, item_id):
    pool = request.app.ctx.db_pool
    try: data = request.json
    except Exception: return text("invalid JSON", status=400)
    if not isinstance(data, dict): return text("invalid JSON", status=400)
    if pool is None: return text("not found", status=404)
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE items SET name=$2, price=$3, quantity=$4 WHERE id=$1",
                item_id, data["name"], int(data["price"]), int(data["quantity"]))
    except Exception: return text("not found", status=404)
    if result.rsplit(" ", 1)[-1] == "0": return text("not found", status=404)
    if request.app.ctx.redis:
        try: await request.app.ctx.redis.delete(f"item:{item_id}")
        except Exception: pass
    return json_resp({"id": item_id})

# ── Entry point ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--cert", default=None)
    p.add_argument("--key", default=None)
    args = p.parse_args()
    kw = dict(host=args.host, port=args.port, access_log=False, motd=False)
    if args.cert and args.key and os.path.exists(args.cert) and os.path.exists(args.key):
        # Sanic TLS in worker mode: neither obvious form works with HttpArena's
        # cert. A pre-built SSLContext isn't picklable to forked workers
        # ("cannot pickle 'SSLContext'"); the bare {"cert","key"} dict routes
        # through CertSimple, which decodes the cert's subjectAltName — which
        # HttpArena's server.crt lacks → KeyError. But CertSimple only decodes
        # the SAN when "names" is ABSENT, so supplying "names" (picklable dict,
        # rebuilt per worker) skips the decode and serves TLS.
        kw["ssl"] = {"cert": args.cert, "key": args.key, "names": ["localhost"]}
    if args.workers and args.workers > 1: kw["workers"] = args.workers
    else: kw["single_process"] = True
    app.run(**kw)

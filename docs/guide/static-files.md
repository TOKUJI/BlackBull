# Static files

`app.static(url_prefix, root_dir)` registers a `StaticFiles`
middleware that serves files from `root_dir` for paths starting
with `url_prefix`.  Useful for CSS, JS, images, fonts, and other
file-system assets that don't change on every request.

## Quick start

```python
from blackbull import BlackBull

app = BlackBull()
app.static('/assets', 'public/assets')
app.static('/images', 'public/images')

@app.route(path='/')
async def index(scope, receive, send):
    ...
```

A request to `/assets/style.css` is intercepted by the global
middleware before routing and served from
`public/assets/style.css`.  Requests that don't match the prefix
fall through to the route handlers normally.

## Standalone usage

`StaticFiles` is also a standalone ASGI app — useful in tests
or when mounting without BlackBull:

```python
from blackbull.middleware.static import StaticFiles

app = StaticFiles(directory='public')
# app(scope, receive, send)  — 3-argument ASGI
```

## Environment gate

The `BLACKBULL_ENV` environment variable controls serving
behaviour:

| Value | Effect |
|---|---|
| `production` | Always return 404 — static files are never served |
| `development` (default) | Serve files normally |
| `test` | Serve files normally |

```bash
BLACKBULL_ENV=production python app.py   # static routes return 404
BLACKBULL_ENV=development python app.py  # static files served
```

The production-mode passthrough exists because production
deployments typically front BlackBull with nginx or a CDN that
serves static assets directly, and you don't want the framework
double-serving the same files.

## How it serves files

### In-memory cache (opt-in)

`StaticFiles` supports an in-process body cache for fast cache-hit
serving.  It is **off by default**: every request does a fresh
`stat()` and reads the body from disk.  This matches the behaviour
of Starlette / FastAPI / Flask static serving and keeps BlackBull's
default behaviour compatible with standards that require "read
files from disk on every request, no in-memory caching" (e.g. the
HttpArena standard-mode static profile).

Opt in by passing `cache=True`:

```python
# Default — read from disk every request.
app.static('/assets', 'public/assets')

# Opt-in — small files (≤ 4 MiB each, up to 256 entries) held in
# process memory; stat() syscall throttled to once per second per
# entry; sibling-existence answers memoised across requests.
app.static('/assets', 'public/assets', cache=True)
```

When you should turn it on:

- BlackBull terminates static traffic directly (no nginx / no CDN
  in front).
- The asset set is small enough that all files fit in 256 × 4 MiB
  of process memory and small enough that the cache hit rate is
  high.
- You're OK with edit-on-disk visibility up to ~1 second behind
  (the stat-throttle window, override via `BB_STATIC_STAT_TTL_S`).

When to leave it off (the default):

- nginx / a CDN fronts the framework — they handle the static path
  far more efficiently than any in-process cache can.
- You want edit-on-disk visibility to be immediate.
- You're running the benchmark suites HttpArena's standard-mode
  rules describe.

The cache is per-process — multi-worker deployments hold a
separate cache in each worker.

For files above the cache threshold, `StaticFiles` streams the
body in chunks so peak per-request memory stays bounded
regardless of file size.

### Precompressed sibling serving

If a file `app.js` has a sibling on disk like `app.js.br`,
`app.js.zst`, or `app.js.gz`, `StaticFiles` will serve the
sibling (with the right `Content-Encoding` header) when the
client's `Accept-Encoding` allows it.  Preference order is
`br > zstd > gzip`.

```
public/
  app.js          # 50 KiB original
  app.js.br       # 12 KiB pre-compressed (served when Accept-Encoding: br)
  app.js.gz       # 17 KiB pre-compressed (served when Accept-Encoding: gzip)
```

This is the same pattern as nginx's
[`gzip_static`](https://nginx.org/en/docs/http/ngx_http_gzip_static_module.html)
/ `brotli_static` modules and ASP.NET's `MapStaticAssets`.
Generating the siblings is a build-time concern (CI script, asset
pipeline) — BlackBull does not produce them at runtime.

Range requests bypass sibling lookup — encoded bodies have a
different size than the original and serving a `Range` over an
encoded variant is messy.  Matches nginx's `gzip_static` +
`Range` behaviour.

`StaticFiles` always advertises `Vary: Accept-Encoding` on
responses where a precompressed sibling was selected, so HTTP
caches don't mis-cache an encoded body to a client that didn't
ask for it.

When `cache=True` is set, the per-path sibling-existence answer
is memoised after the first lookup (the file set is deterministic
for the lifetime of the server).  When `cache=False`, sibling
existence is rechecked on every request.

### Range requests (RFC 7233)

`StaticFiles` supports `Range` requests:

```
GET /assets/video.mp4 HTTP/1.1
Range: bytes=0-1023
```

Response:

```
HTTP/1.1 206 Partial Content
Content-Range: bytes 0-1023/4096000
Content-Length: 1024
```

Unsatisfiable ranges return `416 Range Not Satisfiable`.

## Security

- **Path traversal**: URL-encoded paths are decoded with
  `urllib.parse.unquote` before resolution.  Any resolved path
  that escapes the configured root directory returns
  `400 Bad Request`.
- **Directory listing**: Requests for bare directories return
  `404`; no directory listing is ever served.

## Inspecting registered roots

```python
app.static('/a', 'public/a')
app.static('/b', 'public/b')
print(app._static_roots)
# [('/a', PosixPath('/abs/public/a')), ('/b', PosixPath('/abs/public/b'))]
```

## Pairing with compression

The `StaticFiles` middleware does not compress responses itself.
To gzip / brotli / zstd static content on the fly, layer the
`Compression` middleware globally:

```python
from blackbull.middleware.compression import Compression

app.use(Compression())
app.static('/assets', 'public/assets')
```

Order matters — `Compression` registered before `app.static`
will see the static response and compress it.  For very large
files, prefer pre-compressing on disk and serving the matching
variant.

## Next

- [Middleware](middleware.md) — `Compression`, `CORS`, and the
  rest of the middleware surface.
- [Configuration](configuration.md) — `BLACKBULL_ENV` and other
  environment variables.

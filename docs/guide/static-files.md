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

### In-memory cache

Small files (default ≤ 4 MiB each, up to 256 entries) are cached
in process memory keyed on `(path, mtime, size)`.  Cache hits
serve directly — no disk I/O on the hot path.  When a file's
modification time or size changes on disk, the next request
re-reads it and replaces the cached copy.

The cache is per-process — multi-worker deployments hold a
separate cache in each worker.

For files above the cache threshold, `StaticFiles` streams the
body in chunks so peak per-request memory stays bounded
regardless of file size.

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

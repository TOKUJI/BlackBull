# Static files

`app.static(url_prefix, root_dir)` registers a **route** that serves
files from `root_dir` under `url_prefix`, with a `StaticFiles`
middleware attached to it.  Useful for CSS, JS, images, fonts, and
other file-system assets that don't change on every request.

## Quick start

```python
from blackbull import BlackBull

app = BlackBull()
app.static('/assets', 'public/assets')
app.static('/images', 'public/images')

@app.route(path='/')
async def index(conn, receive, send):
    ...
```

A request to `/assets/style.css` is routed to the static route and
served from `public/assets/style.css`.  Requests that don't match the
prefix are routed normally and never enter `StaticFiles` at all —
static serving costs a non-static request nothing, because the router
resolves it without ever consulting the static route.

Each call registers three entries: `<prefix>/{filepath:path}` for
files, plus `<prefix>` and `<prefix>/` so the [directory
index](#directory-index) can answer the mount root.  An explicit route
you registered yourself always wins — `app.static()` never replaces a
path you already claimed.

!!! note "A miss under the prefix is a 404"
    Because the static route has matched, `/assets/missing.css` is
    answered with 404 rather than continuing on to another route that
    might also match `/assets/...`.  The 404 goes through the normal
    error path, so `@app.on_error(HTTPStatus.NOT_FOUND)` still applies.

## Standalone usage

`StaticFiles` is also a standalone ASGI app — useful in tests
or when mounting without BlackBull:

```python
from blackbull.middleware.static import StaticFiles

app = StaticFiles(directory='public')
# app(scope, receive, send)  — 3-argument ASGI
```

## Directory index

By default `StaticFiles` serves files by their exact path only.
Pass `index` to serve a named file when a request resolves to a
directory:

```python
app.static('/', 'public', index='index.html')   # GET / → public/index.html
```

The index candidate is run through the same realpath + traversal
guard as any other target, so it can't escape the root.  The
`blackbull serve` CLI (below) turns this on by default.

## Zero-code static server: `blackbull serve`

For an ad-hoc static server with no application code — a drop-in
upgrade over `python -m http.server` — use the `serve` subcommand:

```bash
blackbull serve ./public --bind :8080
blackbull serve ./public --certfile cert.pem --keyfile key.pem  # HTTPS + HTTP/2
```

It wires `StaticFiles` with `index='index.html'`; ETag / `304`
conditional responses are served natively by `StaticFiles` (see
[Conditional requests](#conditional-requests-etag-last-modified) below)
and can be turned off with `--no-etag`.  See
[Configuration → blackbull serve](configuration.md#serving-static-files-with-blackbull-serve)
for the full flag list.

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

The body cache is off by default. Enable it only if retaining file bodies
in each worker's memory and delaying on-disk edit visibility is acceptable.

Opt in by passing `cache=True`:

```python
# Default — read from disk every request.
app.static('/assets', 'public/assets')

# Opt-in — small files (≤ 4 MiB each, up to 256 entries) held in
# process memory; cached-body validation throttled to once per second per
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

- A reverse proxy or CDN already serves the static paths.
- You want edit-on-disk visibility to be immediate.

The cache is per-process — multi-worker deployments hold a
separate cache in each worker.

For files above the cache threshold, `StaticFiles` streams the
body in chunks so peak per-request memory stays bounded
regardless of file size.

### Precompressed sibling serving

If a file `app.js` has a sibling on disk like `app.js.br`,
`app.js.zst`, or `app.js.gz`, `StaticFiles` will serve the
sibling (with the right `Content-Encoding` header) when the
client's `Accept-Encoding` allows it, using the same
[acceptance rules as dynamic compression](middleware.md#compression-compress).
Preference order is `br > zstd > gzip`.

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

If the requested end is at or beyond the file's length, it is clipped to
the last byte; the response remains `206 Partial Content`, with
`Content-Range` and `Content-Length` describing the bytes actually served
(RFC 9110 §14.1.2).

An **unsatisfiable** range (start at or past the end of the
file) returns `416 Range Not Satisfiable`.  A **malformed** or
unsupported `Range` header — a bad byte spec (`bytes=abc-def`), a
non-`bytes` unit, or a multi-range set — is ignored and the full file
is served with `200 OK` (RFC 9110 §14.2); it never fails the request.

### Conditional requests (ETag / Last-Modified)

`StaticFiles` emits a strong `ETag` (derived from the file's
modification time and size) and a `Last-Modified` header on every
response.  A revalidating client can then avoid re-downloading an
unchanged asset:

```
GET /assets/app.css HTTP/1.1
If-None-Match: "18f3a-1c2"
```

```
HTTP/1.1 304 Not Modified
ETag: "18f3a-1c2"
Last-Modified: Sun, 13 Jul 2026 00:00:00 GMT
```

`If-None-Match` takes precedence over `If-Modified-Since` (RFC 9110
§13); the `304` is answered before the file body is read, so a
revalidation of a large asset costs no disk I/O.  Pass
`conditional=False` to `app.static(...)` (or `blackbull serve
--no-etag`) to suppress the validators and always serve the full body.

## Security

- **Path traversal**: URL-encoded paths are decoded with
  `urllib.parse.unquote` before resolution.  Any resolved path
  that escapes the configured root directory returns
  `400 Bad Request`.
- **Directory listing**: Requests for bare directories return
  `404`; no directory listing is ever served.
- **Precompressed variants**: the check applies to the file finally
  selected — original, index or a `.br` / `.zst` / `.gz` sibling.  A
  sibling symlink resolving outside the root answers `400` before it is
  cached, opened or sent; symlinks resolving inside keep serving.  It
  re-runs every request (a swap between requests is refused on the next
  one) but not within one request — do not leave the served tree writable
  by untrusted users.
- **A selected variant that escapes loses the whole request**: the
  refusal is `400` even when the original or another variant would
  have served — an outward-symlinked `file.gz` with `Accept-Encoding:
  gzip` refuses although `file` itself is fine.  There is no fallback
  to the next candidate.
- **A variant is its filename**: a hard link of the original named
  `file.gz` is served as `Content-Encoding: gzip` although its bytes
  are plain.  Identity is never measured (`st_nlink` would not make it
  robust) — keep the served tree's names honest.  Hard links are not
  detected at all: a hard link inside the root serves whatever file it
  links to, even outside the root — keep the served tree free of links
  you did not create.

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

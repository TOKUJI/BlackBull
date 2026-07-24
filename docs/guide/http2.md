# HTTP/2

BlackBull speaks HTTP/2 natively.  ALPN negotiates `h2` on the
same TLS listener that serves HTTP/1.1; cleartext h2c is detected
on first preface bytes (no separate port required, no upgrade
dance).  This page covers what the framework does with HTTP/2's
extra knobs and what your app code can do with them.

## Activating HTTP/2

```python
app.run(port=8443, certfile='cert.pem', keyfile='key.pem')
```

ALPN advertises both `h2` and `http/1.1`.  Whichever the client
picks is what gets used.  Browsers and modern HTTP clients
(curl with `--http2`, httpx with `http2=True`, h2load) negotiate
`h2`; older clients negotiate `http/1.1` and continue working
unchanged.

For cleartext h2c (no TLS, useful inside a private network behind
a load balancer that terminates TLS), bind a plain port and let
the protocol detector sniff the connection preface:

```python
app.run(port=8080)   # serves both HTTP/1.1 and h2c on the same socket
```

## Priority hints

HTTP/2 lets a client tell the server which of its outstanding
requests is more important.  BlackBull surfaces the hint on the
scope so your app can act on it.

### What lands on `scope`

Every HTTP/2 request scope advertises the priority hint via the
`http.response.priority` ASGI extension (matches gunicorn's beta
HTTP/2 key shape; the *contents* are RFC 9218 urgency/incremental,
not the deprecated RFC 7540 weight/tree):

```python
scope['extensions']['http.response.priority']
# → {'urgency': int, 'incremental': bool}
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `urgency` | `int` 0–7 | `3` | 0 = most urgent, 7 = least urgent |
| `incremental` | `bool` | `False` | Client accepts interleaved partial responses |

BlackBull resolves the value in this order (first wins):

1. `PRIORITY_UPDATE` frame (RFC 9218, type `0x10`) received from
   the client — either before or after the HEADERS frame.
2. `priority` HTTP header in the request (e.g. `priority: u=1, i`).
3. RFC 9218 §4.1 defaults: `urgency=3`, `incremental=False`.

For HTTP/1.1 requests the extension key is absent.  Always use
`.get()` with a default so your handler works across both protocols.

### Using it in a handler

```python
_DEFAULT_PRIORITY = {'urgency': 3, 'incremental': False}

@app.route(path='/search')
async def search(scope, receive, send):
    ext = scope.get('extensions') or {}
    hint = ext.get('http.response.priority', _DEFAULT_PRIORITY)
    if hint['urgency'] <= 2:
        # High-urgency: return cached / pre-computed result immediately
        result = get_cached_result()
    else:
        # Normal / background: run the full search
        result = await run_full_search()
    await send(JSONResponse(result))
```

### Across every route, via an interceptor

```python
@app.intercept('before_handler')
async def handle_priority(event):
    scope = event.detail['conn']
    ext = scope.get('extensions') or {}
    hint = ext.get('http.response.priority', {'urgency': 3, 'incremental': False})
    if hint['urgency'] <= 1:
        logger.info('HIGH-PRIORITY u=%d: %s %s',
                    hint['urgency'], event.detail['method'], event.detail['path'])
```

### Migrating from `scope['http2_priority']` (pre-v0.31)

Earlier BlackBull releases exposed priority as a top-level scope
key, `scope['http2_priority']`.  v0.31 moved it under
`scope['extensions']` to match ASGI conventions and align the key
name with gunicorn's beta HTTP/2 surface.  The legacy key is still
populated alongside the new extension during the v0.31 cycle and
is scheduled for removal in v0.32.0.

To migrate, replace:

```python
hint = scope.get('http2_priority', DEFAULT)            # legacy
```

with:

```python
ext = scope.get('extensions') or {}
hint = ext.get('http.response.priority', DEFAULT)       # v0.31+
```

The dict shape (`{'urgency': int, 'incremental': bool}`) is
unchanged.

### HTTP/2 stream info (gRPC foundation)

Alongside the priority extension, v0.31 adds
`scope['extensions']['http.response.http2_stream']` — a snapshot of
the HTTP/2 stream identity and send-flow-control credit at request
entry:

```python
scope['extensions']['http.response.http2_stream']
# → {'stream_id': int, 'send_window_remaining': int,
#    'connection_send_window_remaining': int}
```

| Key | Meaning |
|---|---|
| `stream_id` | The HTTP/2 stream ID this request arrived on (odd for client-initiated, even for server pushes) |
| `send_window_remaining` | Bytes the peer will currently accept on this stream before WINDOW_UPDATE |
| `connection_send_window_remaining` | Same, at the connection level |

The window values are snapshots taken at scope-build time; they
move as the response body streams.  Applications that need
live readings (e.g. gRPC server-streaming back-pressure) can
re-read the dict, though they will see the snapshot value unless
they keep a reference and the populate site re-fetches it — which
v0.31 does not do.  Live properties are a future-sprint
consideration.

Peer-side receive-window is intentionally absent: BlackBull sends
`WINDOW_UPDATE` per consumed DATA frame, so there is no scalar to
snapshot.

See [`examples/PriorityExample/`](https://github.com/TOKUJI/BlackBull/tree/master/examples/PriorityExample/)
for a dedicated server + client pair that demonstrates both
sources (`PRIORITY_UPDATE` frame and `priority` header).

### Testing priority with curl

```bash
# Default priority (urgency=3)
curl --http2 -k https://localhost:8443/priority-echo

# High urgency
curl --http2 -k -H 'priority: u=1' https://localhost:8443/priority-echo

# Background prefetch, incremental
curl --http2 -k -H 'priority: u=6, i' https://localhost:8443/priority-echo
```

curl converts the `priority` header to a `PRIORITY_UPDATE` frame
when talking HTTP/2 over TLS.  httpx (Python) sends it as a plain
header; BlackBull parses both forms.

### What BlackBull does *not* do with priority

BlackBull's stance is **receive but do not schedule**.  Priority
signals are accepted, logged at `DEBUG`, and made available on
the scope — but the framework does not reorder responses based on
them.  Your application decides what to do with the hint.

This is valid per RFC 9218 §2: "A server that does not implement
prioritization MUST ignore this frame."  The framework reports
the hint to you; you choose whether and how to honour it.

## Server push

Server push lets the server proactively send a resource to the
client before the client asks for it.  A typical use case is
pushing a CSS file alongside the HTML page that references it,
saving one round-trip.

### Triggering a push

The app signals a push by calling `send` with an
`http.response.push` event before the final `http.response.body`:

```python
@app.route(path='/')
async def index(scope, receive, send):
    # Push a stylesheet before sending the HTML.
    await send({
        'type': 'http.response.push',
        'path': '/static/style.css',
        'headers': [],
    })
    await send({'type': 'http.response.start', 'status': 200,
                'headers': [(b'content-type', b'text/html')]})
    await send({'type': 'http.response.body',
                'body': b'<html>...</html>'})
```

`path` must be a plain string (percent-encoding decoded).
Pseudo-headers (`:method`, `:scheme`, `:authority`) are filled in
automatically — do not include them in `headers`.

### What the framework does

1. Allocates the next even stream ID for the pushed resource
   (RFC 7540 §5.1.1 — server-initiated streams are always even:
   2, 4, 6, …).
2. Sends a `PUSH_PROMISE` frame on the parent stream.  The frame
   contains the synthetic request headers (`GET /static/style.css`)
   the client can use to match its cache.
3. Creates a synthetic scope (`type='http'`, `method='GET'`,
   `path='/static/style.css'`) and dispatches it to your app as
   a new task on the promised stream.  Your app handles the
   pushed request exactly like a normal GET — same route, same
   middleware, same response cycle.

### Checking for support

The server advertises push support in `scope['extensions']`:

```python
if 'http.response.push' in scope.get('extensions', {}):
    await send({'type': 'http.response.push',
                'path': '/logo.png', 'headers': []})
```

For HTTP/1.1 requests `scope['extensions']` does not contain
`'http.response.push'`, so the guard above keeps the same handler
working on both protocols.

### Caveats

- Clients can disable server push by sending
  `SETTINGS_ENABLE_PUSH=0`.  BlackBull does not currently check
  this setting; if the client rejects the push it will send a
  `RST_STREAM`, which BlackBull logs and ignores.
- Pushed resources should be cacheable.  Pushing non-cacheable
  content wastes bandwidth and may confuse browsers.

## Flow control

HTTP/2 has two independent flow-control windows — connection-level
(shared across all streams on one connection) and stream-level
(per-stream budget).  BlackBull handles both internally; no app
code needs to interact with flow control directly.

Configuration knobs that affect flow-control behaviour live in
the `BB_H2_*` family of environment variables — see
[Configuration](configuration.md) for the full list.

## WebSocket over HTTP/2

WebSocket can be carried over HTTP/2 via RFC 8441 (Extended
CONNECT).  Off by default; opt in via
`BB_H2_ENABLE_WEBSOCKET=1`.  See
[WebSockets — Transport](websockets.md#transport-http11-upgrade-vs-http2-extended-connect)
for the details.

## Next

- [WebSockets](websockets.md) — the WebSocket transport,
  including HTTP/2 carriage.
- [Configuration](configuration.md) — `BB_H2_*` flow-control,
  concurrency, and window-size environment variables.
- [Internals](../about/internals.md) — the HTTP/2 actor model,
  stream state machine, and frame parser, for readers who want
  to know what the framework is doing on the wire.

# Hosting a foreign ASGI app

BlackBull can serve an ASGI 3.0 application through [`ASGIApp`][blackbull.ASGIApp].
This page covers a foreign app on BlackBull's server. To run a BlackBull app
under another server, see [Running BlackBull](../deployment/running.md).

## Entry points

The CLI wraps a foreign callable automatically:

```bash
blackbull myproject:app --bind 127.0.0.1:8000
```

For Python embedding, use `Server(foreign_app, asgi=True)` or construct the
adapter explicitly:

```python
from blackbull import ASGIApp
from blackbull.server import Server

adapter = ASGIApp(foreign_app)
server = Server(adapter)
```

`ASGIApp` is a native BlackBull callable, not an app to pass to another ASGI
host. `Server(foreign_app)` alone does not infer ASGI from its signature.
It serves HTTP and WebSocket only: MQTT and other raw protocols are added to a
BlackBull app with `add_extension`, which `ASGIApp` does not have.
`BB_FORCE_ASGI_SCOPE=1` also selects the adapter; combining it with
`Server(..., asgi=False)` raises `ConfigurationError`.

## Mounting inside BlackBull

```python
from blackbull import ASGIApp, BlackBull

app = BlackBull()
foreign = ASGIApp(foreign_app, mounted=True)
app.mount('/api', foreign)
```

Register before startup. The mounted adapter belongs to one host; it cannot
be served directly or registered with `route()`. It must wrap a foreign ASGI
app, rather than BlackBull or a wrapper exposing BlackBull via `__wrapped__`.

The prefix and its descendants accept every HTTP method, including extension
methods. More specific host routes take precedence. The foreign scope keeps
the original `path`, `raw_path` and query string; `root_path` adds the prefix
(`/api` above). Mounting at `/` adds an empty prefix. The host `Connection`
is not rewritten, including on exceptions or cancellation.

Host middleware applies and host listeners own the request events; listeners
on the mounted adapter do not fire. Registering the same adapter at several
prefixes runs its lifespan once per host lifecycle and shares its lifespan
state. Startup failure reclaims already-started mounted lifespans.

`enable_grpc()` intercepts gRPC requests before HTTP routing, including mounts.
To serve gRPC from a foreign app, let that app own gRPC instead of enabling
the host's registry. The foreign app supplies its own API schema; host
`get_routes()` and OpenAPI generation do not enumerate its internal routes.

## Current compatibility

These are transport capabilities, not certification of every ASGI framework
or middleware package. Check the application's required extensions before
deploying it.

| Capability | HTTP/1.1 | HTTP/1.1 + TLS | HTTP/2 prior knowledge | HTTP/2 + TLS/ALPN |
|---|---|---|---|---|
| HTTP requests and streamed responses | Yes | Yes | Yes | Yes |
| Response trailers (`trailers=True`) | Chunked | Chunked | HEADERS | HEADERS |
| `http.response.pathsend` advertised | Yes | No | No | No |
| `http.response.push` advertised | No | No | Only if peer permits | Only if peer permits |
| WebSockets and `websocket.http.response` | Yes | Yes | With Extended CONNECT enabled | With Extended CONNECT enabled |

Use `scope['extensions']` for the current request, rather than inferring
extensions from the protocol alone. H2 also supplies `http.response.priority`
and `http.response.http2_stream`; the latter's flow-control values are a
snapshot, not a live credit balance. See [HTTP/2](http2.md).
For file responses without `pathsend`, send ordinary `http.response.body`
messages. Do not send an extension message merely because another ASGI host
supports it.

When the foreign app is served directly, closing or raising before acceptance
returns HTTP 403. To choose the rejection status, headers and body, use the
[`websocket.http.response` extension](https://asgi.readthedocs.io/en/latest/extensions.html#websocket-denial-response)
before accepting. An HTTP denial closes the HTTP/1.1 connection or ends the
HTTP/2 stream. Mounts serve HTTP only.

## Lifespan and request state

The server runs the app's lifespan once per worker's server lifecycle, before
accepting requests and during shutdown. `ASGIApp(app, lifespan=False)` skips it.
Each HTTP request and WebSocket session receives a shallow copy of the lifespan `scope['state']`:
changing a top-level key is local to that request; referenced objects remain
shared.

An app that exits without answering `lifespan.startup`, or raises before its
first lifespan `receive()`, is treated as not supporting lifespan. An explicit
`lifespan.startup.failed` aborts startup. An exception after receiving startup
but before sending `lifespan.startup.complete` also aborts it.
See [Listeners](listeners.md) for startup and cleanup ownership.

## HTTP errors and disconnects

- An exception or return before `http.response.start` produces `500`.
- An exception or return before a started response completes ends the
  exchange; a replacement `500` is not sent. H1 ends the connection; H2 ends
  the affected stream.
- An exception after the response completes does not replace it or force
  the connection to close.
- Out-of-order HTTP messages raise `RuntimeError` in the app. A send after a
  detected disconnect raises an `OSError` subclass.
- After the request body, H1 `receive()` does not independently monitor the
  socket while the app answers. A disconnect may be learned through sending
  or the request timeout; do not rely on immediate disconnect notification
  from a receive-only listener. H2 can report a peer stream reset to `receive()`.

Use application deadlines as well as the server's
[request and write limits](configuration.md).

## Events and middleware

Register BlackBull observers/interceptors on the adapter, for example
`@adapter.on('request_completed')`; see [Events](events.md).
An interceptor exception can prevent the foreign app from running and produce
`500`.

An adapter around a BlackBull app, or a wrapper exposing it through
`__wrapped__`, shares that app's dispatcher. An ordinary class-based ASGI
middleware wrapper does not expose the inner app this way: actor-layer
events such as `connection_accepted` go to the adapter's dispatcher, not to
the inner `app.on` listeners. Register transport observers on the object
actually passed to `Server`.


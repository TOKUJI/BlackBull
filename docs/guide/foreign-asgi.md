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
`BB_FORCE_ASGI_SCOPE=1` also selects the adapter; combining it with
`Server(..., asgi=False)` raises `ConfigurationError`.

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
| Foreign app WebSockets | Unavailable | Unavailable | Unavailable | Unavailable |

Use `scope['extensions']` for the current request, rather than inferring
extensions from the protocol alone. H2 also supplies `http.response.priority`
and `http.response.http2_stream`; the latter's flow-control values are a
snapshot, not a live credit balance. See [HTTP/2](http2.md).
For file responses without `pathsend`, send ordinary `http.response.body`
messages. Do not send an extension message merely because another ASGI host
supports it.

Foreign WebSocket requests end without calling the foreign app; a specific
HTTP rejection status is not guaranteed. WebSocket support and pre-upgrade
HTTP responses are tracked in BLA-676. Mounting an `ASGIApp` under a BlackBull
route is tracked in BLA-674. Neither is a currently supported integration.
The native [WebSocket API](websockets.md) is a separate supported path.

## Lifespan and request state

The server runs the app's lifespan once per worker's server lifecycle, before
accepting requests and during shutdown. `ASGIApp(app, lifespan=False)` skips it.
Each HTTP request receives a shallow copy of the lifespan `scope['state']`:
changing a top-level key is local to that request; referenced objects remain
shared.

An app that exits without answering `lifespan.startup`, or raises before its
first lifespan `receive()`, is treated as not supporting lifespan. An explicit
`lifespan.startup.failed` aborts startup. An exception after receiving startup
but before sending `lifespan.startup.complete` also aborts it.
See [Listeners](listeners.md) for startup and cleanup ownership.

## Errors and disconnects

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


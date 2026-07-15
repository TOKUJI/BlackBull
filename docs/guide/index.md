# Guide

The Guide covers everything you'd build a BlackBull app with —
routing, middleware, request/response handling, WebSockets,
HTTP/2, error handling, configuration, and more.

If you're new, read [Getting Started](../getting-started/installation.md)
first — it covers installation and the shape of a minimal app.

## Topics

- [**Routing**](routing.md) — `@app.route`, path parameters
  (string, regex, typed), route groups, WebSocket route
  registration.
- [**Middleware**](middleware.md) — writing middleware,
  `@as_middleware`, attaching to routes, built-in middleware
  (`websocket`, `Compression`, `Cache`, `CORS`,
  `StaticFiles`), common recipes.
- [**Error handling**](error-handling.md) — `@app.on_error`,
  the DEV-mode traceback page, PROD-mode terse responses.
- [**Requests and responses**](requests-and-responses.md) —
  reading the body and headers, cookies, query parameters,
  forms; `Response` / `JSONResponse` / `StreamingResponse`;
  detecting client disconnection.
- [**WebSockets**](websockets.md) — the WebSocket route, the
  `websocket` middleware, `permessage-deflate`, RFC 8441
  (WebSocket over HTTP/2), subprotocols, fragmented messages.
- [**HTTP/2**](http2.md) — ALPN, priority hints,
  `http.response.push`, flow control, h2c.
- [**Events**](events.md) — `@app.on` (observation) and
  `@app.intercept` (interception) for lifecycle hooks.
- [**Logging**](logging.md) — access log, framework log,
  `@log` decorator, forwarding logs to a remote server.
- [**Static files**](static-files.md) — `app.static`,
  in-memory cache, range requests, production passthrough.
- [**Configuration**](configuration.md) — environment
  variables, TOML config file, CLI flags, precedence.
- [**OpenAPI**](openapi.md) — `app.enable_openapi`,
  Swagger UI, dataclass schemas, body deserialization.
- [**Testing**](testing.md) — BlackBull's clients on an
  ephemeral port, `httpx.ASGITransport`, direct handler tests.

## Deploying

Once you have something working, the [Deployment](../deployment/running.md)
chapter covers running it under multiple workers, behind nginx,
with TLS, on an `AF_UNIX` socket, with systemd socket
activation, and with hot-reload during development.

## Reference

- [Environment variables](../reference/env-vars.md) — the full
  `BB_*` table.
- API reference (auto-generated from source) — see the API
  section in the navigation.

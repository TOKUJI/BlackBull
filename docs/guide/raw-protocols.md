# Non-ASGI protocols (raw TCP)

Most BlackBull traffic is HTTP/1.1, HTTP/2, or WebSocket — protocols that map
cleanly onto the ASGI `(scope, receive, send)` contract. Some protocols do not:
MQTT, Redis RESP, a raw line protocol, the PostgreSQL wire protocol. They have no
method, path, status, or headers, so forcing them through ASGI would be a leaky
abstraction.

The **Non-ASGI bridge** lets such a protocol run inside the same BlackBull
process, sharing the connection machinery and the event system, while speaking
the socket directly. You register an async handler with `raw_handler` and give it
a port:

```python
from blackbull import BlackBull

app = BlackBull()

@app.route(path='/')
async def index():
    return "HTTP here; raw TCP echo on :9000."

@app.raw_handler('echo', port=9000)
async def echo(reader, writer, ctx):
    while True:
        data = await reader.read(1024)
        if not data:
            break
        await writer.write(data)

app.run(port=8000)   # HTTP on 8000, echo on 9000
```

A full runnable version is in `examples/echo_tcp.py`.

## The handler contract

A raw handler is an async callable `(reader, writer, ctx)` that **owns the
connection for its entire lifetime** — it decides when to stop. This is
deliberately unlike HTTP's request-per-connection model; raw protocols are
typically long-lived and stateful.

- `reader` is an `AbstractReader`: `await reader.read(n)`,
  `await reader.readexactly(n)`, `await reader.readuntil(sep)`.
- `writer` is an `AbstractWriter`: `await writer.write(data)`,
  `await writer.close()`.
- `ctx` is a `ProtocolContext` carrying `peername`, `sockname`, `ssl`,
  `connection_id` (a uuid4 hex for log correlation), `protocol` (the registered
  name), and `aggregator` (for emitting events — see below).

When the handler returns, or raises, BlackBull closes the connection. A raised
exception is reported through the `error` event and otherwise isolated — one bad
connection never affects another.

`register_protocol_handler(name, handler, *, port=...)` is the non-decorator
form of `raw_handler` and behaves identically.

## How dispatch works

BlackBull routes every accepted connection through a single **protocol
registry**. `http1` and `http2` are built-in registry bindings on the shared
HTTP listener; a `raw_handler` adds a binding on its own dedicated port.
Connections arriving on a raw protocol's port skip HTTP detection entirely and
go straight to the handler. WebSocket is unaffected — it is still reached through
an HTTP `Upgrade` on the HTTP listener, not as a top-level protocol.

The bridge is **dormant by default**: an app with no `raw_handler` binds only the
HTTP port and runs no raw code paths.

## Core protocols vs. bridge protocols

BlackBull distinguishes two tiers of protocol, and the distinction is
deliberate:

- **Core protocols — the HTTP family.** HTTP/1.1, HTTP/2, and (as they land)
  gRPC and HTTP/3. These *are* the framework: they share the from-scratch HTTP
  stack BlackBull exists to implement. gRPC is HTTP/2 + protobuf framing and
  HTTP/3 is HTTP over QUIC, so both reuse the core stream machinery rather than
  introducing a foreign transport. They live in the core (`blackbull.protocol`,
  `blackbull.server`).
- **Bridge protocols — everything else.** MQTT (and, in principle, Redis RESP,
  AMQP, …) are independent protocol families that ride the Non-ASGI bridge but
  share none of the HTTP protocol logic. They are wired in as
  [extensions](extensions.md) and live in their own subpackages
  (`blackbull.mqtt`), kept opt-in via an extra (`pip install 'blackbull[mqtt]'`).
  A bridge protocol is structured so it can be extracted to a standalone
  `blackbull-<name>` distribution later without touching the core — the same
  path `blackbull-session` took.

The MQTT broker is the reference bridge protocol; see [MQTT broker](mqtt.md).

## Events

Raw protocols emit the protocol-agnostic Level B events (subscribe with
`@app.on`):

| Event | Fires when | Detail |
|-------|-----------|--------|
| `connection_accepted` | a connection is accepted | `peername`, `protocol` |
| `connection_closed` | the connection closes | `peername`, `protocol`, `duration_ms` |
| `message_received` | the handler reports an inbound message | `protocol`, `message_type`, `payload_size` |
| `message_sent` | the handler reports an outbound message | `protocol`, `message_type`, `payload_size` |
| `error` | the handler raises | `scope`, `exception` |

`message_received` / `message_sent` are emitted by the handler itself when it has
parsed an application-level message, via the aggregator on `ctx`:

```python
if ctx.aggregator is not None:
    await ctx.aggregator.on_message_received('echo', 'data', len(data))
```

`connection_accepted` and `connection_closed` fire automatically.

## Limitations (Sprint 50)

- **Cleartext only.** TLS termination for raw protocols is not yet wired up.
- **Single worker.** Port-bound protocols are served only when `workers=1`;
  `app.run()` with a `raw_handler` forces single-worker automatically.
- **Port-based routing only.** Sharing one port between HTTP and a raw protocol
  (first-byte sniffing) is planned for a later release.

## A note on the server class

The server class that drives all this is `blackbull.server.Server` (it serves
both ASGI and non-ASGI listeners). Its previous name, `ASGIServer`, remains as a
backward-compatible alias, so existing
`from blackbull.server import ASGIServer` imports keep working.

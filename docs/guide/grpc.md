# gRPC

BlackBull serves **gRPC** — all four RPC shapes: unary, server-streaming,
client-streaming, and bidirectional — over its own pure-Python HTTP/2 layer.
gRPC is just HTTP/2 with `content-type: application/grpc` and a length-prefixed
message body, where the call result is reported in `grpc-status` trailers — so a
gRPC service multiplexes onto the **same port** as your REST and WebSocket
traffic, in one process, with no C extensions and nothing to compile on ARM.

```python
from blackbull import BlackBull
from blackbull.grpc import GrpcServiceRegistry, GrpcStatus, GrpcError

app = BlackBull()
grpc = GrpcServiceRegistry()


@grpc.method('/helloworld.Greeter/SayHello')
async def say_hello(request: bytes, context) -> bytes:
    # `request` is the raw protobuf message body; deserialise it however you
    # like (this example just echoes bytes back).
    if not request:
        context.abort(GrpcStatus.INVALID_ARGUMENT, 'empty request')
    return b'Hello, ' + request


app.enable_grpc(grpc)

# REST on the same port — unaffected
@app.route(path='/healthz')
async def healthz():
    return {'ok': True}

app.run(port=8443, cert='cert.pem', key='key.pem')   # gRPC needs HTTP/2 (TLS+ALPN)
```

## The handler contract

A gRPC handler is a coroutine:

```python
async def handler(request: bytes, context: GrpcContext) -> bytes
```

- `request` — the de-framed request message bytes (a single message for unary
  calls). Deserialise with your protobuf classes:
  `MyRequest.FromString(request)`.
- return value — the response message bytes: `my_response.SerializeToString()`.
- `context` — per-call context (see below).

**Protobuf is not a dependency.** Handlers exchange raw bytes, so you choose
your own serialisation — `grpc_tools.protoc`-generated classes, the `protobuf`
package, or hand-rolled. The `pip install 'blackbull[grpc]'` extra reserves the
name but pulls in nothing; add `protobuf` yourself if you want generated
message classes.

### Server-streaming

A **server-streaming** handler takes one request and returns many responses. Write
it as an async generator that `yield`s each response message:

```python
@grpc.method('/pkg.Svc/StreamItems')
async def stream_items(request: bytes, context):
    for item in load_items(request):
        yield item.SerializeToString()
```

The registry detects the async-generator form automatically — no flag needed.
(If a decorator hides the generator nature of your handler, force it with
`grpc.method(path, streaming=True)` or `add_method(path, fn, streaming=True)`.)

Semantics that match the gRPC spec:

- The status rides the trailing HEADERS frame after the last message. A handler
  that fails **after** emitting messages reports its status in the trailers; one
  that fails **before** the first message produces a clean Trailers-Only error,
  exactly like a unary failure.
- `context.set_trailing_metadata(...)` / `set_code(...)` apply to the final
  trailers.
- The generator is always finalised (its `finally`/cleanup runs) when the client
  cancels or disconnects mid-stream, so long streams stop producing promptly.
- A `grpc-timeout` deadline bounds the **whole** stream (`DEADLINE_EXCEEDED` on
  expiry).

## Client-streaming and bidirectional RPCs

A handler whose first parameter is named `request_iter` (also accepted:
`request_iterator`, `requests`, `request_stream`) receives the request as an
**async iterator** of message bytes instead of one buffered `bytes` value —
messages are delivered as they arrive on the wire. Pass
`client_streaming=True` to override the name-based detection:

```python
@grpc.method('/pkg.Svc/Sum')
async def sum_lengths(request_iter, context) -> bytes:   # client-streaming
    total = 0
    async for msg in request_iter:
        total += len(msg)
    return str(total).encode()

@grpc.method('/pkg.Svc/Echo')
async def echo(request_iter, context):                   # bidirectional
    async for msg in request_iter:
        yield msg
```

The response axis is detected automatically (a plain coroutine returns one
message; an async generator streams), so the four gRPC shapes — unary,
server-streaming, client-streaming, and bidirectional — all register through
the same decorator.

## The call context

`GrpcContext` carries request metadata and lets the handler shape the outcome:

| Member | Purpose |
|---|---|
| `context.metadata(name, default=b'')` | Read a request header (call metadata). |
| `context.abort(status, details='')` | End the call immediately with a non-OK `GrpcStatus`. |
| `context.set_code(status)` / `context.set_details(str)` | Set the status reported in the trailers (defaults to `OK`). |
| `context.set_trailing_metadata([(b'k', b'v')])` | Add custom trailing metadata. |

Raising `GrpcError(status, details)` anywhere in the handler has the same effect
as `context.abort`. Any other unexpected exception becomes
`GrpcStatus.INTERNAL` — a handler bug never tears down the stream.

## Registering services

```python
grpc = GrpcServiceRegistry()

# One method at a time, decorator or call form:
@grpc.method('/pkg.Svc/Method')
async def method(request, context): ...

grpc.add_method('/pkg.Svc/Other', other_handler)

# Or a whole service at once:
grpc.add_service('helloworld.Greeter', {'SayHello': say_hello})
```

Paths are the standard gRPC `/package.Service/Method` form (a leading slash is
optional). Unknown methods return `GrpcStatus.UNIMPLEMENTED`.

## Scope and limits

- **All four RPC shapes are served**: unary, server-streaming,
  client-streaming, and bidirectional (see above).
- **gzip message compression** is supported (`grpc-encoding: gzip` /
  `identity`); a compressed request in any other coding is rejected with
  `UNIMPLEMENTED`, and the response advertises
  `grpc-accept-encoding: identity,gzip` so the client can retry.
- **HTTP/2 required.** Run with TLS + ALPN (`h2`) for real clients; the gRPC
  request must arrive over the HTTP/2 layer.
- BlackBull is **server-side**. For a client, use the standard `grpcio` client
  against your service.

See `blackbull.grpc` for the codec (`encode_message` / `decode_messages`) and
status codes (`GrpcStatus`).

## How it fits the architecture

gRPC is served through BlackBull's existing ASGI bridge rather than a dedicated
protocol Actor — each call still runs in its own isolated HTTP/2 `StreamActor`
task, but the gRPC logic lives at the application layer. For the full design
rationale (and why streaming RPCs may revisit it), see the
[gRPC assessment](../about/grpc-assessment.md#design-decision-grpc-rides-the-asgi-bridge-not-a-new-actor)
and [Internals](../about/internals.md#grpc-no-dedicated-actor).

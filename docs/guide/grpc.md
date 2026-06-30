# gRPC

BlackBull serves **unary gRPC** calls over its own pure-Python HTTP/2 layer.
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

- **Unary RPCs only** in this first cut. The wire path for streaming (DATA
  frames + trailers) is already in place; streaming handler types are a
  follow-up.
- **No message compression** — a request with the compressed flag set is
  rejected with `UNIMPLEMENTED`.
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

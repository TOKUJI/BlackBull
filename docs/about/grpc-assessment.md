# gRPC design

## Design decision: gRPC rides the ASGI bridge, not a new Actor

gRPC uses HTTP/2 stream lifetime, flow control and trailers. Do not add a
connection dispatcher or duplicate those mechanisms in the gRPC layer.
Each call owns isolated stream work, not the connection.

Keep message framing, compression, metadata, status and deadlines in
`blackbull.grpc`. Handlers exchange bytes; protobuf adapters stay optional.

## Streaming and cancellation

Unary, server-streaming, client-streaming and bidirectional calls share the
same transport. Await its send path for backpressure; diagnostic window
snapshots are not an admission mechanism.

Cancellation must finalize response generators and release per-call work.
`grpc-timeout` bounds the whole call, including streaming. Failures after the
first message still need status trailers.

Read `blackbull/grpc/asgi.py` for call lifetime and `registry.py` for handler
classification. Public contracts are in the [gRPC guide](../guide/grpc.md);
external-client checks live in `tests/conformance/grpc/`.

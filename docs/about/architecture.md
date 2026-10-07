# Architecture

## Protocol ownership

Keep HTTP/1.1, HTTP/2, WebSocket, gRPC and MQTT wire handling in BlackBull's
Python code. Do not replace it with a third-party protocol state machine.
HPACK uses the pure-Python `hpack` codec; standard-library C extensions and
optional `uvloop` do not change protocol ownership.

## Multi-protocol, one process

All protocols share one runtime. Attach non-HTTP protocols through
`app.add_extension(...)` and select sockets through
[listeners](../guide/listeners.md). Protocol dispatch belongs to bindings,
not application routing or a second server runtime.

## Actor model

Mutable protocol state has one owner. Coordinate concurrent work through
messages and explicit lifetime boundaries, not shared locks. MQTT routing
and socket output have separate serial inbox owners. HTTP/2 streams share
connection-level framing, HPACK and flow control while retaining isolated
request work. See [Internals](internals.md) and
[MQTT broker design](mqtt-actor-design.md).

## Fault injection

Fault servers assemble their own wire bytes. They must not reuse production
serializers, because a shared serializer cannot expose its own defects.
Use the [fault-injection guide](../guide/fault_injection.md) for scenarios
and differential oracles.

## Conformance

Use [conformance workflows](conformance.md) and their artifacts to check a
change. A finite suite establishes its cases, not complete correctness.
Keep measurements and run records under `bench/`.

## What BlackBull defers

Before extending the public surface, check
[Known limitations](https://github.com/TOKUJI/BlackBull/blob/master/KNOWN_LIMITATIONS.md)
and the [use-case guide](../getting-started/why-blackbull.md).

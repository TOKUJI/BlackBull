# Is BlackBull right for your project?

BlackBull is not a general-purpose "use me for everything" framework. It makes
specific [architectural bets](../about/architecture.md) that pay off in
specific scenarios and cost you elsewhere. This page helps you self-select in
about 60 seconds.

## Scenarios where BlackBull excels

### Multiple protocols in one process

You're building an IoT gateway that ingests MQTT sensor data, serves a
WebSocket dashboard, exposes a REST API, and talks to backend services over
gRPC — all from a single `python app.py` on a small box. Most Python stacks
need a reverse proxy plus a separate MQTT broker; BlackBull serves all of it
in one runtime, on one process.

### You want to see what your HTTP server is doing

Every frame, every stream-state transition, every HPACK encoding is visible
in your debugger. The protocol implementations are pure Python — no C
extensions, no opaque libraries. That makes BlackBull a strong platform for
learning HTTP/2 internals or prototyping protocol-level behaviour.

### You need to test protocol edge cases

BlackBull ships a programmable fault-injection framework: craft malformed
HTTP/2 frames, simulate connection drops mid-stream, run differential oracles
against a reference implementation. If your product depends on HTTP/2
correctness under adverse conditions, this toolchain is unusual and useful.

### You deploy to ARM, RISC-V, or constrained environments

Zero C extensions means zero cross-compilation pain. BlackBull runs anywhere
CPython runs — no build step, no native dependencies to source for your
target architecture.

### Throughput matters more than ecosystem breadth

BlackBull is competitive-to-faster than the popular Python async frameworks on
identical hardware (see the `bench/` HttpArena results). The trade-off is
fewer community extensions, tutorials, and Stack Overflow answers.

## Scenarios where another framework may fit better

### A standard CRUD API with rich OpenAPI

If your primary need is Pydantic models → OpenAPI schema → Swagger UI, a stack
with mature body-schema generation and security-scheme declaration will get
you to production faster today. BlackBull's OpenAPI support is functional but
not yet as complete.

### Built-in authentication and rate limiting

BlackBull currently leaves auth and rate limiting to application code. If you
want batteries-included `AuthenticationMiddleware` and rate-limiting backends,
another framework reduces boilerplate for standard API projects.

### AnyIO / Trio compatibility

BlackBull is asyncio-only. If your codebase or dependencies require Trio via
AnyIO, that's a genuine mismatch.

### Server-driven UI (LiveView-style)

If your primary need is server-driven, component-model UI updates, a
purpose-built stack for that pattern will serve you better. BlackBull's
WebSocket support is excellent for data channels but is not a UI framework.

## The honest trade-off

| BlackBull invests in | BlackBull defers to the ecosystem |
|---|---|
| Protocol correctness (h2spec, Autobahn, differential oracle) | Auth / rate-limiting middleware |
| Multi-protocol density (five protocols, one process) | ORM / DB integration |
| Raw throughput on identical hardware | Tutorial count / community size |
| Testing infrastructure (fault injection, oracle, fuzzing) | Pydantic-grade body-schema generation |
| Pure Python (zero C extensions, easy deploy) | AnyIO / Trio compatibility |

For the reasoning behind these bets, see [Architecture](../about/architecture.md);
for the current gaps in detail, see
[Known limitations](https://github.com/TOKUJI/BlackBull/blob/master/KNOWN_LIMITATIONS.md).

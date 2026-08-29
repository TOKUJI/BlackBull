# Architecture

BlackBull makes a small number of deliberate architectural bets. This page
states them and the reasoning behind each, so you can judge the framework on
its design rather than a feature checklist. For the line-by-line internals,
see [Internals](internals.md); for "does this fit my project?", see
[Is BlackBull right for your project?](../getting-started/why-blackbull.md).

## Protocol ownership

BlackBull implements HTTP/1.1, HTTP/2, WebSocket, gRPC-over-h2c, and MQTT 5.0
in pure Python. It does **not** delegate the wire to Uvicorn, Hypercorn,
`h11`, `wsproto`, or `h2`. Every frame — every HPACK block, every stream
state transition, every chunk boundary — is Python you can step through in a
debugger.

The trade-off is explicit: BlackBull carries the cost of implementing and
conforming protocols itself, in exchange for two things no delegating stack
can offer — end-to-end debuggability, and the ability to make the protocol
layer do things a library won't let you (see *Fault injection* below).

## Multi-protocol, one process

A single `python app.py` serves every protocol at once — HTTP routes,
a WebSocket channel, a gRPC service, and an MQTT broker, in one runtime.
Non-HTTP protocols attach through one seam, `app.add_extension(...)`, while
the HTTP core stays protocol-agnostic. There is no reverse proxy to configure,
no separate broker process, and no C extension in the path.

Ports are not part of the count. A deployment states the sockets it wants as
[listeners](../guide/listeners.md) — cleartext and TLS side by side, two
certificates, an admin port on loopback — and one process serves all of them,
with every worker on every one.

This is the bet behind the IoT-gateway and edge use cases: protocol density
in a single deployable, on hardware where running four services is a burden.

## Actor model

Concurrency is message-passing, not shared-lock. A `ConnectionActor` owns
each connection and spawns the protocol actor that claims it — `HTTP1Actor`,
`HTTP2Actor`, `WebSocketActor`, `BrokerActor`. Each runs its own inbox loop;
state that would otherwise need a lock lives inside one actor and is mutated
only by that actor's loop.

The payoff is isolation: a misbehaving or slow connection is contained to its
own actor and cannot corrupt another's state. The same model runs your HTTP
routes, the MQTT broker, and the gRPC handlers — one concurrency story across
every protocol. See [Internals](internals.md) for the actor topology and the
per-connection `TaskGroup` supervision.

## Fault injection — the differentiator

Because BlackBull owns its HTTP implementations, it can be instructed to
**misbehave on command**: half-closed streams, exhausted flow-control
windows, illegal SETTINGS frames, trailers where none belong on HTTP/2; a
status line delivered a byte at a time, a `Content-Length` that lies, a
chunked body that stops mid-chunk on HTTP/1.1 — all crafted from a Python
script. A delegating stack cannot easily do this, because the misbehaviour
would have to be programmed into a third-party protocol library.

The same ownership creates the obligation that goes with it: the fault
servers assemble their own bytes rather than calling the production send
path, because a breaker sharing the production serialiser cannot emit a fault
that serialiser has.

That makes BlackBull a portable conformance test bench: if you maintain an
HTTP client, proxy, or middleware, you can drive it against adversarial
protocol conditions in CI, and run a differential oracle that compares
BlackBull's wire behaviour against a reference server such as nginx. See the
[Fault injection guide](../guide/fault_injection.md).

## Conformance

BlackBull is validated against the same external suites used to check nginx
and Envoy, not just its own tests:

- **h2spec** (RFC 9113 + RFC 7541) — the de-facto HTTP/2 + HPACK suite,
  ~146 numbered cases, run in CI on every push.
- **Autobahn|Testsuite** (RFC 6455) — ~500 WebSocket cases.
- **Differential oracle** — BlackBull vs nginx, compared wire-for-wire on a
  captured request corpus.

The authoritative status is the CI pipeline, not any point-in-time local run;
see [Conformance](conformance.md) for how to reproduce each suite and where
the CI results live.

## Performance

BlackBull is benchmarked with [HttpArena](https://github.com/) against the
popular Python async frameworks on identical cloud hardware; the harness and
per-release results live under `bench/`. The headline is that a pure-Python
stack is competitive-to-faster on throughput despite owning every byte — the
numbers, and the methodology, are in the benchmark results rather than quoted
here so they never go stale. See [Conformance](conformance.md) and the
`bench/` directory.

## Testing infrastructure

The framework ships the tooling used to keep it correct:

- Programmable fault injection (HTTP/1 client + HTTP/2 server scenarios).
- Atheris fuzzing integration over the parsers.
- A multi-job CI conformance pipeline (h2spec, Autobahn, corpus replay,
  HTTP/2 flow control, gRPC interop).

## What BlackBull defers

These bets have a cost, and the honest list of what BlackBull leaves to the
ecosystem — auth/rate-limiting middleware, ORM/DB integration, Pydantic-grade
body-schema generation, AnyIO/Trio compatibility, community breadth — is in
[Known limitations](https://github.com/TOKUJI/BlackBull/blob/master/KNOWN_LIMITATIONS.md)
and the [use-case guide](../getting-started/why-blackbull.md).

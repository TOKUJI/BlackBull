# MQTT broker — actor-model design

This page explains *how* the MQTT broker is built, for readers who want to
understand the internals or extend them. For the user-facing API (`on_message`,
`{name}` captures, tap modes) see the [MQTT broker guide](../guide/mqtt.md).

The MQTT broker is the first part of BlackBull that uses the framework's
**Actor inbox** for real — see [Relationship to the framework actor model](#relationship-to-the-framework-actor-model)
below.

## Why an actor model at all

A broker is shared mutable state by nature: one routing table, one set of
sessions, one retained-message store, all touched by every connection
concurrently. The obvious implementation reaches for locks. The actor model
removes the need for them: a single actor *owns* the state and processes one
message at a time, so concurrent connections can never interleave inside it.

BlackBull's broker is three kinds of actor:

| Actor | Count | Owns | Inbox carries |
|-------|-------|------|---------------|
| `BrokerActor` | one per app/worker | all routing/session/retained state | client control events (`Attach`, `ClientPublish`, …) |
| `MQTTConnectionActor` | one per connection | one socket's write side | outbound packets (`Send`, `Close`) |
| `TapActor` | one per app/worker | nothing (stateless dispatch) | published messages for `on_message` taps |

Each lives in its own module: `BrokerActor` in `blackbull.mqtt.broker`,
`MQTTConnectionActor` in `blackbull.mqtt.connection`, `TapActor` (with the
`Message` read-model) in `blackbull.mqtt.tap`, and the user-facing
`MQTTExtension` wiring in `blackbull.mqtt.extension`. The wire codec is separate
again, in `blackbull.mqtt.messages`.

## The two-actor data plane

```
            ┌──────────────────────── one per connection ────────────────────────┐
  socket →  reader loop ──decode──►  MQTTConnectionActor.run()  ──write──►  socket
  (bytes)        │  (control packets)        ▲   (sole writer, drains its inbox)
                 │                           │
                 ▼  send(ClientPublish, …)   │  send(Send(packet=…)), send(Close)
            ┌─────────────────────────────────────────────┐
            │            BrokerActor.run()                 │   one per app/worker
            │  owns subscriptions / sessions / retained    │   (serial inbox → no locks)
            └─────────────────────────────────────────────┘
```

### `BrokerActor` — the state owner

`BrokerActor` is the single locus of routing state: the live-connection
registry, per-client sessions (subscriptions and pending QoS state), retained
messages, and Will templates. It never touches a socket. When it needs to send
something to a client it `send`s a `Send` (or `Close`) message to that client's
connection actor and moves on.

Because it processes its inbox serially, two PUBLISHes from two different
connections are handled one after another, never concurrently — so the routing
table and session dicts are plain Python objects with **no locks and no shared
mutable state**. This is the property the actor model buys.

### `MQTTConnectionActor` — the sole socket writer

Each connection has one `MQTTConnectionActor`. Its inbox carries *only outbound
packets*, and its `run()` loop — draining that inbox — is the **only** code that
writes to the socket. That single-writer invariant means there are no
cross-task write races, even though the broker, the keep-alive path, and the
reader can all originate outbound traffic.

A sibling **reader task** (`read_loop`) does the opposite direction: it decodes
the wire and `send`s control messages (`ClientPublish`, `ClientSubscribe`, …) to
the broker. Stateless replies the connection can answer itself — PINGRESP, AUTH —
it routes back through its *own* inbox so that `run()` stays the only writer.

`serve_connection` is the raw-protocol handler body that wires the reader task
and the writer loop together and guarantees the broker sees a `Detach` when the
connection ends — including an abnormal (cancelled) close, which is what makes a
Will fire.

### The Will-on-teardown payoff

Because `BrokerActor` outlives every connection actor by construction, a peer's
Last-Will-and-Testament routes to live subscribers *during* that peer's
teardown with no special-casing. An earlier (pre-actor) broker had to keep
global state alive forever to make this work; the long-lived broker actor makes
the crutch unnecessary.

## The tap dispatch plane

`on_message` handlers are **application-level taps** on top of routing — the
broker delivers to subscribers whether or not any tap is registered. Taps have
their own dispatch plane so a slow tap can never stall the data plane.

- **actor mode (default).** The connection *offers* each published `Message` to
  the shared `TapActor` with a non-blocking call and returns immediately. The
  `TapActor` has a **bounded inbox**; on overflow it drops the newest message
  and logs a running dropped-count. A slow tap therefore back-pressures
  *nothing* — the cost surfaces as bounded coverage, not latency.
- **inline mode.** The connection awaits the matching callbacks itself, so a
  slow tap back-pressures only its own connection. Selected with
  `MQTTExtension(tap_mode='inline')`; retained mainly so the
  `bench/mqtt/tap_throughput.py` comparison is a controlled one-variable test.

Both modes share one matching/invocation path (`run_taps`), so a tap behaves
identically either way apart from the back-pressure characteristic. `{name}`
topic captures are compiled once (rewriting `{name}` to a `+` level and
recording the capture position) and bound to keyword arguments at dispatch.

## Reading the wire: `PacketFramer`

TCP is a byte stream, so a single read may contain several MQTT packets, a
partial packet, or junk from a desynchronised peer. `PacketFramer` isolates that
framing/resync state machine from the read loop: fed raw bytes, it yields each
fully decoded packet and keeps any trailing partial packet for the next feed. An
incomplete packet ends the current iteration (wait for more bytes); a hard
decode error drops one byte and resyncs.

!!! note "Why the framer still copies at the decode boundary"
    `PacketFramer` snapshots its buffer with `bytes(...)` before each decode
    because the codec's `decode_packet` input contract is deliberately `bytes`
    (enforced by beartype). A zero-copy framer would mean widening that contract
    to the buffer protocol; that is deferred, and the copy is bounded by one
    packet's length.

## Relationship to the framework actor model

The framework's [actor-model design invariants](../about/internals.md) describe
an `Actor` base with an `asyncio.Queue` inbox and a `send` / `run` / `_handle`
contract. Historically the **HTTP** actors (`ConnectionActor`, `HTTP1Actor`,
`HTTP2Actor`, `StreamActor`, …) override `run()` and call each other through
direct method calls — the inbox is defined but latent on that path.

The MQTT broker is the **first production code that uses the inbox for real**:
`BrokerActor`, `MQTTConnectionActor`, and `TapActor` all keep the base `run()`
loop, override `_handle()`, and communicate exclusively by `await
other.send(message)`. That is why the broker needs no locks — the "one message
at a time" guarantee is the base-class `run()` draining the queue, not a
convention the broker re-implements.

So the broker is not a parallel mechanism bolted onto the framework; it is the
actor model the framework already described, finally exercised end-to-end. If
you are looking for a worked example of BlackBull's actor inbox, this is it.

# MQTT 5 broker

BlackBull ships a pure-Python **MQTT 5 broker** that runs as a sidecar on the
[Non-ASGI bridge](raw-protocols.md). One process can serve HTTP/1.1, HTTP/2, and
WebSocket *and* speak MQTT on the standard `1883` port — no separate broker, no C
extension, no extra dependency.

It is the first real consumer of the bridge: where a raw `raw_handler` owns a
single socket, the MQTT broker layers a full protocol on top — packet codec,
per-connection actor, and process-wide message routing between clients.

```python
from blackbull import BlackBull
from blackbull.mqtt import MQTTExtension, Message

app = BlackBull()
mqtt = app.add_extension(MQTTExtension(port=1883))

@app.route(path='/')
async def index():
    return "HTTP here; MQTT broker on :1883."

@mqtt.on_message(topic='sensors/+/temperature')
async def on_temperature(msg: Message):
    print(msg.topic, msg.payload.decode())

app.run(port=8000)   # HTTP on 8000, MQTT on 1883
```

A full runnable version is in `examples/mqtt_broker.py`.

## Wiring and the handler API

The broker is an [`Extension`](extensions.md): you register it through the core's
single extension seam, `app.add_extension(MQTTExtension(port=1883))`, which
returns the extension so you can keep a handle on it. `BlackBull` itself carries
no MQTT-specific code.

`MQTTExtension.on_message(topic='#')` decorates an async `(message) -> None`
callback. It receives a single `blackbull.mqtt.Message` (`msg.topic`,
`msg.payload`, `msg.qos`, `msg.retain`, `msg.properties`) — mirroring how
`@app.on` hands an observer one `Event`. The callback fires for every PUBLISH
whose topic matches *topic* — an MQTT **topic filter**, so the `+` (single level)
and `#` (multi level) wildcards apply.

```python
mqtt = app.add_extension(MQTTExtension())

@mqtt.on_message(topic='#')            # every message
async def firehose(msg: Message): ...

@mqtt.on_message(topic='alerts/#')     # one subtree
async def alerts(msg: Message): ...
```

A filter level may also be a `{name}` **capture**: it matches one level like `+`
and is injected into the callback as a keyword argument, mirroring HTTP path
params.

```python
@mqtt.on_message(topic='sensors/{room}/temperature')
async def on_temperature(msg: Message, room: str):
    print(room, msg.payload.decode())   # room == 'kitchen' for sensors/kitchen/temperature
```

Handlers are an **application-level tap**: they run *in addition to* normal
broker routing, never instead of it. The broker still delivers each PUBLISH to
every subscribed MQTT client whether or not a handler matches. A handler that
raises is isolated and logged — it never disturbs the broker or other handlers.

By default taps are dispatched on a **decoupled `TapActor`**: the connection
hands each message off without waiting, so a slow tap can never back-pressure
delivery or the broker. The `TapActor`'s inbox is bounded; if taps fall behind,
the newest messages are dropped (best-effort observability) and a running
dropped-count is logged. Taps are therefore *not* a reliable delivery path — use
a real MQTT subscription for that. (`MQTTExtension(tap_mode='inline')` runs taps
inline on the receiving connection instead — the original behaviour, kept
mainly so the `bench/mqtt/tap_throughput.py` comparison stays reproducible.)

The broker also runs without any handler at all: `on_message` is just how an
application observes traffic. `app.add_extension(MQTTExtension())` on its own
gives you a fully functional broker with no tap.

## What the broker implements

The broker targets the MQTT 5.0 OASIS feature set exercised by BlackBull's
conformance matrix:

| Area | Support |
|------|---------|
| Connection | CONNECT / CONNACK, protocol-level check (rejects non-5 with `0x84`), Clean Start, Session Present |
| Subscriptions | SUBSCRIBE / SUBACK, UNSUBSCRIBE / UNSUBACK, `+` and `#` wildcards, `$`-topic rules; `No Local`, `Retain As Published`, and `Retain Handling` (0/1/2, §3.3.1.3) options honoured; invalid Topic Filters rejected (`0x8F`) |
| Shared subscriptions | `$share/{ShareName}/{filter}` groups (§4.8.2): each matching message goes to exactly **one** connected group member, round-robin — see below |
| Publish | invalid Topic Names (wildcards / null) rejected (`0x90`), never routed or retained |
| QoS 0 | fire-and-forget delivery |
| QoS 1 | PUBACK round-trip |
| QoS 2 | PUBREC → PUBREL → PUBCOMP four-way handshake; duplicate PUBLISH de-duplicated; unacked messages replayed with `DUP=1` on reconnect |
| Retained | one retained message per topic; delivered to late subscribers; zero-length payload clears |
| Will (LWT) | delivered on abnormal disconnect; suppressed on a normal `DISCONNECT` (`0x00`) |
| Keep-alive | PINGREQ / PINGRESP; an idle connection is closed (Will fired) at 1.5× the negotiated Keep Alive (§3.1.2.10) |
| Session takeover | a second CONNECT for a live Client Identifier disconnects the prior connection with `0x8E` (§3.1.4) |
| Properties | the full MQTT 5 property set (§2.2.2.2) on every packet that carries properties |
| Sessions | subscriptions and pending QoS state preserved across reconnects with Clean Start = 0 |
| Flow control | the client's `Receive Maximum` (§3.1.2.11.3) is enforced in the outbound direction; the broker's own is advertised in CONNACK as a promise to conforming clients |
| Resource limits | packet size, session backlog, and retained-store size are bounded and advertised — see below |

The wire codec lives in `blackbull.mqtt.messages` (the 15 control-packet
dataclasses, `encode_packet` / `decode_packet`, the property system, reason
codes, and `topic_matches_filter`). The broker is an actor model split across a
few small modules: `blackbull.mqtt.broker` holds the `BrokerActor`, which owns
all routing state (subscriptions, sessions, retained messages) and, processing
its inbox serially, needs no locks; `blackbull.mqtt.connection` holds the
`MQTT5Actor` (one per connection — the sole writer to its socket,
forwarding decoded control packets to the broker) and `serve_connection`, which
wires the two; `blackbull.mqtt.tap` holds the `TapActor` and the `Message`
read-model; and `blackbull.mqtt.extension` holds `MQTTExtension` and
`MQTTProtocolDetector`, which recognises the MQTT CONNECT first byte (`0x10`) for
shared-port sniffing.

## Shared subscriptions

Subscribers that name the same `$share/{ShareName}/{filter}` pair form a
*share group* (MQTT 5 §4.8.2). Each application message matching the filter is
delivered to exactly **one** member of the group — round-robin across the
members currently connected — instead of every matching subscriber. This is
the standard MQTT pattern for load-balancing a work queue across a pool of
consumers:

```bash
# terminals 1 and 2 — two workers in the same share group
mosquitto_sub -t '$share/pool/jobs/#' -p 1883 -V 5
mosquitto_sub -t '$share/pool/jobs/#' -p 1883 -V 5

# terminal 3 — publishes alternate between the two workers
mosquitto_pub -t 'jobs/import' -m 'job-1' -p 1883 -V 5
mosquitto_pub -t 'jobs/import' -m 'job-2' -p 1883 -V 5
```

Semantics worth knowing:

- **Non-shared subscriptions are unaffected** — a client subscribed to a plain
  `jobs/#` still receives every message, alongside whichever group member the
  rotation picks. Shared and non-shared subscriptions held by the same client
  are independent delivery channels.
- **Delivery QoS** is the chosen member's granted QoS (capped by the publish
  QoS, as usual).
- **Disconnected members are skipped** while any member is connected
  (§4.8.2.3). If *no* member is connected, the message is not queued for the
  group — the same no-offline-queue behaviour the broker applies to non-shared
  subscriptions.
- **Retained messages are never delivered to a shared subscription** (§4.8.2).
- **`No Local` cannot be combined with a shared subscription** — MQTT 5 makes
  it a Protocol Error (§3.8.3.1), and the broker disconnects with `0x82`.
- Malformed forms (`$share/g`, an empty ShareName, a wildcard in the ShareName,
  or an empty filter portion) are rejected per-entry with `0x8F`.

## Resource limits

An MQTT broker holds state on a client's behalf: buffered packet bytes, unacked
messages, retained messages that outlive the session that published them. Each
of those is bounded, and each bound is **advertised in CONNACK** where MQTT 5
has a property for it — so a conforming client stays inside the limits without
ever meeting the enforcement path.

| Limit | Default | Advertised as | Over the limit |
|---|---|---|---|
| `BB_MQTT_MAX_PACKET_SIZE` | 1 MiB | `Maximum Packet Size` (§3.2.2.3.6) | `DISCONNECT` **0x95 Packet Too Large**, connection closed |
| `BB_MQTT_RECEIVE_MAXIMUM` | 64 | `Receive Maximum` (§3.2.2.3.3) | a conforming client waits — a promise, not a gate (see below) |
| `BB_MQTT_MAX_QUEUED_MESSAGES` | 1000 | — (a broker-side total) | newest message refused, cap hit logged |
| `BB_MQTT_MAX_RETAINED` | 10000 | — (a broker-side total) | retained publish to a *new* topic refused; `0x97` in the PUBACK/PUBREC at QoS ≥ 1 |

Three properties of these limits are worth knowing before you tune them:

**The packet limit is judged from the header.** MQTT 5 lets a peer declare a
Remaining Length of 268,435,455 bytes (256 MiB) and then deliver it slowly. The
check runs as soon as the fixed header is readable, so the payload is never
buffered — the broker refuses on what the peer *claimed*, not on what it
managed to send.

**The backlog exists because flow control is not a licence to forget.** When a
client's `Receive Maximum` window is full, matching messages are held rather
than dropped: the client asked the broker to slow down, not to lose its
messages. But "hold everything" is how a subscriber that never acknowledges
turns a subscription into a leak, so the queue is bounded too. At the bound the
**newest** message is refused and the oldest kept — a subscriber is owed what it
was promised first, and has no way to detect a message silently dropped from the
middle.

**Retained messages are capped by topic count, and correction is always
allowed.** At the cap, a retained publish to a *new* topic is refused, but
updating or deleting an already-retained topic still works. A client locked out
of correcting its own retained state would be worse off than one that could
never set it — and deleting (a zero-length retained payload, §3.3.2.3) is what
frees the room being contended for. The message is still delivered to current
subscribers; only the storage is declined.

How the publisher finds out depends on the QoS it chose, because that is what
decides whether the protocol has a channel for the answer. **QoS 1 and 2**
receive `0x97 (Quota Exceeded)` in the PUBACK or PUBREC. **QoS 0 is not told**
— it has no acknowledgement (§3.3.4), and closing the connection over a storage
quota would be disproportionate as well as destroying a live delivery that
succeeded. If you need to know your retained state was stored, publish it at
QoS ≥ 1.

**One limit is advertised but not enforced, and it is worth knowing which.**
`BB_MQTT_RECEIVE_MAXIMUM` tells a client how many QoS>0 publishes it may have
in flight *towards* the broker. Nothing counts a non-conforming client's
against it; what bounds that direction is the 16-bit packet-identifier space
and the packet size cap. The **client's** Receive Maximum, in the outbound
direction, is enforced — that is what `BB_MQTT_MAX_QUEUED_MESSAGES` backs.

Every refusal above emits a record on `blackbull.caps` (see
[Logging](logging.md#cap-hit-log-blackbullcaps)), so a limit that fires is a
limit you can see fire.

## Trying it with Mosquitto

The broker speaks standard MQTT 5, so the Eclipse Mosquitto CLI works against it
(`apt install mosquitto-clients`):

```bash
# terminal 1 — subscribe
mosquitto_sub -t 'sensors/#' -p 1883 -V 5

# terminal 2 — publish
mosquitto_pub -t 'sensors/room1/temperature' -m '21.5' -p 1883 -V 5
```

The message appears in the subscriber's terminal and in any matching
`@mqtt.on_message` handler.

## Documenting the taps with AsyncAPI

OpenAPI documents BlackBull's HTTP surface, but it has no vocabulary for topics
or the publish/subscribe direction, so the broker is invisible to it. The
messaging-world counterpart is [AsyncAPI](https://www.asyncapi.com/), and
`AsyncAPIExtension` emits an AsyncAPI 3.0 document for the topic taps your app
registered — served over HTTP, exactly as `/openapi.json` is. It is a normal
extension and coexists with `OpenAPIExtension`:

```python
from blackbull import BlackBull
from blackbull.mqtt import MQTTExtension, AsyncAPIExtension, Message

app = BlackBull()
mqtt = app.add_extension(MQTTExtension(port=1883))
app.add_extension(AsyncAPIExtension(title='Sensor Gateway', version='1.0.0'))

@mqtt.on_message(topic='sensors/{room}/temperature')
async def on_temp(msg: Message, room: str):
    """Temperature readings per room."""
```

After `app.run()` the document is at `/asyncapi.json` and an HTML viewer (a
CDN-hosted AsyncAPI renderer — no new Python dependency) at `/asyncapi`. Each
`on_message` filter becomes a *channel* whose `address` is the filter as you
wrote it (`{name}` captures preserved); each callback becomes a `receive`
*operation* (the application *receives* PUBLISHes), with its docstring summary.
Pass `docs_path=None` to skip the HTML page, or `server_host=` to override the
advertised broker host (default `localhost:<port>`).

The document is generated lazily on each request, so taps registered *after*
`add_extension(AsyncAPIExtension(...))` are still included. The MQTT extension
must be present when the spec route is hit, or the request raises
`RuntimeError`.

Three honest caveats — also stated in the document's `info.description`:

- It documents the **application's taps**, not "the broker's API". A broker
  accepts any topic from any client; `on_message` filters describe only what
  *this* app observes.
- **QoS and retain are not captured** — taps fire regardless of QoS, so MQTT
  channel bindings are omitted until the tap API carries that metadata.
- **Payloads are opaque bytes** (`application/octet-stream`) until a future
  opt-in `schema=` on `on_message` lands.

## TLS (`mqtts://`)

`MQTTExtension(port=8883, tls=True)` serves the broker port over TLS using the
same certificate the HTTPS listener uses — pass `certfile`/`keyfile` (or an
`ssl_context`) to `app.run()` as usual. The server refuses to start if
`tls=True` is set with no certificate configured. Cleartext remains the
default (`tls=False`), so existing deployments are unchanged.

## Why the broker has a single owner

The broker runs on **worker 0** only, while HTTP scales across all workers.
That is a protocol requirement, not an implementation shortcut.

MQTT 5.0 (OASIS Committee Specification 02, March 2019) defines semantics
that depend on broker-side state visible to *every* connection:
publish-subscribe matching (§3.3), retained messages (§3.3.2.3), session
state across `Clean Start = 0` reconnects (§3.1.2.11), Will messages
(§3.1.2.5), and QoS 1/2 delivery tracking (§4.3).  All five break if that
state is split across worker processes with no shared store.

This matches industry practice: the Eclipse Mosquitto reference
implementation ([mosquitto.org](https://mosquitto.org/)) is single-threaded
by architecture for the same reason, and EMQX clusters via Erlang
distributed message passing rather than splitting state across local
workers.  (Confirmed 2026-06-25 against the OASIS MQTT 5.0 specification and
Mosquitto's project documentation.)

See [Workers](../deployment/workers.md) for how the single-owner binding
interacts with `--reload` and `SO_REUSEPORT`.

## Limitations

- **No MQTT-over-WebSocket transport.** TLS is supported via
  `MQTTExtension(tls=True)`; the WebSocket binding is not yet wired up.
- **Single owner (HTTP still scales).** The broker runs on **worker 0** only —
  its state (subscriptions, sessions, retained messages) lives in that one
  process and is neither shared across workers nor persisted across restarts.
  HTTP, however, scales across all workers: `app.run(workers=4)` alongside the
  broker runs HTTP on every worker and the broker on worker 0. (`--reload` still
  pins `workers=1` when a broker is registered.)
- **In-memory sessions.** Sessions are retained for the process lifetime rather
  than expired on a timer; restarting the broker clears all session state.

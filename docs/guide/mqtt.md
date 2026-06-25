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
inline on the receiving connection instead — the pre-Sprint-54 behaviour, kept
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
| Subscriptions | SUBSCRIBE / SUBACK, UNSUBSCRIBE / UNSUBACK, `+` and `#` wildcards, `$`-topic rules, shared subscriptions |
| QoS 0 | fire-and-forget delivery |
| QoS 1 | PUBACK round-trip |
| QoS 2 | PUBREC → PUBREL → PUBCOMP four-way handshake |
| Retained | one retained message per topic; delivered to late subscribers; zero-length payload clears |
| Will (LWT) | delivered on abnormal disconnect; suppressed on a normal `DISCONNECT` (`0x00`) |
| Keep-alive | PINGREQ / PINGRESP |
| Properties | the full MQTT 5 property set (§2.2.2.2) on every packet that carries properties |
| Sessions | subscriptions and pending QoS state preserved across reconnects with Clean Start = 0 |

The wire codec lives in `blackbull.mqtt.messages` (the 15 control-packet
dataclasses, `encode_packet` / `decode_packet`, the property system, reason
codes, and `topic_matches_filter`). The broker is an actor model split across a
few small modules: `blackbull.mqtt.broker` holds the `BrokerActor`, which owns
all routing state (subscriptions, sessions, retained messages) and, processing
its inbox serially, needs no locks; `blackbull.mqtt.connection` holds the
`MQTTConnectionActor` (one per connection — the sole writer to its socket,
forwarding decoded control packets to the broker) and `serve_connection`, which
wires the two; `blackbull.mqtt.tap` holds the `TapActor` and the `Message`
read-model; and `blackbull.mqtt.extension` holds `MQTTExtension` and
`MQTTProtocolDetector`, which recognises the MQTT CONNECT first byte (`0x10`) for
shared-port sniffing.

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

## Limitations

- **Cleartext only.** TLS / MQTT-over-WebSocket transport is not yet wired up,
  matching the bridge's current limits.
- **Single owner (HTTP still scales).** The broker runs on **worker 0** only —
  its state (subscriptions, sessions, retained messages) lives in that one
  process and is neither shared across workers nor persisted across restarts.
  HTTP, however, scales across all workers: `app.run(workers=4)` alongside the
  broker runs HTTP on every worker and the broker on worker 0. (`--reload` still
  pins `workers=1` when a broker is registered.)
- **In-memory sessions.** Sessions are retained for the process lifetime rather
  than expired on a timer; restarting the broker clears all session state.

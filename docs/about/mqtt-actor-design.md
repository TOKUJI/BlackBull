# MQTT broker design

## Data-plane ownership

`BrokerActor` owns routing, sessions, subscriptions, retained messages and
Wills. Its serial inbox is the admission boundary: after rejection, takeover
or retirement, commands from that connection identity are inert. Checking
only a Client Identifier would admit a superseded connection.

`MQTT5Actor` owns socket writes. Readers, keep-alive handling and broker
replies send output through its inbox; packets cannot interleave and replies
cannot precede CONNACK or follow rejected admission.

Read `blackbull/mqtt/broker.py`, `connection.py` and `messages.py` before
changing packet order. See the public
[admission contract](../guide/mqtt.md#connection-admission-and-retirement).

## Backpressure and teardown

Data-plane mailboxes have independent count and wire-byte budgets. Broker
input may backpressure a reader; the broker must never await room in a
connection's output queue. That can stall every peer or form a wait cycle
with the reader. Output overload closes only the affected connection.

Supervise reader and writer together. Either child's failure must wake the
other, including a reader waiting on a silent peer. `Detach` is the FIFO
teardown barrier: preserve commands before it and flush its replies before
closing output. Shutdown must release admission and completion waiters.
Expiry notifications coalesce without mutating state outside the broker loop.
Read `blackbull/mqtt/mailbox.py` and `serve_connection`.

## Application taps

Taps observe publishes; they do not own delivery. Positive admission permits
a tap but does not certify PUBLISH validity, storage or delivery.
Never await a tap in the broker's routing loop.

Actor-mode taps have a bounded non-blocking inbox and drop newest on
overflow. Inline taps backpressure their publishing connection. Both share
matching and invocation in `blackbull/mqtt/tap.py`; changing mode must not
change topic captures or callback arguments.

## Reading the wire

`PacketFramer` retains incomplete packets and refuses malformed packets
without resynchronizing. A truncated inner field in a complete packet is
malformed, not incomplete input. Before admission, refusal closes silently;
after admission it sends DISCONNECT through the sole writer.

The serving reader drains packets between bounded reads. Direct `feed()`
callers own their feed sizes: a packet cap is not an aggregate feed-buffer
cap. Keep Alive bounds receive idleness, not total packet assembly time.
See `blackbull/mqtt/connection.py` and
[Resource limits](../guide/mqtt.md#resource-limits).

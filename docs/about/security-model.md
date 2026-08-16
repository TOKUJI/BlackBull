# Security model

BlackBull parses every protocol itself and accepts connections directly, so
it is the first thing an untrusted peer talks to. This page states what that
peer can and cannot make the server spend, what the limits are out of the box,
and what BlackBull explicitly does **not** claim.

It describes the version it ships with. Where a limit is on by default, that is
stated; where it is off, that is stated too, along with what to set.

## Scope and trust boundary

**Every byte from a peer is untrusted until dispatch.** Request lines, headers,
bodies, WebSocket frames, HTTP/2 frames and MQTT packets are all parsed by
BlackBull's own code, and every one of those paths is bounded before it can
allocate on a peer's behalf.

Named non-goals — real limits, not oversights:

| Out of scope | Why |
|---|---|
| Your application code | A handler that reads an unbounded file or shells out is beyond any server's reach. |
| Authentication and authorisation | BlackBull ships neither. Use middleware or a gateway. |
| TLS trust decisions | BlackBull terminates TLS when configured to, but certificate issuance, pinning and revocation are yours. |
| OS and deployment hardening | File-descriptor limits, cgroups, network ACLs. BlackBull *reads* the fd limit (see below) but does not set it. |
| Volumetric denial of service | A flood that saturates your uplink is answered upstream, not in a Python process. |

## What a peer can make the server spend

| Resource | How a peer reaches it | Bounded by |
|---|---|---|
| Memory | large or accumulating request bodies, WebSocket messages, MQTT packets and broker state | size and total caps on every growable path |
| Event-loop time | floods of cheap control frames that each oblige a small piece of work | per-type frame-rate meters |
| Connection slots | opening connections and holding them | `BB_MAX_CONNECTIONS`, plus idle and header deadlines |
| Task slots | opening HTTP/2 streams | `BB_H2_MAX_CONCURRENT_STREAMS`, per-connection handler semaphore |
| Write path | requesting a response and refusing to read it | `BB_WRITE_TIMEOUT`, on the socket drain *and* on the HTTP/2 flow-control wait |
| Broker state | subscribing without acknowledging; retaining messages | `BB_MQTT_MAX_QUEUED_MESSAGES`, `BB_MQTT_MAX_RETAINED` |

## The invariant

> **Every path a peer can grow has a bound on one unit, on the total, and on
> how long it may take.**

Those three questions are the design rule, and they are not interchangeable. A
total cap with no time bound lets a peer hold a slot for as long as the cap
allows; a time bound with no total cap lets it deliver forever in small, timely
pieces. Each row below names the knob for each column.

| Path | One unit | Total | Time |
|---|---|---|---|
| HTTP/1.1 request head | `BB_HEADER_MAX_LINE` 8 KiB | `BB_HEADER_MAX_TOTAL` 64 KiB | `BB_HEADER_TIMEOUT` 10 s |
| HTTP/1.1 request body | `BB_BODY_CHUNK_MAX` 512 KiB | `BB_MAX_BODY_SIZE` 30 MiB | `BB_BODY_TIMEOUT` 30 s + `BB_MIN_BODY_RATE` 240 B/s |
| HTTP/2 header block | `SETTINGS_MAX_FRAME_SIZE` | `BB_HEADER_MAX_TOTAL` 64 KiB | `BB_HEADER_TIMEOUT` 10 s |
| HTTP/2 request body | `SETTINGS_MAX_FRAME_SIZE` | `BB_MAX_BODY_SIZE` 30 MiB | `BB_MIN_BODY_RATE` 240 B/s |
| HTTP/2 connection | — | stream and window budgets | `BB_H2_IDLE_TIMEOUT` 300 s + `BB_H2_PING_TIMEOUT` 30 s |
| HTTP/2 response write | `SETTINGS_MAX_FRAME_SIZE` | — | `BB_WRITE_TIMEOUT` 30 s |
| Control frames (HTTP/2 + WebSocket) | — | `BB_FRAME_RATE_LIMIT` 20 per type | `BB_FRAME_RATE_WINDOW` 1 s |
| WebSocket frame | `BB_WS_MAX_FRAME_PAYLOAD` 64 MiB | — | — |
| WebSocket message | per frame | `BB_WS_MAX_MESSAGE_SIZE` 16 MiB | — *(see qualification 3)* |
| MQTT packet | `BB_MQTT_MAX_PACKET_SIZE` 1 MiB | same, before buffering | keep-alive × 1.5 |
| MQTT session state | 16-bit packet-identifier space | `BB_MQTT_MAX_QUEUED_MESSAGES` 1000 | session expiry |
| MQTT retained store | — | `BB_MQTT_MAX_RETAINED` 10000 | — |
| Connections | — | `BB_MAX_CONNECTIONS` (derived) | detect deadline, keep-alive |

Full descriptions: [Environment variables](../reference/env-vars.md).

### Three limits that are less obvious than they look

**A message is bounded by what your handler receives, not by what the wire
carried.** `permessage-deflate` ratios measured in this codebase reach
**1028.8:1**, so a WebSocket frame far under the 64 MiB frame cap can still
inflate to gigabytes. `BB_WS_MAX_MESSAGE_SIZE` bounds the message *after*
fragment reassembly and *after* inflation, and the inflate is bounded by zlib
itself — an over-sized message is refused without ever being built.

**A size cap is judged from what the peer declares, before its payload is
read.** An over-cap `Content-Length`, an over-cap MQTT Remaining Length, and an
over-cap HTTP/2 frame are all refused at the header. MQTT 5 permits a peer to
declare 268,435,455 bytes; nothing waits for them to arrive.

**An advertised limit is not always an enforced one, and this page says
which is which.** `BB_MQTT_RECEIVE_MAXIMUM` is announced in CONNACK so a
conforming client paces itself; it is a *promise*, and nothing counts a
non-conforming client's in-flight publishes against it. What bounds that
direction is the 16-bit packet-identifier space and the packet size cap. The
client's own Receive Maximum, in the outbound direction, **is** enforced. Every
other limit in the table above is enforced.

**A rate floor is what a per-read timeout cannot be.** An arrival-paced read
completes before any per-read deadline no matter how few bytes it carries, so a
one-byte-per-second drip satisfies `BB_BODY_TIMEOUT` indefinitely.
`BB_MIN_BODY_RATE` bounds the *average*, which a drip cannot reset by sending
one more byte.

## Posture by protocol

Three rungs, and the difference between them is what an operator has to do:

- **`bounded-by-default`** — every growable path is bounded with nothing
  configured.
- **`bounded-when-configured`** — the bounds exist but one or more default open.
- **`bounded-unit-only`** — individual units are bounded; totals are not.

| Protocol | Posture | Notes |
|---|---|---|
| HTTP/1.1 | `bounded-by-default` | see the two qualifications below |
| HTTP/2 | `bounded-by-default` | includes control-frame rate metering and PING-based liveness |
| gRPC | `bounded-by-default` | own message bounds, plus everything HTTP/2 provides |
| WebSocket | `bounded-by-default` | message bounded post-reassembly and post-inflation; see qualification 3 |
| MQTT | `bounded-by-default` | limits are also *advertised* in CONNACK, so conforming clients stay inside them |

**Three qualifications, stated here rather than in a footnote**, because they
are the difference between the label and the whole truth:

1. **There is no total request-duration bound by default.**
   `BB_REQUEST_TIMEOUT` is `0`. A peer delivering at exactly the minimum body
   rate, up to the body cap, legally holds a connection for
   31,457,280 ÷ 240 = **131,072 seconds ≈ 36.4 hours**. Every bound above is
   doing its job the whole time — that is the composition of a size cap and a
   rate floor, not a hole in either. nginx and Kestrel have the same property.
   Set `BB_REQUEST_TIMEOUT` if your application has no long-lived requests.

2. **The default connection cap is only as protective as your `ulimit`.**
   `BB_MAX_CONNECTIONS` defaults to `auto`, which derives the cap from the
   process's own `RLIMIT_NOFILE` less a 64-descriptor reserve. That is finite
   and honest — a cap above the fd budget would be decorative, since `accept()`
   fails with `EMFILE` before the cap is consulted — but on a host whose limit
   is 1,048,576 the derived cap is ~1,048,512. It bounds *descriptor
   exhaustion*, not event-loop health. For the latter, set an explicit number;
   1024 is a typical single-loop value.

3. **A WebSocket connection has no retention bound of its own.** HTTP/1.1
   closes an idle keep-alive connection and HTTP/2 probes a silent peer with a
   PING; WebSocket does neither. A peer that completes the handshake and then
   says nothing holds its connection until it disconnects or the process does,
   bounded only by `BB_MAX_CONNECTIONS` — so qualifications 1 and 2 are what
   hold it. Message *content* is fully bounded; this is about the connection.
   If you serve WebSocket to untrusted peers, set `BB_MAX_CONNECTIONS`
   explicitly, and send application-level pings from your handler if you need
   dead peers evicted sooner.

## Defaults and deployment checklist

On out of the box:

- every limit in the invariant table except `BB_REQUEST_TIMEOUT`;
- cap-hit logging on `blackbull.caps` — every refusal above emits a structured
  `WARNING` naming the limit, the requested value and the configured one (see
  [Logging](../guide/logging.md#cap-hit-log-blackbullcaps));
- MQTT limits advertised to clients in CONNACK, so a conforming client never
  reaches the enforcement path.

Worth setting for an internet-facing deployment:

| Setting | Why |
|---|---|
| `BB_REQUEST_TIMEOUT` | the only default-open bound; see qualification 1 |
| `BB_MAX_CONNECTIONS` | an explicit number bounds event-loop health, which the derived value does not |
| `BB_WS_MAX_MESSAGE_SIZE` | the default admits 16 MiB so the WebSocket conformance suite passes unconfigured; lower it if you do not serve huge messages |
| `BB_MAX_BODY_SIZE` | lower it if you accept no uploads |
| `BB_TCP_USER_TIMEOUT_MS` | evicts dead peers behind NATs without waiting for keepalives |

How the defaults compare, verified against primary sources:

| Limit | BlackBull | Peer |
|---|---|---|
| Max request body | 30 MiB (31,457,280 B) | Kestrel `MaxRequestBodySize` 30,000,000 B (~28.6 MB) — the same class, not the same number |
| Min request body rate | 240 B/s, 5 s grace | Kestrel `MinRequestBodyDataRate` 240 B/s, 5 s grace — identical |
| Max connections | derived from `RLIMIT_NOFILE` | HAProxy `maxconn` also defaults to `ulimit -n`; nginx `worker_connections` 512; Kestrel `MaxConcurrentConnections` unlimited |
| HTTP/2 keep-alive ping | on, 300 s delay / 30 s timeout | Kestrel `KeepAlivePingDelay` disabled by default, `KeepAlivePingTimeout` 20 s |

## Evidence

Every claim on this page is backed by a test or an external conformance suite.

| Claim | Evidence |
|---|---|
| Body size and rate bounds | `tests/conformance/http1/test_rfc9110_body_cap.py`, `tests/conformance/http2/test_rfc9113_body_cap.py`, `tests/unit/test_body_rate_limit.py` |
| A slow drip is closed at the shipped default | `tests/conformance/http1/test_rfc9112_slowloris.py::TestBodyTrickle` |
| WebSocket message bounds, including that a bomb is never materialised | `tests/unit/test_ws_message_bounds.py` |
| MQTT packet, backlog and retained-store bounds | `tests/unit/test_mqtt_resource_bounds.py` |
| HTTP/2 time bounds, including that a responsive peer is never closed | `tests/unit/test_h2_time_bounds.py` |
| Frame-rate metering | `tests/unit/test_rate_window.py`, `tests/unit/test_frame_rate_metering.py` |
| Derived connection cap | `tests/unit/test_max_connections_default.py` |
| Protocol conformance | h2spec (HTTP/2 + HPACK), Autobahn (WebSocket), http11probe — see [Conformance](conformance.md) |

## Non-claims

Stated plainly, because a security page that only lists strengths is worth
less than one that draws its own boundary:

- **No third-party security audit.** No external firm has reviewed this code.
- **No red-team exercise.** The limits above were derived by auditing the code
  for growable paths, not by attacking a running deployment.
- **Not volumetric-DoS protection.** Bandwidth and SYN floods are answered
  upstream.
- **A malicious application handler is out of scope.** BlackBull bounds what a
  *peer* can spend, not what your own code can.
- **"No known gaps" is not "no gaps."** A bound that no one has found missing is
  not the same as a bound proven complete.

Found something? Please open an issue at
[github.com/TOKUJI/BlackBull](https://github.com/TOKUJI/BlackBull/issues).

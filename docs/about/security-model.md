# Security model

Use this page to choose resource bounds. The tables cover audited paths, not
a proof that every growable path has been found. Client and server limits
have separate owners.

## Scope and trust boundary

**Every byte from a peer is untrusted until dispatch.** Request lines, headers,
bodies, WebSocket frames, HTTP/2 frames and MQTT packets are all parsed by
BlackBull's own code. Each path listed on this page is bounded before it can
allocate on a peer's behalf, and each bound has a test named under
[Evidence](#evidence). What establishes the *list* is a code audit, and
[Non-claims](#non-claims) says what that method can and cannot prove.

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
| Memory | large or accumulating request bodies, WebSocket messages, MQTT packets and broker state | size and total caps on each path in the table below |
| Event-loop time | floods of cheap control frames that each oblige a small piece of work | per-type frame-rate meters |
| Connection slots | opening connections and holding them | `BB_MAX_CONNECTIONS` (accept-time: see the [startup window](../deployment/unix-and-fd.md#the-startup-window)), plus idle and header deadlines and, on HTTP/2 and WebSocket, a liveness probe |
| Task slots | opening HTTP/2 streams | `BB_H2_MAX_CONCURRENT_STREAMS`, per-connection handler semaphore |
| Write path | requesting a response and refusing to read it | `BB_WRITE_TIMEOUT`, on the socket drain *and* on the HTTP/2 flow-control wait |
| Broker state | subscribing without acknowledging; subscribing to endless filters; retaining messages; leaving sessions behind | `BB_MQTT_MAX_QUEUED_MESSAGES`, `BB_MQTT_MAX_SUBSCRIPTIONS`, `BB_MQTT_MAX_RETAINED`, `BB_MQTT_MAX_SESSIONS` |

## The invariant

> **A path a peer can grow gets a bound on one unit, on the total, and on how
> long it may take.**

Apply the rule to each growable path, including write-only state. Unit,
aggregate, count and time limits are independent; one cannot replace another.

| Path | One unit | Total | Time |
|---|---|---|---|
| HTTP/1.1 request head | `BB_HEADER_MAX_LINE` 8 KiB | `BB_HEADER_MAX_TOTAL` 64 KiB | `BB_HEADER_TIMEOUT` 10 s |
| HTTP/1.1 request body | `BB_BODY_CHUNK_MAX` 512 KiB | `BB_MAX_BODY_SIZE` 30 MiB | `BB_BODY_TIMEOUT` 30 s + `BB_MIN_BODY_RATE` 240 B/s |
| HTTP/2 header block | `SETTINGS_MAX_FRAME_SIZE` | `BB_HEADER_MAX_TOTAL` 64 KiB | `BB_HEADER_TIMEOUT` 10 s |
| HTTP/2 request body | `SETTINGS_MAX_FRAME_SIZE` | `BB_MAX_BODY_SIZE` 30 MiB | `BB_MIN_BODY_RATE` 240 B/s |
| HTTP/2 connection | — | stream and window budgets | `BB_H2_IDLE_TIMEOUT` 300 s + `BB_H2_PING_TIMEOUT` 30 s |
| HTTP/2 response write | `SETTINGS_MAX_FRAME_SIZE` | — | `BB_WRITE_TIMEOUT` 30 s |
| Control frames (HTTP/2 + WebSocket) | — | `BB_FRAME_RATE_LIMIT` 20 per type | `BB_FRAME_RATE_WINDOW` 1 s |
| HTTP/2 priority signals | 5-byte PRIORITY payload | no state recorded (PRIORITY); `SETTINGS_MAX_CONCURRENT_STREAMS` (PRIORITY_UPDATE hints) | — |
| WebSocket frame | `BB_WS_MAX_FRAME_PAYLOAD` 64 MiB | — | — |
| WebSocket message | per frame | `BB_WS_MAX_MESSAGE_SIZE` 16 MiB | — |
| WebSocket connection | — | `BB_MAX_CONNECTIONS` (derived) | `BB_WS_IDLE_TIMEOUT` 300 s + `BB_WS_PONG_TIMEOUT` 30 s |
| MQTT packet | `BB_MQTT_MAX_PACKET_SIZE` 1 MiB | same, before buffering | keep-alive × 1.5 |
| MQTT session state | `BB_MQTT_MAX_SUBSCRIPTIONS` 1000, `BB_MQTT_MAX_QUEUED_MESSAGES` 1000, 16-bit packet-identifier space | `BB_MQTT_MAX_SESSIONS` 10000 | Session Expiry Interval, swept *(see qualification 3)* |
| MQTT retained store | — | `BB_MQTT_MAX_RETAINED` 10000 | — |
| Connections | — | `BB_MAX_CONNECTIONS` (derived) | detect deadline, keep-alive |
| *Client* response head (HTTP/1.1) | `BB_CLIENT_HEAD_MAX_LINE` 8 KiB | `BB_CLIENT_HEAD_MAX_TOTAL` 64 KiB | `BB_CLIENT_HEAD_TIMEOUT` 30 s |
| *Client* response head (HTTP/2) | `BB_CLIENT_H2_MAX_FRAME_SIZE` 16 KiB | `BB_CLIENT_HEAD_MAX_TOTAL` 64 KiB, spent twice — encoded, per field block reassembled across CONTINUATION; and decoded, across every section on the stream — plus `BB_CLIENT_H2_MAX_HEADER_LIST_SIZE` 64 KiB decoded, per section, inside the decoder | `BB_CLIENT_HEAD_TIMEOUT` 30 s |
| *Client* response body | 64 KiB read (HTTP/1.1); `BB_CLIENT_H2_MAX_FRAME_SIZE` (HTTP/2) | `BB_CLIENT_BODY_MAX_TOTAL` **off** | `BB_CLIENT_BODY_TIMEOUT` 30 s + `BB_CLIENT_MIN_BODY_RATE` **off** (payload bytes; H2 per stream) |
| *Client* raw HTTP/2 stream | `BB_CLIENT_H2_MAX_FRAME_SIZE` 16 KiB | `BB_CLIENT_RAW_QUEUE_DEPTH` 1024 frames | — |
| *Client* WebSocket frame | `BB_CLIENT_WS_MAX_FRAME_PAYLOAD` 64 MiB | — | — |
| *Client* WebSocket message | per frame | `BB_CLIENT_WS_MAX_MESSAGE_SIZE` 16 MiB | — |
| *Client* send path | HTTP/2 peer `SETTINGS_MAX_FRAME_SIZE` | — deliberately no upload aggregate; the flow-control window is only in-flight credit | `BB_CLIENT_WRITE_TIMEOUT` 30 s per socket-drain/flow-control wait, not a whole-upload total; one-byte credit can make an upload indefinite |

One client bound is not in that grid, because it is not one of the three.
`BB_CLIENT_MAX_INTERIM_RESPONSES` (8) bounds how many `1xx` heads may precede
the final one, on either transport: a **count**, and what owns the aggregate of
the two per-head bounds above it, each of which is spent afresh on every
interim. It is also the only bound an HTTP/2 interim section with no fields
has — one of those charges nothing to `BB_CLIENT_HEAD_MAX_TOTAL`, which is
what owns the aggregate once fields are present.

Full descriptions: [Environment variables](../reference/env-vars.md).

### Four limits that are less obvious than they look

**Bound messages after reassembly and decompression.** A small frame can
inflate to a large message. `BB_WS_MAX_MESSAGE_SIZE` bounds inflation before
the oversized result is materialized; a frame cap alone does not.

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

- **`bounded-by-default`** — every path listed above is bounded with nothing
  configured. It is a statement about this page's rows, not about paths nobody
  has thought of yet.
- **`bounded-when-configured`** — the bounds exist but one or more default open.
- **`bounded-unit-only`** — individual units are bounded; totals are not.

The rungs are read against the rows above, so the table needs to say *whose*
path each row was. A protocol is not a role: the same HTTP/2 parser bounds a
request we were sent and a response we asked for, and only one of those came
from a peer who chose us.

| Role | Protocol | Posture | Notes |
|---|---|---|---|
| Server | HTTP/1.1 | `bounded-by-default` | see qualifications 1 and 2 below |
| Server | HTTP/2 | `bounded-by-default` | includes control-frame rate metering and PING-based liveness |
| Server | gRPC | `bounded-by-default` | own message bounds, plus everything HTTP/2 provides |
| Server | WebSocket | `bounded-by-default` | message bounded post-reassembly and post-inflation; a silent peer is probed with a PING and closed if it does not answer |
| Server | MQTT | `bounded-by-default` | limits are also *advertised* in CONNACK, so conforming clients stay inside them; see qualification 3 |
| Client | HTTP/1.1 | `bounded-when-configured` | two of its bounds ship off; see qualification 4 |
| Client | HTTP/2 | `bounded-when-configured` | the same two default-open bounds; see qualification 4 |

**Four qualifications, stated here rather than in a footnote**, because they
are the difference between the label and the whole truth:

1. **There is no total request-duration bound by default.**
   `BB_REQUEST_TIMEOUT` is `0`. A peer delivering at exactly the minimum body
   rate, up to the body cap, legally holds a connection for
   31,457,280 ÷ 240 = **131,072 seconds ≈ 36.4 hours**. Every bound above is
   doing its job the whole time — that is the composition of a size cap and a
   rate floor, not a hole in either. nginx and Kestrel have the same property.
   Set `BB_REQUEST_TIMEOUT` if your application has no long-lived requests.

2. **An auto connection cap is a descriptor budget, not a memory budget.**
   `auto` subtracts 64 reserved descriptors and those already open from
   `RLIMIT_NOFILE`. Set an explicit cap for the worker's memory and CPU budget,
   allowing for descriptors the application opens after acceptance starts.
   TLS handshakes count from accept. Excess connections wait in the accept
   queue; HTTP/1.1 and prior-knowledge h2c get best-effort 503, while ALPN-h2
   and raw connections close silently.

3. **An MQTT session that never expires is bounded by the total, not by the
   clock.** §3.1.2.11.2 defines a Session Expiry Interval of `0xFFFFFFFF` as
   *does not expire*, and BlackBull honours it. Such a session is collected by
   nothing; what bounds it is `BB_MQTT_MAX_SESSIONS`, at which a CONNECT for an
   unknown Client Identifier is refused with `0x97 (Quota Exceeded)`. Sessions
   with a finite interval are swept by a timer armed at the earliest pending
   deadline, so they cost nothing while none is pending. Sessions live in
   memory only and do not survive a restart.

4. **Client buffered-body and rate bounds default off.** Set
   `BB_CLIENT_BODY_MAX_TOTAL` when response size is predictable, and
   `BB_CLIENT_MIN_BODY_RATE` only for transfers expected to make sustained
   progress. Both apply to HTTP/1.1 and HTTP/2. Client send and WebSocket
   limits use client-owned settings, separate from server tuning.

## Defaults and deployment checklist

On out of the box:

- every limit in the invariant table except `BB_REQUEST_TIMEOUT` and the
  client's two, `BB_CLIENT_BODY_MAX_TOTAL` and `BB_CLIENT_MIN_BODY_RATE`;
- cap-hit logging on `blackbull.caps` — every refusal above emits a structured
  `WARNING` naming the limit, the requested value and the configured one, the
  client's included, on *each* protocol that enforces the bound (see
  [Logging](../guide/logging.md#cap-hit-log-blackbullcaps));
- MQTT limits advertised to clients in CONNACK, so a conforming client never
  reaches the enforcement path.

Worth setting for an internet-facing deployment:

| Setting | Why |
|---|---|
| `BB_REQUEST_TIMEOUT` | the only default-open bound; see qualification 1 |
| `BB_MAX_CONNECTIONS` | an explicit number bounds event-loop health, which the derived value does not.  Set it to the concurrency the deployment actually expects, particularly when serving WebSocket, where connections are long-lived by design |
| `BB_WS_MAX_MESSAGE_SIZE` | the default admits 16 MiB so the WebSocket conformance suite passes unconfigured; lower it if you do not serve huge messages |
| `BB_MAX_BODY_SIZE` | lower it if you accept no uploads |
| `BB_TCP_USER_TIMEOUT_MS` | evicts dead peers behind NATs without waiting for keepalives — BlackBull's own listeners only ([scope](../deployment/workers.md#socket-options-across-bind-paths)) |

The client's two turn on a different question — not *is this deployment
internet-facing* but *do I know what this peer should return*. Set
`BB_CLIENT_BODY_MAX_TOTAL` when you do, at half of what one response may
occupy, because the buffered body costs about **twice** the cap in peak
memory. `BB_CLIENT_MIN_BODY_RATE` asserts the peer is a transfer rather than a
stream, which an event stream or a long poll is not.

## Evidence

Every claim on this page is backed by a test or an external conformance suite.

| Claim | Evidence |
|---|---|
| Body size and rate bounds | `tests/conformance/http1/test_rfc9110_body_cap.py`, `tests/conformance/http2/test_rfc9113_body_cap.py`, `tests/unit/test_body_rate_limit.py` |
| A slow drip is closed at the shipped default | `tests/conformance/http1/test_rfc9112_slowloris.py::TestBodyTrickle` |
| WebSocket message bounds, including that a bomb is never materialised | `tests/unit/test_ws_message_bounds.py` |
| MQTT packet, backlog and retained-store bounds | `tests/unit/test_mqtt_resource_bounds.py` |
| MQTT session count, subscription count and expiry sweep | `tests/unit/test_mqtt_session_bounds.py` |
| HTTP/2 time bounds, including that a responsive peer is never closed | `tests/unit/test_h2_time_bounds.py` |
| WebSocket liveness probe, including that an answering peer is never closed | `tests/unit/test_ws_liveness_probe.py` |
| Frame-rate metering | `tests/unit/test_rate_window.py`, `tests/unit/test_frame_rate_metering.py` |
| Derived connection cap | `tests/unit/test_max_connections_default.py` |
| Protocol conformance | h2spec (HTTP/2 + HPACK), Autobahn (WebSocket), http11probe — see [Conformance](conformance.md) |
| Client response bounds, and that a peer which stops is abandoned rather than waited on | `tests/unit/client/test_http1_client_head_bounds.py`, `tests/unit/client/test_http1_client_desync_and_deadline.py`, `tests/unit/client/test_http2_client_response_bounds.py` |
| Client rate floor, including HTTP/2 stream isolation and DATA payload accounting | `tests/unit/client/test_http1_client_rate_floor.py`, `tests/unit/client/test_http2_client_rate_floor.py` |
| Client-owned transport and WebSocket bounds | `tests/unit/client/test_client_owned_bounds.py`, `tests/unit/test_cap_log_sites.py` |
| Every client bound names itself on `blackbull.caps`, on every protocol that enforces it | `tests/unit/test_cap_log_sites.py` — the per-cap tests, plus `test_client_caps_are_wired_on_every_protocol_that_enforces_them` and `test_every_client_env_var_has_a_verdict` |
| A buffered client response body costs about twice the cap | `tests/unit/client/test_client_body_buffer_cost.py` |

## Non-claims

The limits do not cover volumetric floods or arbitrary application resource
use. No third-party audit or red-team exercise establishes completeness.
Treat the tables as audited paths, not proof of absence of other paths.

- **The server sends `100 (Continue)` only to HTTP/1.1.** An HTTP/1.0 client
  gets no interim response at all and its `Expect` is ignored (COMP-NO-1XX-HTTP10),
  so a client that waits for the signal stalls until its own timeout. The
  server also never originates a `103 (Early Hints)`: it sends what the
  application sends and infers nothing before then.

On the async HTTP client specifically, and after the server ones so the
contrast is visible:

- **A peer that sends content where RFC 9112 §6.3 rule 1 says there is none —
  a 204, a 304 or a HEAD response carrying a body — is not refused over
  HTTP/1.1, unless it also declares a boundary the rule forbids it to
  have.** Those octets are left
  unread and are parsed as the start of the next response: a `ProtocolError`
  in the usual case, and a response taken for another when the leftover
  happens to look like one. The reader trusts the rule rather than the peer
  here; a 205 is consumed to its declared length precisely because the rule
  does not cover one. HTTP/2 refuses such frames instead, because frames are
  self-delimiting and no boundary is at stake. The one exception is the next
  bullet: a message that names the boundary the rule forbids it to have is
  refused, because there the leftover and the next response cannot be told
  apart at all.
- **A 204 or a 1xx that declares a boundary is the one shape that is
  refused.** RFC 9112 §6.1 forbids `Content-Length` and `Transfer-Encoding`
  in one, so what follows cannot be told apart from the next response; the
  connection is retired and the reader refuses it. A 204 with no boundary, or
  with `Content-Length: 0`, still leaves its octets to the next response:
  nothing is declared, so nothing is refused.

- **It does not follow redirects and does not pool connections.** Neither
  exists in `blackbull/client/` — so neither is bounded *or* unbounded, and a
  report that one of them is unsafe is a feature request.
- **A buffered response body costs about twice the cap in peak memory.**
  Reaching ~1× means `stream()`, which exposes no status, no headers and sits
  outside the cap — so today a caller can have ~1× *or* all three, never both.
Found something? Please open an issue at
[github.com/TOKUJI/BlackBull](https://github.com/TOKUJI/BlackBull/issues).

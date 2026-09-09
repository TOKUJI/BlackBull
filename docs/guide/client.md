# The async client

`blackbull.client` is BlackBull's own protocol-layer HTTP client: pure Python,
the same wire code the server uses, driven from the caller's side. It exists so
that BlackBull can be tested against its own bytes and pointed at deliberately
broken peers, and it is exported because those are useful jobs outside the test
suite too.

The generated API reference — the `blackbull.client` pages under **API** in the
navigation — says what each name takes and returns. This page answers the four
things it cannot: **which client to construct, what an `async with` block owns,
what a call can raise, and what the defaults do and do not bound.**

If you are here to write a test, start with
[Testing](testing.md#end-to-end-with-blackbulls-clients) — it has the fixture,
the four-client selection table, and worked round-trips. This page is what you
read when one of those clients surprises you.

## Choosing a client

[Testing](testing.md#end-to-end-with-blackbulls-clients) carries the selection
table. Two things it leaves out are worth having in front of you before you
pick.

### `Client` never gives you h2c

`Client` picks a protocol by reading the negotiated ALPN protocol off the TLS
object. With the default `ssl=None` there is no TLS, therefore no ALPN,
therefore nothing to read — and the dispatcher falls back to HTTP/1.1. It is
not that h2c is unsupported; `HTTP2Client` speaks it directly. It is that the
dispatcher has no way to *select* it.

```python
from http import HTTPMethod
from blackbull.client import Client, HTTP1Client, HTTP2Client

# No TLS, so no ALPN: the dispatcher hands back HTTP/1.1.
async with Client('127.0.0.1', port) as c:
    assert isinstance(c, HTTP1Client)

# h2c on that same plaintext port — name the client instead of asking.
async with HTTP2Client('127.0.0.1', port) as c:
    res = await c.request(HTTPMethod.GET, '/')
```

So: **pass a TLS context and let ALPN decide, or name the protocol yourself.**
Reaching for `Client` on a plaintext port to "let it work out" silently gets
you HTTP/1.1 every time.

### The two clients are not the same size

They share `request()` and a `ClientResponse`, and diverge past that:

- **`HTTP1Client.stream()` has no HTTP/2 counterpart.** It yields body chunks
  without buffering, which is how a response larger than memory is read. Every
  HTTP/2 response body is accumulated whole — which is why
  `BB_CLIENT_BODY_MAX_TOTAL` matters more there (see [below](#what-is-not-bounded)).
- **`body=` accepts an async generator on HTTP/1.1 only.** `HTTP2Client.request`
  takes `bytes`.
- **HTTP/1.1 adds the raw-wire primitives** — `send_request_line`,
  `send_header_line`, `send_chunk`, `wire_buffer`, `handoff` — and HTTP/2 adds
  the frame ones: `frame_factory`, `send_raw_frame`, `register_raw_stream`.
- **`WebSocketClient` (RFC 6455) and `WebSocketH2Client` (RFC 8441) are separate
  front doors.** WebSocket is deliberately outside ALPN dispatch: it is an
  HTTP/1.1 upgrade or an Extended CONNECT, not something a handshake picks for
  you.

## A client is one connection

Every client is an async context manager over exactly one transport. There is
no pool, no reconnect, and no request-level retry. `host` and `port` are fixed
at construction, so a redirect to another origin is yours to follow.

**`__aenter__` opens the transport** under `connect_timeout` (30 s by default;
[Testing](testing.md#end-to-end-with-blackbulls-clients) covers the number and
how to opt out) and then does whatever the protocol owes on connect. For
HTTP/1.1 that is nothing. For HTTP/2 it is the connection preface, one
`SETTINGS` frame, and a background receive loop — which is why an `HTTP2Client`
has a task running whether or not you have a request in flight, and why
`scenario_mode` exists to suppress all of it.

**Requests inside the block reuse that connection.** On HTTP/1.1 they are
sequential and persistence is negotiated per exchange; `Connection: close` from
either end retires it. On HTTP/2 they are concurrent streams on one connection,
so `asyncio.gather` over `request()` is the normal shape rather than a trick.

**`__aexit__` is terminal, and it does not wait for in-flight work.** The
HTTP/2 client cancels its receive loop first, then fails every response still
pending with `ConnectionError('client connection closed')` — so a `gather`
abandoned by an exception elsewhere in the block raises rather than hanging.
Both clients then close the transport best-effort. A closed client cannot be
re-entered: the next call raises `ConnectionError`, naming the close you
performed rather than blaming the peer.

The one exception is `handoff()`, which transfers a CONNECT or 101 transport to
an `HTTP1UpgradeSession` and moves ownership of closing it with it — see
[Testing](testing.md#connect-tunnels-and-protocol-upgrades).

## What a call raises

Everything the client itself refuses derives from `ClientError`:

```
ClientError
├── ProtocolError      the peer's message was malformed, or ours would be
├── ConnectionError    the transport went away, or this client closed it
├── ResponseTooLarge   a byte budget of ours was passed (carries .seen)
├── HandshakeError     a WebSocket or HTTP/2 handshake failed
└── StreamReset        the peer sent RST_STREAM (carries .stream_id, .error_code)
```

Roughly: `ProtocolError` and `ResponseTooLarge` come out of reading a response
on either protocol; `ConnectionError` out of any call once the peer has gone or
the context has exited; `StreamReset` only from HTTP/2, only from the
`request()` whose stream was reset; `HandshakeError` only from
`WebSocketClient.connect()` and `WebSocketH2Client`'s Extended CONNECT.

### A stall is not a `ClientError`, on purpose

Every time bound in the client — the connect deadline, the head and body
deadlines — raises a bare `TimeoutError`. That is a deliberate split, and it is
the useful one when you are diagnosing a peer: **a peer that stalled and a peer
that answered wrongly are different bugs**, and catching them together throws
away the distinction.

```python
from blackbull.client import ClientError

try:
    res = await client.request(HTTPMethod.GET, '/report')
except TimeoutError:
    ...     # the peer went quiet — nothing was refused
except ClientError:
    ...     # the peer spoke, and what it said was refused
```

`ClientError` is the right granularity when you do not intend to act
differently per cause — a test asserting "this peer is not usable", a retry
wrapper. Catch the leaves when the recovery differs: raising a budget for
`ResponseTooLarge` is a sensible response to it and a nonsensical response to
`ProtocolError`.

### The WebSocket read path is outside the hierarchy

Worth knowing before you write `except ClientError` around a WebSocket loop:
the client's WebSocket reader is the server's `WebSocketRecipient`, and it
raises **`blackbull.server.recipient.ProtocolError`, which is not a
`ClientError`.** The name collides with `blackbull.client.ProtocolError` and the
classes are unrelated.

So a frame that breaches `BB_CLIENT_WS_MAX_FRAME_PAYLOAD`, a message that
breaches `BB_CLIENT_WS_MAX_MESSAGE_SIZE`, invalid UTF-8 in a TEXT message, or a
fragmented control frame all surface from `session.receive()` as an exception
`except ClientError` will not catch. The disconnect event is delivered first and
the exception on the receive after it. Until the two hierarchies are joined,
catch `Exception` around a WebSocket receive loop, or import the server class
explicitly and know which one you have.

## What the client waits for

The client's timeout design is one idea applied at two layers, and reading it
as one rule is how people end up surprised by it.

**At the connection, a peer may be silent for as long as it likes.** The HTTP/2
frame reader never bounds the wait for the *next* frame to begin. It must not:
server-sent events, a streaming response, a quiet multiplexed connection — all
of those are a peer behaving correctly. What it does bound is the wait for a
frame to *finish*: once nine header bytes have arrived the peer has committed to
a payload length, so silence after that is an abandoned frame rather than an
idle connection, and the read ends the way EOF does.

**At a stream that has asked a question, both waits are bounded.** A response
runs one clock through two consecutive phases, never both at once:

| phase | runs from | until | bound |
|---|---|---|---|
| head | the request is fully on the wire | the final (`>= 200`) response head | `BB_CLIENT_HEAD_TIMEOUT` |
| body | that head | the response ends | `BB_CLIENT_BODY_TIMEOUT` |

The head phase starts only once the request is fully sent — a send parked on
our own flow-control window is our backpressure, not the peer's silence. A
`1xx` interim response neither stops the head clock nor starts the body one:
it announces that the peer is still working, which is the case the deadline is
there for. The body phase is re-armed by each DATA frame that delivers payload,
so it bounds a *gap*, not a duration: a long response may outlast the deadline
many times over as long as no single gap does. It is per stream, because a
connection-wide clock is reset by any traffic and a busy stream would shelter a
stalled one.

**The consequence to plan for:** an HTTP/2 peer that legitimately defers its
response head past 30 seconds — long polling, or a query it computes before
flushing headers — is refused at the *stream* level, even though the connection
would have waited forever. `BB_CLIENT_HEAD_TIMEOUT=0` is the opt-out, and it
disables the HTTP/1.1 arm along with it. HTTP/1.1 has always refused that shape
at this number; the HTTP/2 side was brought into line because the *peer*, not
you, chooses which client `Client` hands back, and a limit answerable on one
path only is a limit you cannot configure.

## What the defaults bound

The reasoning for every number lives in
[Environment variables](../reference/env-vars.md#async-http-client) and is not
repeated here. What that table does not show at a glance is the **shape** —
which question each knob answers:

| question | knobs |
|---|---|
| how big may **one** thing be | `BB_CLIENT_HEAD_MAX_LINE`, `BB_CLIENT_WS_MAX_FRAME_PAYLOAD`, `BB_CLIENT_H2_MAX_FRAME_SIZE` |
| how big may the **total** be | `BB_CLIENT_HEAD_MAX_TOTAL`, `BB_CLIENT_H2_MAX_HEADER_LIST_SIZE`, `BB_CLIENT_WS_MAX_MESSAGE_SIZE`, `BB_CLIENT_RAW_QUEUE_DEPTH`, `BB_CLIENT_BODY_MAX_TOTAL` *(off)* |
| how **long** may it take | `BB_CLIENT_HEAD_TIMEOUT`, `BB_CLIENT_BODY_TIMEOUT`, `BB_CLIENT_WRITE_TIMEOUT`, `connect_timeout=` *(an argument, not a knob)* |
| how **many** may there be | `BB_CLIENT_MAX_INTERIM_RESPONSES` |
| how **slowly** may it arrive | `BB_CLIENT_MIN_BODY_RATE` *(off)*, paced by `BB_CLIENT_MIN_BODY_RATE_GRACE` |
| not a bound at all | `BB_CLIENT_H2_ENABLE_PUSH` — a conformance switch |

Reading it that way makes the gaps visible, and the gaps are the point.

### What is not bounded

A client is not a server, and the defaults say so. **A server bounds what
strangers may push into your process; a client bounds what you went and asked
for.** Two axes are therefore off out of the box, and you should turn them on
deliberately rather than assume they are on:

- **The response body has no total.** `BB_CLIENT_BODY_MAX_TOTAL` defaults to
  `0`. On HTTP/1.1 the escape is `stream()`, which never accumulates. On HTTP/2
  there is no `stream()`, so with the default every h2 response body is
  buffered whole with no total watching it. If you know what your peer should be
  returning, set it — and size it against *twice* the value, which is what
  reaching the cap costs in peak memory.
- **A trickling peer is not refused.** `BB_CLIENT_MIN_BODY_RATE` defaults to
  `0`. The body deadline abandons a peer that *stops*; one that sends a byte
  before every deadline satisfies it forever.

Two more are unbounded by design rather than by default:

- **An upload has no total time.** `BB_CLIENT_WRITE_TIMEOUT` bounds one socket
  drain or one flow-control credit wait. A peer granting one byte at a time can
  keep a large upload going indefinitely.
- **A quiet connection is never closed by the client.** Only a stream with a
  request outstanding is on a clock.

Every bound that fires logs one `WARNING` on the `blackbull.caps` logger naming
itself — see [Logging](logging.md#cap-hit-log-blackbullcaps). For a client that
is most of the value: it turns "the request failed" into "this limit, this
number, this peer".

## Driving a misbehaving peer

`execute_scenario` and the `scenario_mode=True` flag are for putting a
deliberately broken client on the wire — slowloris, a lied-about frame length,
a rapid-reset burst. `scenario_mode` is the one place the two clients genuinely
diverge, and [Fault injection](fault_injection.md#quick-start-http2-client-side)
explains why and shows the vocabulary. Note that a scenario never raises; the
outcome lands on `ScenarioResult`.

# Fault injection

BlackBull ships a deliberate-misbehaviour toolkit under
`blackbull.fault_injection` for testing other HTTP implementations
against bad-server / bad-client behaviour.

## What it covers

Both roles, on both HTTP protocols — the grid, rather than a count,
because which cell you need depends on which side you are testing:

| | Broken **client** → your server | Broken **server** → your client |
|---|---|---|
| **HTTP/1.1** | ✅ `HTTP1Client.execute_scenario` + `oracle_h1` | ✅ `H1FaultServer` + catalogue |
| **HTTP/2** | ✅ `HTTP2Client.execute_scenario` + catalogue | ✅ `H2FaultServer` + catalogue |
| **WebSocket** | ✗ not implemented | ✗ not implemented |
| **gRPC** | — | ⚠ transport only, via `H2FaultServer` |
| **MQTT** | — | — out of scope |

* **Broken client → your server** drives a target server through
  slowloris-style misbehaviour: on HTTP/1.1 trickled bytes, partial
  headers, mid-request idle, abrupt RST; on HTTP/2 a preface that never
  arrives, a Rapid Reset burst, a PING or SETTINGS flood, a header block
  opened and abandoned, a frame header that lies about its length.  The
  `oracle_h1` half compares two servers' responses to the same scenario, so
  you can diff your server against a reference such as nginx.
* **Broken server → your client** emits misbehaviour at a connected
  client: on HTTP/1.1 a trickled status line, a `Content-Length` that
  lies, a chunked body that stops mid-chunk; on HTTP/2 half-closed
  streams, exhausted flow-control windows, illegal SETTINGS, weird
  frame sequences.  Both are backed by a named catalogue you can
  `parametrize` over.

**Where it stops, stated because you would otherwise plan around it:**

* **gRPC** has no fault injection of its own.  gRPC rides HTTP/2, so
  `H2FaultServer` already misbehaves *beneath* a gRPC client at the
  transport layer — but gRPC-specific faults (an invalid `grpc-status`,
  a malformed length-prefixed message, a trailers-only response) cannot
  be expressed.
* **MQTT** has none and is not planned to.  The broker is server-side by
  design and fault injection for it is out of scope.
* **WebSocket** has none in either direction.

This module is an opt-in testing instrument.  It refuses to start in a
production context (`BLACKBULL_ENV=production`, or `BB_PRODUCTION` as an
explicit override) so the deliberate-misbehaviour code path cannot
accidentally fire on a production deployment, and both fault servers
refuse a non-loopback bind without `allow_remote=True`.

## Install

```bash
pip install 'blackbull[fault-injection]'
```

The extra adds `cryptography` (for the self-signed TLS helper) and
`httpx[http2]` (so the canonical example runs out of the box).
`H2FaultServer` itself only needs the stdlib — if you drive it over
plaintext h2c you can skip the extra.

## Quick start — HTTP/2 server-side

`H2FaultServer` accepts an `ssl.SSLContext` so it can negotiate
HTTP/2 over TLS with real clients (httpx, curl, ...) via ALPN.  Use
`make_self_signed_h2_context()` to spin up a localhost-only TLS
context with ALPN ``h2`` advertised:

```python
import pytest
from blackbull.fault_injection import H2FaultServer, make_self_signed_h2_context
from blackbull.fault_injection.catalogue import (
    half_closed_stream_no_data,
    exhausted_window_zero_initial,
    settings_max_frame_size_below_minimum,
    headers_continuation_dropped,
)

@pytest.fixture
async def fault_server(request):
    scenario = request.param()
    ssl_ctx = make_self_signed_h2_context()
    async with H2FaultServer(scenario=scenario, ssl_context=ssl_ctx) as srv:
        yield srv

@pytest.mark.parametrize('fault_server', [
    half_closed_stream_no_data,
    exhausted_window_zero_initial,
    settings_max_frame_size_below_minimum,
    headers_continuation_dropped,
], indirect=True)
async def test_my_client_survives_each_catalogue_scenario(fault_server):
    client = MyH2Client(fault_server.url, verify=False)
    # The exact assertion depends on the scenario — see the catalogue
    # docstrings for the expected client-side behaviour.  Most reduce
    # to: client must error within a bounded time, not hang forever.
    with pytest.raises((TimeoutError, MyClient.ProtocolError)):
        await asyncio.wait_for(client.get('/'), timeout=2.0)
```

Omitting `ssl_context=` runs the server as plaintext h2c — fine for
prior-knowledge clients, but httpx / curl / hyper-h2 only negotiate
HTTP/2 via ALPN over TLS, so most real clients need the TLS path.

After each connection, `fault_server.last_result` is a
`ScenarioH2Result` carrying step-completion count, byte counters,
whether a `WaitForClientFrame` step timed out, and the
elapsed wall time.

## The four spec-grade catalogue categories

| Category | Catalogue builder | What the server does |
|---|---|---|
| Half-closed streams | `half_closed_stream_no_data()` | Sends HEADERS without END_STREAM, then nothing.  Client must time out. |
| Exhausted windows | `exhausted_window_zero_initial()` | Advertises SETTINGS_INITIAL_WINDOW_SIZE=0 then never grants WINDOW_UPDATE.  Client must respect backpressure. |
| Custom / illegal SETTINGS | `settings_max_frame_size_below_minimum()` | Advertises SETTINGS_MAX_FRAME_SIZE below the RFC 9113 §6.5.2 floor (16384).  Client must treat as PROTOCOL_ERROR. |
| Weird frame sequences | `headers_continuation_dropped()` | Sends HEADERS without END_HEADERS, then no CONTINUATION.  Client must close with PROTOCOL_ERROR. |

Stack `parametrize` over the catalogue to assert resilience across
all four categories with a few lines of test code.

## Building your own scenario

A `ScenarioH2` is a tuple of typed steps the server walks per
connection:

```python
from blackbull.fault_injection import (
    ScenarioH2, SendRawBytes, WaitForClientFrame,
    H2Sleep, H2Abort, CloseGracefully,
)

scenario = ScenarioH2(
    steps=(
        # Wait for the client to open stream 1.
        WaitForClientFrame(
            match={'type': 'HEADERS', 'stream_id': 1},
            timeout=5.0,
        ),
        # Emit a malformed frame (illegal frame type 0xFF).
        SendRawBytes(b'\x00\x00\x00\xff\x00\x00\x00\x00\x01'),
        # Pause so the client has time to react.
        H2Sleep(0.5),
        # Tell the client we're done.
        CloseGracefully(error_code=1, last_stream_id=1),
    ),
    send_preface=True,
    initial_settings=((0x5, 16383),),  # MAX_FRAME_SIZE below the floor
)
```

The supported steps:

* `SendFrame(frame)` — emit a typed `FrameBase` instance through the
  framework's frame factory.  Most frames carry their own type byte,
  flags, stream id, and payload.
* `SendRawBytes(data, byte_interval=0.0)` — escape hatch for bytes
  the typed factory cannot construct (illegal type bytes, oversize
  frames, malformed length fields).  `byte_interval > 0` trickles
  byte-by-byte for slowloris patterns.
* `WaitForClientFrame(match, timeout=5.0)` — pause until an inbound
  frame matches the declarative match dict (`type`, `stream_id`,
  `flags_set`, `flags_unset`).  On timeout the scenario advances and
  the result's `wait_timed_out` flips to `True`.
* `H2Sleep(duration)` — idle.
* `H2Abort()` — hard-close the transport (RST on Linux).
* `CloseGracefully(error_code, last_stream_id)` — GOAWAY then close.

`H2Abort` and `CloseGracefully` are terminators; subsequent steps
short-circuit.

## Quick start — HTTP/1.1 client-side

```python
from blackbull.client import HTTP1Client
from blackbull.fault_injection import Scenario, SendBytes, Sleep, ReadResponse

# Send a request one byte every 200 ms — classic slowloris.
trickle = Scenario(steps=(
    SendBytes(b'GET / HTTP/1.1\r\nHost: target\r\n\r\n', byte_interval=0.2),
    ReadResponse(timeout=10.0),
))

async with HTTP1Client('127.0.0.1', 8080) as client:
    result = await client.execute_scenario(trickle)
    assert result.response is not None or result.timed_out
```

The matching differential oracle
(`blackbull.fault_injection.run_scenario`) drives the same scenario
against two servers and categorises whether they agree, disagree,
or both rejected.

## Quick start — HTTP/1.1 server-side

Point your own HTTP/1.1 client at a server that is wrong on purpose:

```python
import pytest
from blackbull.fault_injection import (
    H1FaultServer, H1SCloseGracefully, H1SSendRawBytes,
    ScenarioH1Server, WaitForRequest,
)

@pytest.mark.asyncio
async def test_my_client_rejects_a_short_body():
    scenario = ScenarioH1Server(steps=(
        WaitForRequest(),
        # Declares 100 bytes, sends 5, then closes.
        H1SSendRawBytes(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort'),
        H1SCloseGracefully(),
    ))
    async with H1FaultServer(scenario) as srv:
        with pytest.raises(Exception):
            await my_client.get(f'http://{srv.host}:{srv.port}/')
```

!!! note "Why the `H1S` prefix"

    Three scenario vocabularies live in this package — HTTP/1.1 client-side,
    HTTP/1.1 server-side, HTTP/2 server-side — and they share step names
    because they describe the same shapes of misbehaviour.  The package
    exports them role-qualified so an import cannot silently hand one half's
    step to the other half's executor: `H1S…` is HTTP/1.1 server-side, `H2…`
    is HTTP/2, and the unprefixed `Abort` / `Sleep` are the HTTP/1.1 *client*
    vocabulary that had the names first.

    Importing from the submodule directly (`from
    blackbull.fault_injection.scenario_h1_server import SendRawBytes`) gives
    you the unprefixed names if you prefer them.

The named cases live in `blackbull.fault_injection.catalogue.h1`:

| Case | What the server does |
|---|---|
| `content_length_overstated` | declares 100 bytes, sends 5, closes |
| `content_length_understated` | declares 2, sends a whole second response after it |
| `conflicting_content_length` | two `Content-Length` headers that disagree |
| `chunked_stops_mid_chunk` | announces a 5-byte chunk, sends 2, EOF |
| `chunked_never_terminates` | well-formed chunks, no zero-length terminator |
| `trickled_status_line` | a *correct* response, one byte at a time |
| `headers_never_end` | header lines forever, never the blank line |
| `closed_without_response` | accepts the request, resets the connection |
| `silent_after_request` | holds the connection open, writes nothing |

Two of those are not failures to be caught.  `trickled_status_line` is
correct HTTP delivered slowly — a client that rejects it has a bug of its
own.  `content_length_understated` should *succeed*: RFC 9112 says read
exactly `Content-Length` octets, so returning the 2-byte body is the
conformant answer.  The hazard is the next exchange, not this one — the
surplus is a whole second response left in the buffer, which a keep-alive
client reusing the connection will parse as the reply to a request it has
not sent.

### Every scenario is raw bytes, deliberately

There is no typed `SendResponse` step, and both fault servers assemble
their own output rather than calling BlackBull's production send path.
That is load-bearing rather than stylistic: **a fault server built on the
production serialiser cannot emit a fault that serialiser has**, so the
one bug class it would be least able to find is the one in the code it
shares.  The HTTP/2 half carries its own frame encoder for the same
reason.

## Quick start — HTTP/2 client-side

Drive your own HTTP/2 **server** through misbehaviour a real client can
produce:

```python
import pytest
from blackbull.client import HTTP2Client
from blackbull.fault_injection.catalogue.h2_client import rapid_reset_burst

@pytest.mark.asyncio
async def test_my_server_meters_rapid_reset():
    async with HTTP2Client('127.0.0.1', my_port) as client:
        result = await client.execute_scenario(rapid_reset_burst())
    # The scenario never raises; everything lands on the result.
    assert result.exception is None
```

The vocabulary mirrors the HTTP/1.1 client side — `SendBytes`,
`ReadResponse`, `Sleep`, `Abort`, and `ScenarioResult`'s field names — and
adds two steps HTTP/1.1 has no use for:

| Step | Why HTTP/2 needs it |
|---|---|
| `SendPreface` | HTTP/1.1 has no connection preface.  A *step* rather than a flag, because a client scenario may want to delay it, split it, or never send it |
| `SendFrame` | HTTP/2 is framed where HTTP/1.1 is a byte stream.  `declared_length` sets the header's length independently of the payload — the direct way to say "the peer lied about how much is coming" |

Eleven named cases ship in `blackbull.fault_injection.catalogue.h2_client`,
drawn from the HTTP/2 rows of the project's own attack-surface work so the
names line up with the defences that answer them: `rapid_reset_burst`
(CVE-2023-44487), `ping_flood` (CVE-2019-9512), `settings_flood`
(CVE-2019-9515), `empty_continuation_flood` (the CVE-2024-27983 shape),
`header_block_never_finished`, `data_frame_lies_about_length`,
`unknown_frame_type`, `settings_ack_with_payload`, `preface_never_arrives`,
`preface_trickled`, `abort_mid_header_block`.

Three of them end with a long `Sleep` because *holding* the connection is
the fault they stage; what ends those is your server's own deadline.

## Safety locks

Two locks ensure the deliberate-misbehaviour code path is unreachable
from a production process:

1. `BB_PRODUCTION=1` in the environment causes `H2FaultServer`'s
   constructor to raise `H2FaultServerError`.
2. Binding to a non-localhost interface raises unless
   `allow_remote=True` is passed.  The misbehaviour mode is for
   local-loop tests.

The HTTP/1.1 client-side scenario is benign by construction (it is a
client, not a server-side code path) and carries no equivalent lock.

## Examples

One walkthrough ships with the framework and covers the whole grid:

[`examples/fault_injection.py`](https://github.com/TOKUJI/BlackBull/blob/master/examples/fault_injection.py)

| Cell | What it runs |
|---|---|
| **A** | A broken *client* against a real server — slowloris trickle, partial-headers idle, abrupt RST — driven at a stdlib `http.server` in a background thread.  No third-party deps |
| **B** | A broken *server* against real clients: all nine HTTP/1.1 catalogue cases, driven **twice**, once with BlackBull's `HTTP1Client` and once with `httpx`.  The two columns sit side by side because that is the point — if our client and our fault server ever agree on something wrong, an independent implementation is what notices |
| **C** | A broken *server* against a real HTTP/2 client — every HTTP/2 catalogue case against `httpx` over the self-signed TLS context |
| **D** | Prints what is **not** implemented, and why, so the gap is visible from the same output as the coverage |
| **E** | The same scenarios as **JSON Lines** — serialised, round-tripped, and one loaded from hand-written JSON and executed |

It was previously two files, one per protocol.  A file per cell would have
meant four, and the next cell five.

Cells B and C need `pip install 'blackbull[fault-injection]'`; without it
they report themselves skipped rather than failing.

Cell B's output is worth reading for the two rows that come back **200**:
`trickled_status_line` is correct HTTP delivered slowly, and
`content_length_understated` is the conformant answer to a response that
understates its body — the hazard there is the *next* exchange, not this
one.  A fault catalogue is not a list of things a client must reject.

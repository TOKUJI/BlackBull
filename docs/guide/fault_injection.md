# Fault injection

BlackBull ships a deliberate-misbehaviour toolkit under
`blackbull.fault_injection` for testing other HTTP implementations
against bad-server / bad-client behaviour.  Two directions are
supported:

* **Client → server (HTTP/1.1)** — a programmable client that drives
  a target server through slowloris-style misbehaviour: trickled
  bytes, partial headers, mid-request idle, abrupt RST.  Driven
  through `HTTP1Client.execute_scenario`; the
  `blackbull.fault_injection.oracle_h1` half compares two servers'
  responses to the same scenario.
* **Server → client (HTTP/2)** — a programmable server (`H2FaultServer`)
  that emits deliberate misbehaviour toward a connected client:
  half-closed streams, exhausted flow-control windows, illegal
  SETTINGS, weird frame sequences.  Backed by a named catalogue you
  can `parametrize` over.

This module is an opt-in testing instrument.  It refuses to start
when `BB_PRODUCTION` is set in the environment so the
deliberate-misbehaviour code path cannot accidentally fire on a
production deployment.

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

Two end-to-end walkthroughs ship with the framework, one per
direction:

* [`examples/scenario_h2_fault_injection.py`](https://github.com/TOKUJI/BlackBull/blob/master/examples/scenario_h2_fault_injection.py)
  — runs every catalogue scenario against ``httpx`` (over the
  self-signed TLS context) and prints both what the server emitted
  and how httpx reacted (``LocalProtocolError`` /
  ``RemoteProtocolError`` / ...).  Requires ``pip install 'httpx[http2]'``.
* [`examples/scenario_h1_fault_injection.py`](https://github.com/TOKUJI/BlackBull/blob/master/examples/scenario_h1_fault_injection.py)
  — runs a handful of misbehaving client scenarios (slowloris
  trickle, partial-headers idle, abrupt RST) against a stdlib
  ``http.server.BaseHTTPRequestHandler`` running in a background
  thread, and prints the resulting ``ScenarioResult`` for each.
  No third-party deps.

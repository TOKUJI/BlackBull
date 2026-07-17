# Edge inference serving

A recurring deployment shape for local ML models: a
Raspberry-Pi-class box (often ARM, often headless) runs a model and
has to serve two very different kinds of traffic at once —

- **interactive clients** — browser tabs, `curl`, `httpx` — that call
  an HTTP endpoint and want the answer *streamed token by token* as
  it is generated, and
- **devices** — sensors, cameras, controllers — that report telemetry
  and drop batch jobs over **MQTT**, the protocol they already speak.

The conventional stack for this is three or four moving parts: an
ASGI server for HTTP, a reverse proxy in front of it for HTTP/2, a
separate MQTT broker, and whatever glue keeps them coordinated. On
an edge box every extra part is another daemon to provision, another
config file, and — on ARM or RISC-V — potentially another native
dependency to cross-compile.

BlackBull's bet is that one process can carry the whole surface:

```
                        one Python process
┌──────────────────────────────────────────────────────────┐
│  :8000   HTTP/1.1 · HTTP/2 (h2c, or ALPN with TLS)       │
│     GET /generate   ──►  SSE token stream                │
│     GET /devices    ──►  latest readings (JSON)          │
│                                                          │
│  :1883   MQTT 5 broker                                   │
│     sensors/{device}/{metric}  ──►  tap → in-memory      │
│     jobs/#          ──►  $share/… round-robin work queue │
└──────────────────────────────────────────────────────────┘
    browsers · curl · httpx           devices · workers
```

Everything above is pure Python — no C extensions, so `pip install
blackbull` completes on any architecture CPython runs on, with no
build step and nothing to cross-compile. The runnable version of
this page is
[`examples/edge_inference.py`](https://github.com/TOKUJI/BlackBull/blob/master/examples/edge_inference.py).

## The serving surface: SSE token streaming

Token-streaming inference is a plain streaming handler: iterate the
model, yield one SSE event per token
(see [Streaming](streaming.md) for the full surface):

```python
from blackbull import BlackBull, EventSourceResponse

app = BlackBull()

@app.route(path='/generate')
async def generate(prompt: str = 'Hello, edge'):
    async def events():
        async for token in fake_model(prompt):
            yield {'event': 'token', 'data': token}
        yield {'event': 'done', 'data': ''}
    return EventSourceResponse(events())
```

`prompt` resolves from the query string, which keeps the endpoint
compatible with the browser's `EventSource` API (GET-only). Each
`yield` is written to the wire as it happens — transport
backpressure, not buffering, paces the stream — so the first token
reaches the client while the rest are still being generated.

Concurrent generations are where HTTP/2 earns its place: several
`/generate` streams multiplex over one connection instead of
queueing head-of-line behind each other. No extra setup is needed —
cleartext **h2c** is detected from the connection preface:

```bash
curl --http2-prior-knowledge -N 'http://localhost:8000/generate?prompt=hi'
```

Browsers require HTTP/2 over TLS; pass `certfile=` / `keyfile=` to
`app.run` and ALPN negotiates `h2` on the same port
([TLS deployment](../deployment/tls.md)).

### Swapping in a real model

The example's `fake_model` is an async generator that sleeps between
tokens. A real model is *CPU-bound* (or NPU/GPU-bound behind a
blocking call), and running it inline would stall the event loop —
every other stream, and the MQTT broker, would freeze between
tokens. Keep the loop free by pushing inference off-thread and
draining tokens through a queue:

```python
import asyncio

async def real_model(prompt: str):
    q: asyncio.Queue[str | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def run():                      # blocking generation, worker thread
        for token in llm.generate(prompt):        # llama-cpp, ONNX, ...
            loop.call_soon_threadsafe(q.put_nowait, token)
        loop.call_soon_threadsafe(q.put_nowait, None)

    task = asyncio.create_task(asyncio.to_thread(run))
    try:
        while (token := await q.get()) is not None:
            yield token
    finally:
        task.cancel()
```

The serving code around it — the route, the SSE framing, the
multiplexing — is unchanged.

## Device ingest: the broker you already run

`MQTTExtension` puts a full [MQTT 5 broker](mqtt.md) in the same
process. Devices connect to `:1883` with any standard client;
`on_message` taps let the application observe the traffic — here,
keeping the latest reading per device for the HTTP side to report:

```python
from blackbull.mqtt import MQTTExtension, Message

mqtt = app.add_extension(MQTTExtension(port=1883))
_readings: dict[str, dict[str, str]] = {}

@mqtt.on_message(topic='sensors/{device}/{metric}')
async def on_reading(msg: Message, device: str, metric: str):
    _readings.setdefault(device, {})[metric] = msg.payload.decode()

@app.route(path='/devices')
async def devices():
    return _readings
```

`{device}` and `{metric}` are captures — they match one topic level
like `+` and arrive as keyword arguments, the MQTT counterpart of
HTTP path params. Taps observe routing; delivery to subscribed MQTT
clients happens whether or not a tap matches.

## Batch jobs: a work queue with no queue server

Interactive requests take the HTTP door; batch work takes the MQTT
door. **Shared subscriptions** (MQTT 5 §4.8.2) turn a topic into a
load-balanced work queue: every worker that subscribes to the same
`$share/{group}/{filter}` joins a share group, and the broker gives
each matching message to exactly **one** member, round-robin.

```bash
mosquitto_sub -t '$share/workers/jobs/#' -p 1883 -V 5    # worker 1
mosquitto_sub -t '$share/workers/jobs/#' -p 1883 -V 5    # worker 2
mosquitto_pub -t 'jobs/generate' -m '{"prompt": "job-1"}' -p 1883 -V 5
mosquitto_pub -t 'jobs/generate' -m '{"prompt": "job-2"}' -p 1883 -V 5
```

`job-1` and `job-2` land on different workers. Scaling the pool is
starting another subscriber — no queue server, no client library
beyond MQTT, and the broker is the process you already run. The
[shared-subscriptions section of the MQTT guide](mqtt.md#shared-subscriptions)
covers the exact semantics (QoS, skipped members, retained
messages).

Two honest caveats for queue duty:

- **No offline buffering for an empty group.** If no member is
  connected when a job is published, the job is dropped, not queued
  (the broker's general no-offline-queue behaviour). Keep at least
  one worker connected whenever producers are publishing.
- **Queue state lives in this process.** A broker restart clears
  in-flight session state; there is no persistence layer.

## When this shape fits — and when it doesn't

This is deliberately a *narrow* pitch. It fits when:

- the deployment target is **one box** — an edge device, a
  single-board computer, a small VM — and you want the whole serving
  surface in one `python app.py`;
- clients genuinely benefit from **streamed output** and **HTTP/2
  multiplexing** (several consumers per connection);
- devices already speak **MQTT** and you'd rather not operate a
  separate broker for them;
- the target is **ARM / RISC-V / anything without a C toolchain**,
  where "no native dependencies" is the difference between `pip
  install` and an afternoon of cross-compilation.

It is *not* the right shape when:

- **inference throughput is the bottleneck** — BlackBull moves the
  bytes; it does not batch, schedule, or accelerate the model. A
  dedicated inference server in front of the GPU does more for
  throughput than any web framework choice;
- you need a **fleet-scale message bus** — the broker runs on one
  worker process, in memory ([limitations](mqtt.md#limitations));
  it is a device-gateway broker, not a clustered one;
- your stack needs **Trio/AnyIO**, or the other trade-offs in
  [Is BlackBull right for your project?](../getting-started/why-blackbull.md)
  cut against you.

## Run it

```bash
pip install blackbull
python examples/edge_inference.py
```

Then, in other terminals:

```bash
curl -N 'http://localhost:8000/generate?prompt=hello+edge'   # SSE tokens
open http://localhost:8000/                                  # browser demo
mosquitto_pub -t 'sensors/pi-cam-1/temperature' -m '41.2' -p 1883 -V 5
curl http://localhost:8000/devices
```

The example is dependency-free — the "model" is canned tokens — so
it runs anywhere, including the box you're about to deploy to.

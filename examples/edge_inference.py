"""Edge inference API server — one Python process, one small box.

The scenario: a Raspberry-Pi-class device runs a local model and has to
serve two kinds of traffic at once —

* **Interactive clients** (browser tabs, `curl`, httpx) call an HTTP
  endpoint and want the answer *streamed token by token* as it is
  generated, not buffered until the end.
* **Devices** report telemetry and drop batch jobs over **MQTT**, the
  protocol they already speak.

One BlackBull process covers both: HTTP/1.1 + HTTP/2 with an SSE
token-streaming endpoint on :8000, and a full MQTT 5 broker on :1883.
No reverse proxy, no separate broker, no C extensions — the whole stack
is Python you can step through.  The "model" here is fake (a canned
token generator) so the example runs with no ML dependency; swap
``fake_model`` for a real one and the serving surface is unchanged.

Run it::

    python examples/edge_inference.py

Streaming inference (SSE):

    curl -N 'http://localhost:8000/generate?prompt=hello+edge'
    open http://localhost:8000/          # browser EventSource demo

Device telemetry over MQTT (``apt install mosquitto-clients``)::

    mosquitto_pub -t 'sensors/pi-cam-1/temperature' -m '41.2' -p 1883 -V 5
    curl http://localhost:8000/devices   # latest reading per device

Batch jobs as a shared-subscription work queue (MQTT 5 §4.8.2) — every
worker that subscribes to the same ``$share/{group}/{filter}`` joins a
share group, and the broker gives each job to exactly **one** member,
round-robin::

    mosquitto_sub -t '$share/workers/jobs/#' -p 1883 -V 5    # terminal 1
    mosquitto_sub -t '$share/workers/jobs/#' -p 1883 -V 5    # terminal 2
    mosquitto_pub -t 'jobs/generate' -m '{"prompt": "job-1"}' -p 1883 -V 5
    mosquitto_pub -t 'jobs/generate' -m '{"prompt": "job-2"}' -p 1883 -V 5

The two publishes land on different terminals — a load-balanced work
queue with no queue server beyond the broker this process already runs.
``curl http://localhost:8000/status`` shows what the process has seen.

HTTP/2 works on the same port with no extra setup — cleartext h2c is
detected from the connection preface, so several ``/generate`` streams
multiplex over one connection::

    curl --http2-prior-knowledge -N 'http://localhost:8000/generate?prompt=h2'

(For browsers, which require HTTP/2 over TLS, pass ``certfile=`` /
``keyfile=`` to ``app.run`` and ALPN negotiates ``h2``.)
"""
from __future__ import annotations

import asyncio

from blackbull import BlackBull, EventSourceResponse, Response
from blackbull.mqtt import MQTTExtension, Message

app = BlackBull()
mqtt = app.add_extension(MQTTExtension(port=1883))


# --- The "model" -----------------------------------------------------------
# A stand-in for real inference: emits one token at a time with a small
# delay, exactly the shape a local LLM runtime hands back.

_CANNED = ("Tokens stream over SSE as they are produced , so a slow "
           "generation is visible immediately instead of after the last "
           "token .").split()


async def fake_model(prompt: str):
    yield f'{prompt} →'
    for token in _CANNED:
        await asyncio.sleep(0.15)          # simulated per-token compute
        yield token


# --- HTTP: the interactive inference surface -------------------------------

@app.route(path='/generate')
async def generate(prompt: str = 'Hello, edge'):
    """Stream one SSE event per generated token; ``done`` terminates."""
    async def events():
        async for token in fake_model(prompt):
            yield {'event': 'token', 'data': token}
        yield {'event': 'done', 'data': ''}
    return EventSourceResponse(events())


_HTML = b"""<!doctype html>
<title>BlackBull edge inference demo</title>
<body style="font-family: ui-monospace, monospace; padding: 2rem;">
<h1>BlackBull edge inference demo</h1>
<form id="f"><input id="p" value="Hello, edge" size="40">
<button>Generate</button></form>
<pre id="out" style="background: #f4f4f4; padding: 1rem; min-height: 4rem;"></pre>
<script>
const out = document.getElementById('out');
document.getElementById('f').addEventListener('submit', e => {
  e.preventDefault();
  out.textContent = '';
  const es = new EventSource(
    '/generate?prompt=' + encodeURIComponent(document.getElementById('p').value));
  es.addEventListener('token', e => { out.textContent += e.data + ' '; });
  es.addEventListener('done',  () => es.close());
});
</script>
"""


@app.route(path='/')
async def index():
    return Response(_HTML, content_type='text/html; charset=utf-8')


# --- MQTT: device ingest on the same process -------------------------------
# Taps observe the broker's routing; delivery to subscribed MQTT clients
# (including the $share work-queue groups) happens whether or not a tap
# matches.

_readings: dict[str, dict[str, str]] = {}     # device -> metric -> latest
_jobs_seen = 0


@mqtt.on_message(topic='sensors/{device}/{metric}')
async def on_reading(msg: Message, device: str, metric: str):
    _readings.setdefault(device, {})[metric] = msg.payload.decode('utf-8', 'replace')


@mqtt.on_message(topic='jobs/#')
async def on_job(msg: Message):
    global _jobs_seen
    _jobs_seen += 1                # broker hands the job to one $share member


@app.route(path='/devices')
async def devices():
    """Latest reading per device, fed by the MQTT tap above."""
    return _readings


@app.route(path='/status')
async def status():
    return {
        'devices': len(_readings),
        'jobs_dispatched': _jobs_seen,
        'work_queue': 'subscribe $share/workers/jobs/# to join the pool',
    }


if __name__ == '__main__':
    app.run(port=8000)   # HTTP/1.1 + h2c on 8000, MQTT broker on 1883

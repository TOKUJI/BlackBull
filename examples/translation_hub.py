"""Protocol translation hub — MQTT → WebSocket / SSE, REST → gRPC, one process.

The C4 showcase: BlackBull speaks four protocols from a single Python
process with no Envoy, no nginx, no sidecar config —

* **MQTT 5 broker** on :1883 — devices publish sensor readings
  (``sensors/{room}/temperature``) with any stock MQTT client.
* **WebSocket** at ``ws://localhost:8000/ws`` — browsers watch the same
  readings live, translated from MQTT PUBLISH to WS text frames.
* **Server-Sent Events** at ``http://localhost:8000/events`` — the same
  feed again for anything that prefers plain HTTP streaming.
* **gRPC** service ``hub.Telemetry`` on the *same* :8000 port (h2c) —
  and a REST route that *translates into it*: ``GET /rooms/{room}/stats``
  builds a `GrpcContext`, invokes the registered method in-process, and
  maps `GrpcStatus` to an HTTP status.  A native gRPC client calling
  ``/hub.Telemetry/RoomStats`` hits the very same handler.

The glue is ~30 lines: an MQTT *tap* feeds an in-process fan-out
(`Hub`), and the WS / SSE handlers drain it.  No protocol ever leaves
the process to reach another.

Run it::

    python examples/translation_hub.py

Then, in other shells::

    # 1. Watch translated frames (browser dashboard has both feeds):
    open  http://localhost:8000/                    # EventSource + WebSocket
    curl -N http://localhost:8000/events            # raw SSE

    # 2. Publish over MQTT (mosquitto-clients):
    mosquitto_pub -t 'sensors/kitchen/temperature' -m '21.5' -p 1883 -V 5

    # 3. REST -> gRPC translation:
    curl http://localhost:8000/rooms/kitchen/stats
    curl http://localhost:8000/rooms/attic/stats    # 404 <- GrpcStatus.NOT_FOUND

    # 4. The same method over native gRPC (same port, h2c):
    grpcurl -plaintext -d @ localhost:8000 hub.Telemetry/RoomStats <<< '"kitchen"'
"""
from __future__ import annotations

import asyncio
import json
import logging
from http import HTTPStatus

from blackbull import BlackBull, EventSourceResponse, JSONResponse, Response
from blackbull.grpc import GrpcContext, GrpcError, GrpcServiceRegistry, GrpcStatus
from blackbull.mqtt import Message, MQTTExtension
from blackbull.utils import Scheme

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('examples.hub')

app = BlackBull()
mqtt = app.add_extension(MQTTExtension(port=1883))
grpc = GrpcServiceRegistry()


# --- In-process fan-out ----------------------------------------------------
# The whole "message bus" of the hub.  Subscribers get their own bounded
# queue; a slow browser drops its own oldest frame instead of stalling the
# broker (same best-effort stance as the MQTT tap layer).

class Hub:
    def __init__(self, maxsize: int = 64):
        self._queues: set[asyncio.Queue] = set()
        self._maxsize = maxsize

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._queues.discard(q)

    def publish(self, item: dict) -> None:
        for q in self._queues:
            if q.full():
                q.get_nowait()          # drop that subscriber's oldest
            q.put_nowait(item)


hub = Hub()
_readings: dict[str, list[float]] = {}   # room -> readings seen so far


# --- MQTT ingestion: devices publish, the tap translates --------------------

@mqtt.on_message(topic='sensors/{room}/temperature')
async def on_temperature(msg: Message, room: str):
    """One MQTT PUBLISH in -> one JSON-shaped dict onto the hub."""
    try:
        value = float(msg.payload)
    except ValueError:
        log.info('ignoring non-numeric reading on %s: %r', msg.topic, msg.payload)
        return
    _readings.setdefault(room, []).append(value)
    hub.publish({'room': room, 'temperature': value})


# --- MQTT -> WebSocket -------------------------------------------------------

@app.route(path='/ws', scheme=Scheme.websocket)
async def ws_feed(scope, receive, send):
    event = await receive()
    if event['type'] != 'websocket.connect':
        return
    await send({'type': 'websocket.accept'})
    q = hub.subscribe()
    # Two waits at once: hub items flow out; a client close ends the handler.
    recv_task = asyncio.ensure_future(receive())
    item_task = asyncio.ensure_future(q.get())
    try:
        while True:
            done, _ = await asyncio.wait({recv_task, item_task},
                                         return_when=asyncio.FIRST_COMPLETED)
            if recv_task in done:
                event = recv_task.result()
                if event['type'] == 'websocket.disconnect':
                    return
                recv_task = asyncio.ensure_future(receive())   # ignore client text
            if item_task in done:
                await send({'type': 'websocket.send',
                            'text': json.dumps(item_task.result())})
                item_task = asyncio.ensure_future(q.get())
    finally:
        hub.unsubscribe(q)
        for task in (recv_task, item_task):
            task.cancel()


# --- MQTT -> SSE -------------------------------------------------------------

@app.route(path='/events')
async def sse_feed():
    async def readings():
        q = hub.subscribe()
        try:
            while True:
                yield {'event': 'reading', 'data': json.dumps(await q.get())}
        finally:
            hub.unsubscribe(q)
    return EventSourceResponse(readings())


# --- The gRPC service (native gRPC clients call this on the same port) -------

@grpc.method('/hub.Telemetry/RoomStats')
async def room_stats(request: bytes, context) -> bytes:
    """Raw-bytes contract like ``examples/grpc_server.py``: the request is a
    JSON-encoded room name, the response JSON stats."""
    room = json.loads(request) if request else ''
    values = _readings.get(room)
    if not values:
        context.abort(GrpcStatus.NOT_FOUND, f'no readings for room {room!r}')
    return json.dumps({
        'room': room, 'count': len(values),
        'min': min(values), 'max': max(values), 'last': values[-1],
    }).encode()


app.enable_grpc(grpc)


# --- REST -> gRPC translation -------------------------------------------------
# The translation layer in full: build a context, invoke the registered
# method, map GrpcStatus to an HTTP status.  ~15 lines, no gateway process.

_GRPC_TO_HTTP = {
    GrpcStatus.NOT_FOUND: 404,
    GrpcStatus.INVALID_ARGUMENT: 400,
    GrpcStatus.PERMISSION_DENIED: 403,
    GrpcStatus.UNAUTHENTICATED: 401,
}


@app.route(path='/rooms/{room}/stats')
async def rest_room_stats(room: str, scope):   # `scope` is the native Connection
    handler = grpc.lookup('/hub.Telemetry/RoomStats')
    context = GrpcContext(scope)
    try:
        payload = await handler(json.dumps(room).encode(), context)
    except GrpcError as exc:
        return JSONResponse({'error': exc.details or exc.status.name},
                            status=HTTPStatus(_GRPC_TO_HTTP.get(exc.status, 500)))
    return Response(payload, content_type='application/json')


# --- Browser dashboard --------------------------------------------------------

_HTML = b"""<!doctype html>
<title>BlackBull translation hub</title>
<body style="font-family: ui-monospace, monospace; padding: 2rem;">
<h1>One process, four protocols</h1>
<p>Publish: <code>mosquitto_pub -t 'sensors/kitchen/temperature' -m '21.5' -p 1883 -V 5</code></p>
<div style="display: flex; gap: 2rem;">
  <div><h2>MQTT &rarr; WebSocket</h2><pre id="ws" style="background:#f4f4f4;padding:1rem;min-height:8rem;"></pre></div>
  <div><h2>MQTT &rarr; SSE</h2><pre id="sse" style="background:#f4f4f4;padding:1rem;min-height:8rem;"></pre></div>
</div>
<p>REST &rarr; gRPC: <a href="/rooms/kitchen/stats">/rooms/kitchen/stats</a></p>
<script>
const wsOut = document.getElementById('ws'), sseOut = document.getElementById('sse');
const ws = new WebSocket(`ws://${location.host}/ws`);
ws.onmessage = e => { wsOut.textContent += e.data + '\\n'; };
const es = new EventSource('/events');
es.addEventListener('reading', e => { sseOut.textContent += e.data + '\\n'; });
</script>
"""


@app.route(path='/')
async def index():
    return Response(_HTML, content_type='text/html; charset=utf-8')


if __name__ == '__main__':
    app.run(port=8000)

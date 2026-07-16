# Protocol translation hub

BlackBull speaks HTTP/1.1, HTTP/2, WebSocket, SSE, gRPC, and MQTT from a
single Python process — which means *translating* between them needs no
Envoy, no nginx, no sidecar, and no config file.  A device publishes over
MQTT; a browser watches over WebSocket or SSE; a REST call becomes a gRPC
invocation.  The glue is ordinary application code.

The complete working example is
[`examples/translation_hub.py`](https://github.com/tokuji/BlackBull/blob/master/examples/translation_hub.py)
— run it and drive all four protocols at once:

```bash
python examples/translation_hub.py
mosquitto_pub -t 'sensors/kitchen/temperature' -m '21.5' -p 1883 -V 5
curl -N http://localhost:8000/events            # the reading, over SSE
curl http://localhost:8000/rooms/kitchen/stats  # REST -> gRPC translation
```

This page walks through the three translations it contains.

## The fan-out primitive

Every translation direction meets in a tiny in-process hub — a set of
bounded per-subscriber queues.  A slow browser drops its own oldest frame
rather than back-pressuring the MQTT broker, the same best-effort stance
the tap layer itself takes:

```python
class Hub:
    def subscribe(self) -> asyncio.Queue: ...
    def unsubscribe(self, q) -> None: ...
    def publish(self, item: dict) -> None:
        for q in self._queues:
            if q.full():
                q.get_nowait()          # drop that subscriber's oldest
            q.put_nowait(item)
```

That is the entire "message bus".  No broker process, no serialization
hop — the dict flows from the MQTT tap to the WS/SSE handlers as a
Python object.

## MQTT → WebSocket / SSE

An [`@mqtt.on_message` tap](mqtt.md) translates each device publish into
a hub item; `{room}` captures work like HTTP path params:

```python
@mqtt.on_message(topic='sensors/{room}/temperature')
async def on_temperature(msg: Message, room: str):
    hub.publish({'room': room, 'temperature': float(msg.payload)})
```

The WebSocket handler subscribes to the hub and forwards items as text
frames, while also watching `receive()` so a client close ends the
handler.  The SSE side is even shorter — an async generator wrapped in
`EventSourceResponse`:

```python
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
```

One MQTT PUBLISH in, one WS frame *and* one SSE event out, per connected
client — three protocols, one process.

## REST → gRPC

The gRPC service registered with `GrpcServiceRegistry` is reachable two
ways at once: natively (any gRPC client calling `hub.Telemetry/RoomStats`
on the same port, h2c or TLS), and through a REST route that performs the
translation in-process — build a `GrpcContext`, invoke the registered
method, and map `GrpcStatus` onto an HTTP status:

```python
@app.route(path='/rooms/{room}/stats')
async def rest_room_stats(room: str, scope: dict):
    handler = grpc.lookup('/hub.Telemetry/RoomStats')
    context = GrpcContext(scope)
    try:
        payload = await handler(json.dumps(room).encode(), context)
    except GrpcError as exc:
        return JSONResponse({'error': exc.details or exc.status.name},
                            status=HTTPStatus(_GRPC_TO_HTTP.get(exc.status, 500)))
    return Response(payload, content_type='application/json')
```

`context.abort(GrpcStatus.NOT_FOUND, ...)` in the service surfaces as a
`NOT_FOUND` status trailer to gRPC clients and as a `404` JSON error to
REST clients — one handler, both protocols, consistent semantics.

## Why this is hard elsewhere

The conventional shape of this system is four processes and a gateway:
an MQTT broker (Mosquitto), a translation worker, a gRPC service behind
Envoy's gRPC-JSON transcoder, and the web server.  Each hop adds a
serialization boundary, a network hop, a config file, and a failure mode.
Here the entire hub is one `python` invocation whose "config" is the
example file itself — and every protocol conversation is observable with
the same [event API](events.md) and [fault-injection](fault_injection.md)
tooling the rest of BlackBull uses.

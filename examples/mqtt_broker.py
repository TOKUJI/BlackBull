"""MQTT 5 broker riding BlackBull's Non-ASGI bridge — side by side with HTTP.

One BlackBull process serves HTTP on :8000 *and* a full MQTT 5 broker on the
standard MQTT port :1883.  The broker handles CONNECT/CONNACK, SUBSCRIBE,
PUBLISH at QoS 0/1/2, retained messages, and Last-Will delivery; messages are
routed between connected clients automatically.

Two decorators, two protocols
-----------------------------
    @app.route(path='/status')             # HTTP: the handler *is* the response
    async def status():
        return {...}                       #   -> its return value is sent back

    @mqtt.on_message(topic='sensors/#')    # MQTT: the handler *observes* routing
    async def on_msg(msg: Message):        #   -> gets one Message; return value
        ...                                #      ignored (broker already delivered)

Both match on an address (``path=`` / ``topic=``) and decorate an async
function, so they feel alike.  They differ where the protocols differ: an HTTP
route is the application, while an MQTT tap rides on top of the broker, which
delivers to subscribers whether or not any handler is registered.

The broker is wired in as an :class:`~blackbull.extension.Extension` — the one
generic registration seam on the core (``app.add_extension``).

Run it::

    python examples/mqtt_broker.py

Then drive it with the standard Mosquitto clients (``apt install mosquitto-clients``)::

    mosquitto_sub -t 'sensors/#' -p 1883 -V 5        # terminal 1
    mosquitto_pub -t 'sensors/room1/temperature' -m '21.5' -p 1883 -V 5  # terminal 2

The publish appears in the subscriber's terminal *and* in this server's log via
the handler below, and the running count shows up at http://localhost:8000/status.
"""
import logging

from blackbull import BlackBull
from blackbull.mqtt import MQTTExtension, AsyncAPIExtension, Message

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('examples.mqtt')

app = BlackBull()
mqtt = app.add_extension(MQTTExtension(port=1883))
# AsyncAPI 3.0 docs for the taps below — served at /asyncapi.json and /asyncapi,
# the messaging-world counterpart of /openapi.json + /docs for HTTP.
app.add_extension(AsyncAPIExtension(title='Sensor Gateway', version='1.0.0'))

_seen: dict[str, int] = {}


# --- HTTP routing: the handler is the application -------------------------------

@app.route(path='/')
async def index():
    return "BlackBull is serving HTTP here, and an MQTT 5 broker on :1883."


@app.route(path='/status')
async def status():
    """Report how many messages each topic has carried (dict -> JSONResponse)."""
    return {'messages_by_topic': _seen, 'total': sum(_seen.values())}


# --- MQTT taps: the handler observes the broker's routing -----------------------
# Each tap receives one Message (msg.topic / msg.payload / msg.qos / msg.retain),
# mirroring how @app.on hands an observer a single Event.

@mqtt.on_message(topic='sensors/{room}/temperature')
async def on_temperature(msg: Message, room: str):
    """Called for every temperature reading; ``{room}`` is captured like an HTTP
    path param and injected as a keyword argument."""
    log.info('temperature in %s: %s', room,
             msg.payload.decode('utf-8', 'replace'))


@mqtt.on_message(topic='#')
async def on_any_message(msg: Message):
    """Firehose tap: count every message the broker routes."""
    _seen[msg.topic] = _seen.get(msg.topic, 0) + 1
    log.info('mqtt publish %s (%d bytes)', msg.topic, len(msg.payload))


if __name__ == '__main__':
    app.run(port=8000)

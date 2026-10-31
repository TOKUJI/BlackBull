"""Caps on how many of something may exist at once."""
import asyncio
import concurrent.futures
import contextlib
import threading

import pytest

from blackbull.client import HTTP2Client
from blackbull.middleware import Compression
from blackbull.mqtt.messages import (MQTTConnack, MQTTPuback, MQTTPublish,
                                     MQTTSuback, MQTTSubscribe)
from blackbull.protocol.frame_types import ErrorCodes, FrameTypes
from tests.conformance.http2._harness import _make_h2_frame
from tests.harness import peer, read_h2_frame, served
from tests.unit import test_unix_accept_queue_warning as backlog

from ._drive import (client_h2, client_http1, connect, h2, h2_data, h2_request,
                     h2_response, head, make_app, mqtt, mqtt_acknowledged,
                     mqtt_subscriber_fate)
from ._table import Row, check, check_under, table

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(30)]


async def _past_max_connections() -> str:
    async with served(make_app(), max_connections=1) as s:
        held = await s.wire()
        await held.send(head())
        await held.response()
        return (await (await s.wire()).recv_all()).split(b' ', 2)[1].decode()


async def _past_compression_inflight() -> str:
    """The first offload is held in the executor while the second response is
    produced, so the second meets the admission cap."""
    loop = asyncio.get_running_loop()
    release = threading.Event()

    class Held(concurrent.futures.ThreadPoolExecutor):
        def submit(self, fn, /, *args, **kwargs):
            return super().submit(lambda: (release.wait(5), fn(*args, **kwargs))[1])

    app = make_app()
    app.use(Compression(min_size=1, executor_threshold=1, executor_max_inflight=1))
    held = Held(2)
    loop.set_default_executor(held)
    try:
        async with served(app) as s:
            first = await s.wire()
            await first.send(head(b'Accept-Encoding: gzip'))
            await asyncio.sleep(0.1)                      # its offload is now held
            second = await s.wire()
            await second.send(head(b'Accept-Encoding: gzip'))
            answer = (await second.response()).header(b'content-encoding')
            release.set()
            await first.response()
    finally:
        release.set()
        held.shutdown(wait=False)
        loop.set_default_executor(concurrent.futures.ThreadPoolExecutor())
    return 'uncompressed' if answer is None else answer.decode()


async def _compressed_once() -> str:
    app = make_app()
    app.use(Compression(min_size=1, executor_threshold=1, executor_max_inflight=1))
    async with served(app) as s:
        wire = await s.wire()
        await wire.send(head(b'Accept-Encoding: gzip'))
        return (await wire.response()).header(b'content-encoding').decode()


async def _past_queued_messages() -> str:
    """A subscriber that grants one in-flight message and acknowledges none:
    the broker queues the rest up to the cap, and refuses the newest."""
    from blackbull import BlackBull
    from blackbull.mqtt import MQTTExtension
    from blackbull.mqtt.messages import decode_packet, encode_packet
    from ._drive import _mqtt_packet

    app = BlackBull()
    app.add_extension(MQTTExtension(port=0))
    async with served(app) as s:
        port = s.server.protocol_ports['mqtt']
        sub_r, sub_w = await asyncio.open_connection('127.0.0.1', port)
        sub_w.write(encode_packet(connect('sub', receive_maximum=1)))
        sub_w.write(encode_packet(MQTTSubscribe(packet_id=1, subscriptions=[('t', 1)])))
        await sub_w.drain()
        await _mqtt_packet(sub_r)                        # CONNACK
        await _mqtt_packet(sub_r)                        # SUBACK
        pub_r, pub_w = await asyncio.open_connection('127.0.0.1', port)
        pub_w.write(encode_packet(connect('pub')))
        for i in range(5):
            pub_w.write(encode_packet(MQTTPublish(topic='t', payload=b'%d' % i,
                                                  qos=1, packet_id=i + 1)))
        await pub_w.drain()
        await _mqtt_packet(pub_r)                        # CONNACK
        for _ in range(5):
            await _mqtt_packet(pub_r)                    # PUBACKs: all accepted
        delivered = 0
        with contextlib.suppress(TimeoutError):
            while True:
                message = decode_packet(await asyncio.wait_for(_mqtt_packet(sub_r), 0.5))[0]
                if isinstance(message, MQTTPublish):
                    delivered += 1
                    sub_w.write(encode_packet(MQTTPuback(packet_id=message.packet_id)))
                    await sub_w.drain()
        sub_w.close()
        pub_w.close()
        return f'{delivered} delivered'


async def _past_raw_queue_depth() -> str:
    sent: list[str] = []
    flooded = asyncio.Event()

    async def origin(reader, writer):
        await reader.readexactly(24)
        writer.write(_make_h2_frame(FrameTypes.SETTINGS))
        writer.write(b''.join(h2_data(1, b'x') for _ in range(8)))
        await writer.drain()
        with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError, TimeoutError):
            while True:
                kind, _, sid, payload = await read_h2_frame(reader)
                if kind == FrameTypes.RST_STREAM.value[0]:
                    sent.append(f'RST_STREAM {ErrorCodes(int.from_bytes(payload, "big")).name}')
                    flooded.set()

    async with peer(origin) as port:
        async with HTTP2Client('127.0.0.1', port) as client:
            client.register_raw_stream(1)
            await asyncio.wait_for(flooded.wait(), 5)
    return sent[0]


async def _past_socket_backlog() -> str:
    import pathlib
    import tempfile
    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as env:
        server = backlog.server_under_startup(pathlib.Path(tmp), 'bound', 0, env)
        refused = await backlog.open_during_startup(server, backlog.BACKLOG + 3)
    return f'{refused} refused'


ROWS = [
    Row('max_connections', 'tcp', '503', setting='max_connections',
        env={'BB_MAX_CONNECTIONS': '1'}, provoke=_past_max_connections),
    Row('stream_queue_depth', 'http2', 'RST_STREAM ENHANCE_YOUR_CALM',
        setting='stream_queue_depth', documented='ENHANCE_YOUR_CALM',
        provoke=lambda: h2(h2_request(1, path=b'/hold'), *(h2_data(1, b'x') for _ in range(17)),
                           stream_queue_depth=1),
        under=lambda: h2(h2_request(1), h2_data(1, b'x', end_stream=True), stream_queue_depth=1),
        under_answer='complete'),
    Row('h2_max_concurrent_streams', 'http2', 'RST_STREAM REFUSED_STREAM',
        setting='h2_max_concurrent_streams', documented='REFUSED_STREAM',
        env={'BB_H2_MAX_CONCURRENT_STREAMS': '1'},
        provoke=lambda: h2(h2_request(1, path=b'/hold'), h2_request(3, path=b'/hold'))),
    Row('h2_ws_max_streams_per_connection', 'h2-ws', 'RST_STREAM REFUSED_STREAM',
        setting='h2_ws_max_streams_per_connection', documented='REFUSED_STREAM',
        env={'BB_H2_ENABLE_WEBSOCKET': '1', 'BB_H2_WS_MAX_STREAMS_PER_CONNECTION': '1'},
        provoke=lambda: h2(*(h2_request(sid, (b':protocol', b'websocket'), method=b'CONNECT',
                                        path=b'/ws') for sid in (1, 3)))),
    Row('compression_max_inflight', 'compression', 'uncompressed',
        setting='compression_max_inflight', provoke=_past_compression_inflight,
        under=lambda: _compressed_once(), under_answer='gzip'),
    Row('mqtt_max_subscriptions', 'mqtt', 'SUBACK 0x97', setting='mqtt_max_subscriptions',
        documented='SUBACK 0x97', env={'BB_MQTT_MAX_SUBSCRIPTIONS': '1'},
        provoke=lambda: mqtt([connect(), MQTTSubscribe(packet_id=1, subscriptions=[
            ('a', 0), ('b', 0)])], answer=MQTTSuback)),
    Row('mqtt_max_retained', 'mqtt', 'PUBACK 0x97', setting='mqtt_max_retained',
        documented='0x97', env={'BB_MQTT_MAX_RETAINED': '1'},
        provoke=lambda: mqtt([connect(), *(MQTTPublish(topic=t, payload=b'v', qos=1, packet_id=i,
                                                      retain=True)
                                          for i, t in ((1, 'a'), (2, 'b')))],
                             answer=MQTTPuback)),
    Row('mqtt_max_sessions', 'mqtt', 'CONNACK 0x97', setting='mqtt_max_sessions',
        documented='CONNACK 0x97', env={'BB_MQTT_MAX_SESSIONS': '1'},
        provoke=lambda: mqtt([connect('kept', session_expiry_interval=60)],
                             [connect('new', session_expiry_interval=60)], answer=MQTTConnack)),
    Row('mqtt_max_queued_messages', 'mqtt', '3 delivered', setting='mqtt_max_queued_messages',
        documented='refuse newest', env={'BB_MQTT_MAX_QUEUED_MESSAGES': '2'},
        provoke=_past_queued_messages),
    Row('mqtt_broker_inbox_maxsize', 'mqtt', '20 acknowledged',
        setting='mqtt_broker_inbox_maxsize', documented='Readers await admission',
        env={'BB_MQTT_BROKER_INBOX_MAXSIZE': '1'},
        provoke=lambda: mqtt_acknowledged(*(MQTTPublish(topic='t', payload=b'v', qos=1,
                                                        packet_id=i + 1) for i in range(20)))),
    Row('mqtt_connection_inbox_maxsize', 'mqtt', 'closed',
        setting='mqtt_connection_inbox_maxsize', documented='ends only that connection',
        env={'BB_MQTT_CONNECTION_INBOX_MAXSIZE': '2'},
        provoke=lambda: mqtt_subscriber_fate(*(MQTTPublish(topic='t', payload=b'x' * 60000)
                                               for _ in range(400)))),
    Row('client_max_interim_responses', 'http1', 'ResponseTooLarge',
        setting='client_max_interim_responses', env={'BB_CLIENT_MAX_INTERIM_RESPONSES': '1'},
        provoke=lambda: client_http1(b'HTTP/1.1 100 Continue\r\n\r\n' * 2
                                     + b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n')),
    Row('client_max_interim_responses', 'http2', 'ResponseTooLarge',
        setting='client_max_interim_responses', env={'BB_CLIENT_MAX_INTERIM_RESPONSES': '1'},
        provoke=lambda: client_h2(h2_response(1, status=b'103'), h2_response(1, status=b'103'),
                                  h2_response(1, end_stream=True))),
    Row('client_raw_queue_depth', 'http2', 'RST_STREAM ENHANCE_YOUR_CALM',
        setting='client_raw_queue_depth', documented='ENHANCE_YOUR_CALM',
        env={'BB_CLIENT_RAW_QUEUE_DEPTH': '4'}, provoke=_past_raw_queue_depth),
    Row('socket_backlog', 'transport', '2 refused', provoke=_past_socket_backlog),
]


@table(*ROWS)
async def test_exceeding_a_count_cap_is_answered_as_documented(row, monkeypatch, caplog):
    await check(row, monkeypatch, caplog)



@table(*ROWS, under=True)
async def test_count_within_a_cap_is_not_recorded(row, monkeypatch, caplog):
    await check_under(row, monkeypatch, caplog)

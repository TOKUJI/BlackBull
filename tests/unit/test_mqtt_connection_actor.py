"""End-to-end wiring tests for the two-actor MQTT topology.

These drive ``serve_connection`` against a live ``BrokerActor`` task using fake
reader/writer pairs — proving the per-connection actor, its reader loop, and the
broker exchange messages correctly over the inbox seam.  The full MQTT-5 wire
contract is covered by the conformance oracle (Phase 4); these are focused
topology smoke tests.
"""
import asyncio
import contextlib

import pytest

from blackbull.mqtt.broker import BrokerActor
from blackbull.mqtt.connection import serve_connection
from blackbull.mqtt.messages import (
    ReasonCode,
    MQTTConnect, MQTTConnack, MQTTPublish, MQTTPuback, MQTTPubcomp, MQTTPubrec,
    MQTTPubrel,
    MQTTSubscribe, MQTTSuback, MQTTPingreq, MQTTPingresp, MQTTDisconnect,
    encode_packet, decode_packet,
)
from blackbull.server.protocol_registry import ProtocolContext
from blackbull.server.recipient import AbstractReader
from blackbull.server.sender import AbstractWriter

pytestmark = pytest.mark.asyncio


class _FakeReader(AbstractReader):
    def __init__(self):
        self._buf = bytearray()
        self._closed = False

    async def read(self, n: int) -> bytes:
        if not self._buf:
            await asyncio.sleep(0.005)
            return b''
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    def feed_packet(self, packet) -> None:
        self._buf.extend(encode_packet(packet))


class _FakeWriter(AbstractWriter):
    def __init__(self):
        self.written = bytearray()

    async def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def drain(self) -> None:
        pass

    def pop_packets(self) -> list:
        out, offset, buf = [], 0, bytes(self.written)
        while offset < len(buf):
            packet, consumed = decode_packet(buf[offset:])
            out.append(packet)
            offset += consumed
        self.written = self.written[offset:]
        return out


def _ctx(conn_id='c'):
    return ProtocolContext(peername=('127.0.0.1', 5), sockname=('0.0.0.0', 1883),
                           ssl=False, aggregator=None, connection_id=conn_id,
                           protocol='mqtt')


@contextlib.asynccontextmanager
async def _running_broker():
    broker = BrokerActor()
    task = asyncio.create_task(broker.run())
    try:
        yield broker
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _serve(broker, reader, ctx, app_handlers=None):
    """Spawn serve_connection as a background task; caller drives the reader."""
    writer = _FakeWriter()
    task = asyncio.create_task(
        serve_connection(reader, writer, ctx, broker, app_handlers=app_handlers))
    return writer, task


async def _drain(*tasks):
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# --------------------------------------------------------------------------

async def test_connect_round_trips_connack():
    async with _running_broker() as broker:
        reader = _FakeReader()
        writer, task = await _serve(broker, reader, _ctx())
        reader.feed_packet(MQTTConnect(client_id='c1', clean_start=True, keep_alive=60))
        await asyncio.sleep(0.1)
        pkts = writer.pop_packets()
        await _drain(task)
    assert any(isinstance(p, MQTTConnack) and p.reason_code == ReasonCode.SUCCESS for p in pkts)


async def test_pingreq_answered_locally():
    async with _running_broker() as broker:
        reader = _FakeReader()
        writer, task = await _serve(broker, reader, _ctx())
        reader.feed_packet(MQTTConnect(client_id='c1', clean_start=True, keep_alive=60))
        reader.feed_packet(MQTTPingreq())
        await asyncio.sleep(0.1)
        pkts = writer.pop_packets()
        await _drain(task)
    assert any(isinstance(p, MQTTPingresp) for p in pkts)


async def test_a_rejected_pubrec_on_the_wire_frees_the_window():
    """§4.9 — a PUBREC carrying an error code stops charging the window,
    so the next message goes out without waiting for a PUBCOMP."""
    async with _running_broker() as broker:
        sub_reader, pub_reader = _FakeReader(), _FakeReader()
        sub_writer, sub_task = await _serve(broker, sub_reader, _ctx('s'))
        _pub_writer, pub_task = await _serve(broker, pub_reader, _ctx('p'))
        sub_reader.feed_packet(MQTTConnect(
            client_id='c1', clean_start=True, keep_alive=60,
            properties={'receive_maximum': 1}))
        sub_reader.feed_packet(MQTTSubscribe(
            packet_id=1, subscriptions=[('t', 2)]))
        pub_reader.feed_packet(MQTTConnect(
            client_id='pub', clean_start=True, keep_alive=60))
        pub_reader.feed_packet(MQTTPublish(
            topic='t', payload=b'a', qos=2, packet_id=1))
        pub_reader.feed_packet(MQTTPublish(
            topic='t', payload=b'b', qos=2, packet_id=2))
        await asyncio.sleep(0.1)
        first = [p for p in sub_writer.pop_packets()
                 if isinstance(p, MQTTPublish)]
        assert [p.payload for p in first] == [b'a']

        sub_reader.feed_packet(MQTTPubrec(
            packet_id=first[0].packet_id,
            reason_code=ReasonCode.QUOTA_EXCEEDED))
        await asyncio.sleep(0.1)
        pkts = sub_writer.pop_packets()
        await _drain(sub_task, pub_task)

    assert not any(isinstance(p, MQTTPubrel) for p in pkts), (
        'the exchange ends at the rejected PUBREC (§2.2.1); no PUBREL follows')
    assert [p.payload for p in pkts if isinstance(p, MQTTPublish)] == [b'b'], (
        'a rejected PUBLISH kept charging the window')
    assert not any(isinstance(p, MQTTPubcomp) for p in pkts), (
        'the broker invented a PUBCOMP for an exchange it only acknowledged')


async def test_disconnect_detaches_client():
    async with _running_broker() as broker:
        reader = _FakeReader()
        writer, task = await _serve(broker, reader, _ctx())
        reader.feed_packet(MQTTConnect(client_id='c1', clean_start=True, keep_alive=60))
        reader.feed_packet(MQTTDisconnect(reason_code=ReasonCode.SUCCESS))
        await asyncio.sleep(0.1)
        await _drain(task)
        # Graceful disconnect with default expiry → session pruned, no live client.
        assert broker._clients == {}
        assert 'c1' not in broker._sessions


async def test_two_connections_publish_subscribe_routing():
    async with _running_broker() as broker:
        sub_reader = _FakeReader()
        sub_writer, sub_task = await _serve(broker, sub_reader, _ctx('sub'))
        sub_reader.feed_packet(MQTTConnect(client_id='sub', clean_start=True, keep_alive=60))
        sub_reader.feed_packet(MQTTSubscribe(packet_id=1, subscriptions=[('chat/+', 1)]))
        await asyncio.sleep(0.05)

        pub_reader = _FakeReader()
        pub_writer, pub_task = await _serve(broker, pub_reader, _ctx('pub'))
        pub_reader.feed_packet(MQTTConnect(client_id='pub', clean_start=True, keep_alive=60))
        pub_reader.feed_packet(MQTTPublish(topic='chat/general', payload=b'hi',
                                           qos=1, packet_id=100))
        await asyncio.sleep(0.1)

        sub_pkts = sub_writer.pop_packets()
        pub_pkts = pub_writer.pop_packets()
        await _drain(sub_task, pub_task)

    delivered = [p for p in sub_pkts if isinstance(p, MQTTPublish)]
    assert delivered and delivered[0].topic == 'chat/general'
    assert delivered[0].payload == b'hi'
    assert any(isinstance(p, MQTTPuback) and p.packet_id == 100 for p in pub_pkts)


async def test_on_message_tap_receives_message_object():
    from blackbull.mqtt import Message
    seen = []

    async def tap(msg):
        seen.append(msg)

    async with _running_broker() as broker:
        reader = _FakeReader()
        writer, task = await _serve(broker, reader, _ctx(),
                                    app_handlers=[('sensors/#', tap)])
        reader.feed_packet(MQTTConnect(client_id='c1', clean_start=True, keep_alive=60))
        reader.feed_packet(MQTTPublish(topic='sensors/a/temp', payload=b'21', qos=0))
        await asyncio.sleep(0.1)
        await _drain(task)

    assert len(seen) == 1
    assert isinstance(seen[0], Message)
    assert seen[0].topic == 'sensors/a/temp'
    assert seen[0].payload == b'21'
    assert seen[0].qos == 0


async def test_on_message_tap_skips_non_matching_topic():
    seen = []

    async def tap(msg):
        seen.append(msg)

    async with _running_broker() as broker:
        reader = _FakeReader()
        writer, task = await _serve(broker, reader, _ctx(),
                                    app_handlers=[('sensors/#', tap)])
        reader.feed_packet(MQTTConnect(client_id='c1', clean_start=True, keep_alive=60))
        reader.feed_packet(MQTTPublish(topic='other/topic', payload=b'x', qos=0))
        await asyncio.sleep(0.1)
        await _drain(task)

    assert seen == []


async def test_on_message_capture_param_injected_inline():
    """A ``{name}`` segment in the filter is injected as a keyword argument."""
    seen = {}

    async def tap(msg, room):
        seen['topic'] = msg.topic
        seen['room'] = room

    async with _running_broker() as broker:
        reader = _FakeReader()
        writer, task = await _serve(broker, reader, _ctx(),
                                    app_handlers=[('sensors/{room}/temp', tap)])
        reader.feed_packet(MQTTConnect(client_id='c1', clean_start=True, keep_alive=60))
        reader.feed_packet(MQTTPublish(topic='sensors/kitchen/temp', payload=b'21', qos=0))
        await asyncio.sleep(0.1)
        await _drain(task)

    assert seen == {'topic': 'sensors/kitchen/temp', 'room': 'kitchen'}


async def test_on_message_dispatched_through_tap_actor():
    """In actor mode the connection offers to a decoupled TapActor."""
    from blackbull.mqtt import Message, TapActor
    seen = []

    async def tap(msg):
        seen.append(msg)

    tap_actor = TapActor([('sensors/#', tap)])
    tap_task = asyncio.create_task(tap_actor.run())
    try:
        async with _running_broker() as broker:
            reader = _FakeReader()
            writer = _FakeWriter()
            task = asyncio.create_task(
                serve_connection(reader, writer, _ctx(), broker, tap=tap_actor))
            reader.feed_packet(MQTTConnect(client_id='c1', clean_start=True, keep_alive=60))
            reader.feed_packet(MQTTPublish(topic='sensors/a/temp', payload=b'9', qos=0))
            await asyncio.sleep(0.1)
            await _drain(task)
    finally:
        tap_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(tap_task)

    assert len(seen) == 1
    assert isinstance(seen[0], Message)
    assert seen[0].topic == 'sensors/a/temp'
    assert tap_actor.dropped == 0


async def test_on_message_tap_exception_is_isolated():
    async with _running_broker() as broker:
        reader = _FakeReader()

        async def boom(msg):
            raise RuntimeError('tap failure')

        writer, task = await _serve(broker, reader, _ctx(),
                                    app_handlers=[('#', boom)])
        reader.feed_packet(MQTTConnect(client_id='c1', clean_start=True, keep_alive=60))
        reader.feed_packet(MQTTPublish(topic='a', payload=b'x', qos=1, packet_id=5))
        await asyncio.sleep(0.1)
        pkts = writer.pop_packets()
        await _drain(task)
    # Broker still acked despite the tap raising (isolation).
    assert any(isinstance(p, MQTTPuback) and p.packet_id == 5 for p in pkts)


async def test_unsupported_version_rejected():
    async with _running_broker() as broker:
        reader = _FakeReader()
        writer, task = await _serve(broker, reader, _ctx())
        reader.feed_packet(MQTTConnect(client_id='c1', clean_start=True,
                                       keep_alive=60, proto_level=4))
        await asyncio.sleep(0.1)
        pkts = writer.pop_packets()
        await _drain(task)
    assert any(isinstance(p, MQTTConnack) and p.reason_code == ReasonCode.UNSUPPORTED_PROTOCOL_VERSION for p in pkts)


@pytest.mark.parametrize('connected', [False, True])
@pytest.mark.parametrize('wire,reason', [
    (b'\x00\x00', ReasonCode.MALFORMED_PACKET),
    (b'\xc1\x00', ReasonCode.MALFORMED_PACKET),
    (b'\x30\x80\x80\x80\x80', ReasonCode.MALFORMED_PACKET),
    (b'\x10\x06\x00\x04MQTT', ReasonCode.MALFORMED_PACKET),
    (b'\x30\xff\xff\xff\x7f', ReasonCode.PACKET_TOO_LARGE),
])
async def test_invalid_wire_ends_connection_without_forwarding_trailing_packet(connected, wire, reason):
    async with _running_broker() as broker:
        reader = _FakeReader()
        writer, task = await _serve(broker, reader, _ctx())
        if connected:
            reader.feed_packet(MQTTConnect(client_id='bad-wire', clean_start=True, keep_alive=60))
        reader._buf.extend(wire)
        reader.feed_packet(MQTTPingreq())
        try:
            await asyncio.wait_for(asyncio.shield(task), 1)
            packets = writer.pop_packets()
            if connected:
                assert [type(p) for p in packets] == [MQTTConnack, MQTTDisconnect]
                assert packets[-1].reason_code == reason
            else:
                assert packets == []
            assert not broker._clients
        finally:
            await _drain(task)


async def test_malformed_connection_fires_will_once_and_leaves_no_tasks():
    async with _running_broker() as broker:
        subscriber = _FakeReader()
        sub_writer, sub_task = await _serve(broker, subscriber, _ctx('sub'))
        subscriber.feed_packet(MQTTConnect(client_id='sub', clean_start=True, keep_alive=60))
        subscriber.feed_packet(MQTTSubscribe(packet_id=1, subscriptions=[('will', 0)]))
        async with asyncio.timeout(1):
            while not any(isinstance(p, MQTTSuback) for p in sub_writer.pop_packets()):
                await asyncio.sleep(0)
        tasks_before = asyncio.all_tasks()
        reader = _FakeReader()
        writer, task = await _serve(broker, reader, _ctx('bad'))
        reader.feed_packet(MQTTConnect(client_id='bad', clean_start=True, keep_alive=60,
                                       will_topic='will', will_payload=b'gone'))
        reader._buf.extend(b'\xc1\x00')
        try:
            await asyncio.wait_for(asyncio.shield(task), 1)
            await asyncio.sleep(0)
            delivered = [p for p in sub_writer.pop_packets() if isinstance(p, MQTTPublish)]
            assert [(p.topic, p.payload) for p in delivered] == [('will', b'gone')]
            assert 'bad' not in broker._clients
            assert asyncio.all_tasks() <= tasks_before
            assert writer.pop_packets()[-1].reason_code == ReasonCode.MALFORMED_PACKET
        finally:
            await _drain(task, sub_task)


@pytest.mark.parametrize('cap', [0, 1048576])
async def test_four_remaining_length_continuations_close_without_more_input(cap, monkeypatch):
    from blackbull.env import reset_settings_cache

    monkeypatch.setenv('BB_MQTT_MAX_PACKET_SIZE', str(cap))
    reset_settings_cache()
    try:
        async with _running_broker() as broker:
            reader = _FakeReader()
            writer, task = await _serve(broker, reader, _ctx())
            reader.feed_packet(MQTTConnect(client_id='bad-length', clean_start=True, keep_alive=0))
            reader._buf.extend(b'\x30\x80\x80\x80\x80')
            try:
                await asyncio.wait_for(asyncio.shield(task), 1)
                packets = writer.pop_packets()
                assert [type(p) for p in packets] == [MQTTConnack, MQTTDisconnect]
                assert packets[-1].reason_code == ReasonCode.MALFORMED_PACKET
                assert not broker._clients
            finally:
                await _drain(task)
    finally:
        reset_settings_cache()

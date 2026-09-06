"""Bounded MQTT actor handoffs, driven with small queues and gated I/O."""
import asyncio
import logging
from contextlib import asynccontextmanager

import pytest

from blackbull.actor import Actor
from blackbull.mqtt.broker import (
    Attach, BrokerActor, ClientPuback, ClientPubrec, ClientPubcomp, ClientPublish, ClientSubscribe,
    Close, Detach, Send,
)
from blackbull.mqtt.connection import MQTT5Actor, serve_connection
from blackbull.mqtt.messages import (
    MQTTConnect, MQTTConnack, MQTTDisconnect, MQTTPingreq, MQTTPingresp,
    MQTTPublish, MQTTPubrel, MQTTSubscribe, decode_packet, encode_packet,
)
from blackbull.mqtt.mailbox import MailboxClosed, MailboxTooLarge
from blackbull.mqtt.tap import Message, TapActor
from blackbull.server.protocol_registry import ProtocolContext
from blackbull.server.recipient import AbstractReader
from blackbull.server.sender import AbstractWriter

pytestmark = pytest.mark.asyncio


class Writer(AbstractWriter):
    def __init__(self):
        self.release = asyncio.Event()
        self.release.set()
        self.entered = asyncio.Event()
        self.packets = []

    async def write(self, data: bytes) -> None:
        self.entered.set()
        await self.release.wait()
        self.packets.append(decode_packet(data)[0])

    async def drain(self) -> None:
        pass


class Reader(AbstractReader):
    def __init__(self, *packets):
        self.data = bytearray(b''.join(encode_packet(p) for p in packets))
        self.waiting = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def read(self, n: int) -> bytes:
        if self.data:
            data = bytes(self.data[:n])
            del self.data[:n]
            return data
        self.waiting.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()
        return b''


def ctx():
    return ProtocolContext(peername=None, sockname=None, ssl=False,
                           aggregator=None, connection_id='bounds', protocol='mqtt')


def connect(client_id='c', **properties):
    return MQTTConnect(client_id=client_id, clean_start=True, keep_alive=0,
                       properties=properties)


async def settled():
    for _ in range(20):
        await asyncio.sleep(0)


@asynccontextmanager
async def running(*actors):
    tasks = [asyncio.create_task(a.run()) for a in actors]
    try:
        yield tasks
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_mailboxes_have_independent_finite_defaults():
    broker = BrokerActor(max_queued=1)
    conn = MQTT5Actor(Writer(), broker, ctx())
    assert broker._inbox.maxsize == 1024
    assert conn._inbox.maxsize == 1024
    assert broker._inbox.max_bytes == conn._inbox.max_bytes == 16 * 1024 * 1024


@pytest.mark.parametrize('byte_bound', [False, True])
async def test_slow_writer_is_closed_without_holding_up_other_connection(byte_bound, caplog):
    broker = BrokerActor(max_queued=1)
    writer = Writer()
    writer.release.clear()
    packet = MQTTPublish(topic='t', payload=b'normal')
    size = len(encode_packet(packet))
    conn = MQTT5Actor(writer, broker, ctx(), inbox_maxsize=100 if byte_bound else 2,
                      inbox_max_bytes=size * 2 if byte_bound else 1024)
    healthy_writer = Writer()
    healthy = MQTT5Actor(healthy_writer, broker, ctx())
    async with running(conn, healthy) as tasks:
        await conn.send(Send(packet=packet))
        await writer.entered.wait()
        await conn.send(Send(packet=packet))
        await conn.send(Send(packet=packet))
        assert conn._inbox.qsize() == 2
        assert conn._inbox.queued_bytes == size * 2
        await conn.send(Send(packet=packet))
        await healthy.send(Send(packet=MQTTPingresp()))
        await settled()
        assert tasks[0].done()
        assert [type(p) for p in healthy_writer.packets] == [MQTTPingresp]
        assert conn._inbox.empty()
        assert conn._inbox.queued_bytes == 0
        assert 'mqtt_connection_inbox' in caplog.text


async def test_healthy_burst_larger_than_queue_is_delivered_in_order():
    broker = BrokerActor()
    writer = Writer()
    conn = MQTT5Actor(writer, broker, ctx(), inbox_maxsize=2)
    async with running(conn):
        for i in range(2048):
            await conn.send(Send(packet=MQTTPublish(topic='t', payload=str(i).encode())))
        await conn.send(Close())
        await settled()
        assert [p.payload for p in writer.packets] == [str(i).encode() for i in range(2048)]


@pytest.mark.parametrize('byte_bound', [False, True])
async def test_broker_backpressures_then_admits_ack_and_detach(byte_bound):
    packet = MQTTPublish(topic='t', payload=b'normal')
    broker = BrokerActor(inbox_maxsize=100 if byte_bound else 1,
                         inbox_max_bytes=len(encode_packet(packet)) if byte_bound else 1024)
    conn = Actor()
    await broker.send(ClientPublish(publish=packet, sender=conn))
    blocked = asyncio.create_task(broker.send(ClientPuback(packet_id=1, sender=conn)))
    try:
        await settled()
        assert not blocked.done()
        assert broker._inbox.qsize() == 1
        async with running(broker):
            async with asyncio.timeout(1):
                await blocked
                await broker.send(Detach(sender=conn))
                await broker._inbox.join()
            assert broker._inbox.empty()
    finally:
        blocked.cancel()
        await asyncio.gather(blocked, return_exceptions=True)


async def test_broker_shutdown_releases_blocked_producer():
    broker = BrokerActor(inbox_maxsize=1)
    await broker.send(Detach())
    task = asyncio.create_task(broker.send(Detach()))
    await settled()
    assert not task.done()
    broker.close()
    async with asyncio.timeout(1):
        result = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result[0], RuntimeError)
    assert broker._inbox.empty()


async def test_expiry_notification_survives_full_broker_mailbox():
    broker = BrokerActor(inbox_maxsize=1)
    conn = Actor()
    await broker._handle(Attach(connect=connect(session_expiry_interval=1), sender=conn))
    await broker._handle(Detach(sender=conn))
    broker._sessions['c']['_expires_at'] = asyncio.get_running_loop().time() - 1
    await broker.send(Detach())
    broker._post_sweep()
    async with running(broker):
        await settled()
        assert 'c' not in broker._sessions


async def test_close_flushes_queued_ack_and_wakes_blocked_reader(monkeypatch):
    monkeypatch.setenv('BB_MQTT_CONNECTION_INBOX_MAXSIZE', '1')
    broker = BrokerActor(max_sessions=1)
    await broker._handle(Attach(connect=connect('occupied'), sender=Actor()))
    writer = Writer()
    reader = Reader(connect('refused'))
    async with running(broker):
        async with asyncio.timeout(1):
            await serve_connection(reader, writer, ctx(), broker)
    assert len(writer.packets) == 1
    assert isinstance(writer.packets[0], MQTTConnack)
    assert writer.packets[0].reason_code != 0


async def test_cancelled_connection_detaches_when_broker_is_full():
    broker = BrokerActor(inbox_maxsize=1)
    reader = Reader(connect())
    serving = asyncio.create_task(serve_connection(reader, Writer(), ctx(), broker))
    try:
        await reader.waiting.wait()
        assert broker._inbox.qsize() == 1
        serving.cancel()
        await settled()
        async with running(broker):
            async with asyncio.timeout(1):
                await asyncio.gather(serving, return_exceptions=True)
                await broker._inbox.join()
            assert not broker._clients
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
        broker.close()


@pytest.mark.parametrize('qos', [0, 1, 2])
@pytest.mark.parametrize('shared', [False, True])
async def test_live_routing_uses_bounded_writer_for_every_qos(qos, shared):
    broker = BrokerActor(max_queued=1)
    writer = Writer()
    conn = MQTT5Actor(writer, broker, ctx(), inbox_maxsize=2)
    async with running(conn):
        await broker._handle(Attach(connect=connect(receive_maximum=8), sender=conn))
        topic_filter = '$share/g/t' if shared else 't'
        await broker._handle(ClientSubscribe(sender=conn, subscribe=MQTTSubscribe(
            packet_id=1, subscriptions=[(topic_filter, qos)])))
        for i in range(4):
            await broker._handle(ClientPublish(sender=Actor(), publish=MQTTPublish(
                topic='t', payload=str(i).encode(), qos=qos, packet_id=i + 1 if qos else None)))
        await conn.send(Close())
        await settled()
        packets = [p for p in writer.packets if isinstance(p, MQTTPublish)]
        assert [p.payload for p in packets] == [str(i).encode() for i in range(4)]
        assert all(p.qos == qos for p in packets)
    broker.close()


async def test_retained_replay_larger_than_mailbox_reaches_healthy_writer():
    broker = BrokerActor()
    for i in range(5):
        await broker._handle(ClientPublish(sender=Actor(), publish=MQTTPublish(
            topic=f't/{i}', payload=b'normal', retain=True)))
    writer = Writer()
    conn = MQTT5Actor(writer, broker, ctx(), inbox_maxsize=2)
    async with running(conn):
        await broker._handle(Attach(connect=connect(), sender=conn))
        await broker._handle(ClientSubscribe(sender=conn, subscribe=MQTTSubscribe(
            packet_id=1, subscriptions=[('t/#', 0)])))
        await conn.send(Close())
        await settled()
        assert [p.topic for p in writer.packets if isinstance(p, MQTTPublish)] == [
            f't/{i}' for i in range(5)]
    broker.close()


async def test_stateless_reply_and_disconnect_share_sole_writer():
    broker = BrokerActor()
    writer = Writer()
    reader = Reader(connect(), MQTTPingreq(), MQTTDisconnect())
    async with running(broker):
        async with asyncio.timeout(1):
            await serve_connection(reader, writer, ctx(), broker)
    assert any(isinstance(p, MQTTPingresp) for p in writer.packets)
    assert not broker._clients


async def test_maximum_default_packet_fits_both_mailboxes():
    broker = BrokerActor()
    maximum = broker._max_packet_size
    payload = b'x' * maximum
    for _ in range(3):
        wire = encode_packet(MQTTPublish(topic='t', payload=payload))
        payload = payload[:len(payload) - max(0, len(wire) - maximum)]
    packet = MQTTPublish(topic='t', payload=payload)
    assert len(encode_packet(packet)) == maximum
    await broker.send(ClientPublish(publish=packet))
    writer = Writer()
    conn = MQTT5Actor(writer, broker, ctx())
    async with running(conn):
        await conn.send(Send(packet=packet))
        await conn.send(Close())
        await settled()
        assert [p.payload for p in writer.packets] == [payload]
    broker.close()


async def test_single_packet_exceeding_input_budget_is_refused_without_waiting(caplog):
    broker = BrokerActor(inbox_max_bytes=8)
    with pytest.raises(MailboxTooLarge):
        await broker.send(ClientPublish(publish=MQTTPublish(topic='t', payload=b'normal')))
    assert broker._inbox.empty()
    assert 'mqtt_broker_inbox_max_bytes' in caplog.text
    broker.close()


async def test_cancelled_admission_does_not_consume_queue_capacity():
    broker = BrokerActor(inbox_maxsize=1)
    await broker.send(Detach())
    blocked = asyncio.create_task(broker.send(Detach()))
    await settled()
    blocked.cancel()
    await asyncio.gather(blocked, return_exceptions=True)
    async with running(broker):
        await broker._inbox.join()
        await broker.send(Detach())
        await broker._inbox.join()
        assert broker._inbox.queued_bytes == 0


async def test_broker_shutdown_releases_admitted_detach_barrier():
    broker = BrokerActor()
    processed = asyncio.Event()
    await broker.send(Detach(processed=processed))
    broker.close()
    assert processed.is_set()
    with pytest.raises(MailboxClosed):
        await broker.send(Detach())


async def test_writer_timeout_wakes_silent_reader_and_detaches(monkeypatch):
    monkeypatch.setenv('BB_WRITE_TIMEOUT', '0.01')
    writer = Writer()
    writer.release.clear()
    broker = BrokerActor()
    async with running(broker):
        async with asyncio.timeout(1):
            await serve_connection(Reader(connect()), writer, ctx(), broker)
            await broker._inbox.join()
        assert not broker._clients


async def test_writer_failure_wakes_silent_reader_and_detaches():
    class BrokenWriter(Writer):
        async def write(self, data: bytes) -> None:
            raise OSError('test writer closed')

    broker = BrokerActor()
    async with running(broker):
        async with asyncio.timeout(1):
            await serve_connection(Reader(connect()), BrokenWriter(), ctx(), broker)
            await broker._inbox.join()
        assert not broker._clients


@pytest.mark.parametrize('qos', [1, 2])
async def test_ack_drains_qos_backlog_without_waiting_on_connection_mailbox(qos):
    broker = BrokerActor(inbox_maxsize=1, max_queued=1)
    writer = Writer()
    conn = MQTT5Actor(writer, broker, ctx(), inbox_maxsize=1)
    async with running(broker, conn):
        await broker.send(Attach(connect=connect(receive_maximum=1), sender=conn))
        await broker.send(ClientSubscribe(sender=conn, subscribe=MQTTSubscribe(
            packet_id=1, subscriptions=[('t', qos)])))
        for pid in [1, 2]:
            await broker.send(ClientPublish(sender=Actor(), publish=MQTTPublish(
                topic='t', payload=str(pid).encode(), qos=qos, packet_id=pid)))
        await broker._inbox.join()
        await settled()
        publishes = [p for p in writer.packets if isinstance(p, MQTTPublish)]
        assert [p.payload for p in publishes] == [b'1']
        pid = publishes[0].packet_id
        if qos == 1:
            await broker.send(ClientPuback(sender=conn, packet_id=pid))
        else:
            await broker.send(ClientPubrec(sender=conn, packet_id=pid))
            await broker._inbox.join()
            await settled()
            assert any(isinstance(p, MQTTPubrel) for p in writer.packets)
            await broker.send(ClientPubcomp(sender=conn, packet_id=pid))
        await broker._inbox.join()
        await settled()
        assert [p.payload for p in writer.packets if isinstance(p, MQTTPublish)] == [b'1', b'2']


async def test_slow_tap_retains_its_separate_drop_policy():
    entered = asyncio.Event()
    release = asyncio.Event()
    received = []

    async def tap(message):
        entered.set()
        await release.wait()
        received.append(message.payload)

    actor = TapActor([('t', tap)], queue_size=1)
    async with running(actor):
        actor.offer(Message(topic='t', payload=b'active'))
        await entered.wait()
        actor.offer(Message(topic='t', payload=b'queued'))
        actor.offer(Message(topic='t', payload=b'overflow'))
        assert actor.dropped == 1
        release.set()
        await settled()
        assert received == [b'active', b'queued']


async def test_full_broker_and_writer_do_not_form_a_cyclic_send_wait():
    broker = BrokerActor(inbox_maxsize=1)
    writer = Writer()
    conn = MQTT5Actor(writer, broker, ctx(), inbox_maxsize=1)
    async with running(conn):
        await broker._handle(Attach(connect=connect(), sender=conn))
        await broker._handle(ClientSubscribe(sender=conn, subscribe=MQTTSubscribe(
            packet_id=1, subscriptions=[('t', 0)])))
        await settled()
        writer.entered.clear()
        writer.release.clear()
        await conn.send(Send(packet=MQTTPingresp()))
        await writer.entered.wait()
        await conn.send(Send(packet=MQTTPingresp()))
        packet = MQTTPublish(topic='t', payload=b'normal')
        await broker.send(ClientPublish(sender=conn, publish=packet))
        reader = asyncio.create_task(conn._forward(packet))
        try:
            await settled()
            assert not reader.done()
            async with running(broker):
                async with asyncio.timeout(1):
                    await reader
                    await broker.send(Detach(sender=conn))
                    await broker._inbox.join()
                assert not broker._clients
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)


async def test_overloaded_output_ends_serving_and_releases_reader(monkeypatch):
    monkeypatch.setenv('BB_MQTT_CONNECTION_INBOX_MAXSIZE', '1')
    broker = BrokerActor(inbox_maxsize=1)
    writer = Writer()
    writer.release.clear()
    reader = Reader(connect(), MQTTPingreq(), MQTTPingreq(), MQTTPingreq())
    async with running(broker):
        tasks_before = asyncio.all_tasks()
        async with asyncio.timeout(1):
            await serve_connection(reader, writer, ctx(), broker)
            await broker._inbox.join()
        assert not broker._clients
        assert not asyncio.all_tasks() - tasks_before


async def test_input_budget_refusal_reaches_the_wire(monkeypatch):
    monkeypatch.setenv('BB_MQTT_BROKER_INBOX_MAX_BYTES', '32')
    broker = BrokerActor()
    writer = Writer()
    reader = Reader(connect(), MQTTPublish(topic='t', payload=b'x' * 64))
    async with running(broker):
        async with asyncio.timeout(1):
            await serve_connection(reader, writer, ctx(), broker)
            await broker._inbox.join()
        assert any(isinstance(p, MQTTDisconnect) and p.reason_code == 0x97
                   for p in writer.packets)
        assert not broker._clients


async def test_single_output_packet_larger_than_budget_closes_and_logs(caplog):
    broker = BrokerActor()
    conn = MQTT5Actor(Writer(), broker, ctx(), inbox_max_bytes=8)
    async with running(conn) as tasks:
        await conn.send(Send(packet=MQTTPublish(topic='t', payload=b'normal')))
        await settled()
        assert tasks[0].done()
        assert conn._inbox.empty()
        assert 'mqtt_connection_inbox_max_bytes' in caplog.text


@pytest.mark.parametrize('qos', [1, 2])
async def test_output_overload_keeps_persistent_pending_qos_for_reconnect(qos):
    broker = BrokerActor()
    writer = Writer()
    conn = MQTT5Actor(writer, broker, ctx(), inbox_maxsize=1)
    async with running(conn):
        await broker._handle(Attach(connect=connect(session_expiry_interval=60), sender=conn))
        await broker._handle(ClientSubscribe(sender=conn, subscribe=MQTTSubscribe(
            packet_id=1, subscriptions=[('t', qos)])))
        await settled()
        writer.release.clear()
        writer.entered.clear()
        await conn.send(Send(packet=MQTTPingresp()))
        await writer.entered.wait()
        for i in [1, 2]:
            await broker._handle(ClientPublish(sender=Actor(), publish=MQTTPublish(
                topic='t', payload=str(i).encode(), qos=qos, packet_id=i)))
        await broker._handle(Detach(sender=conn, graceful=False))
    # A resumed persistent session must retransmit the unacknowledged packets,
    # including the packet whose writer admission triggered the disconnect.
    resumed_writer = Writer()
    resumed = MQTT5Actor(resumed_writer, broker, ctx(), inbox_maxsize=1)
    async with running(resumed):
        await broker._handle(Attach(sender=resumed, connect=MQTTConnect(
            client_id='c', clean_start=False, keep_alive=0,
            properties={'session_expiry_interval': 60})))
        await resumed.send(Close())
        await settled()
        publishes = [p for p in resumed_writer.packets if isinstance(p, MQTTPublish)]
        assert [p.payload for p in publishes] == [b'1', b'2']
        assert all(p.dup for p in publishes)
    broker.close()


@pytest.mark.parametrize('value', ['0', '-1', 'invalid'])
async def test_invalid_environment_limits_keep_finite_defaults(monkeypatch, value):
    for name in ['BROKER_INBOX_MAXSIZE', 'BROKER_INBOX_MAX_BYTES',
                 'CONNECTION_INBOX_MAXSIZE', 'CONNECTION_INBOX_MAX_BYTES']:
        monkeypatch.setenv('BB_MQTT_' + name, value)
    broker = BrokerActor()
    conn = MQTT5Actor(Writer(), broker, ctx())
    assert broker._inbox.maxsize == conn._inbox.maxsize == 1024
    assert broker._inbox.max_bytes == conn._inbox.max_bytes == 16 * 1024 * 1024
    broker.close()


async def test_broker_close_interrupts_reader_before_waiting_for_writer():
    broker = BrokerActor(max_sessions=1)
    await broker._handle(Attach(connect=connect('occupied'), sender=Actor()))
    writer = Writer()
    writer.release.clear()
    reader = Reader(connect('refused'))
    async with running(broker):
        serving = asyncio.create_task(serve_connection(reader, writer, ctx(), broker))
        try:
            async with asyncio.timeout(1):
                await reader.cancelled.wait()
            assert not serving.done()
            writer.release.set()
            async with asyncio.timeout(1):
                await serving
            assert len(writer.packets) == 1
            assert isinstance(writer.packets[0], MQTTConnack)
        finally:
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)


async def test_connection_mailbox_shutdown_logs_after_flushing_packets(caplog):
    caplog.set_level(logging.DEBUG, logger='blackbull.mqtt.connection')
    broker = BrokerActor()
    writer = Writer()
    conn = MQTT5Actor(writer, broker, ctx())
    await conn.send(Send(packet=MQTTPingresp()))
    await conn.send(Close())
    async with asyncio.timeout(1):
        await conn.run()
    assert [type(packet) for packet in writer.packets] == [MQTTPingresp]
    assert 'MQTT connection mailbox closed; stopping writer loop.' in caplog.text
    broker.close()


async def test_serving_waits_for_pending_flush_after_detach():
    class DetachObservedBroker(BrokerActor):
        def __init__(self):
            super().__init__(max_sessions=1)
            self.detached = asyncio.Event()

        async def _handle(self, msg):
            await super()._handle(msg)
            if isinstance(msg, Detach):
                self.detached.set()

    broker = DetachObservedBroker()
    await broker._handle(Attach(connect=connect('occupied'), sender=Actor()))
    writer = Writer()
    writer.release.clear()
    reader = Reader(connect('refused'))
    async with running(broker):
        serving = asyncio.create_task(serve_connection(reader, writer, ctx(), broker))
        try:
            async with asyncio.timeout(1):
                await writer.entered.wait()
                await broker.detached.wait()
            await settled()
            assert not serving.done(), 'detach is not completion of the pending write'
            assert not writer.packets
            writer.release.set()
            async with asyncio.timeout(1):
                await serving
            assert [type(packet) for packet in writer.packets] == [MQTTConnack]
        finally:
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)

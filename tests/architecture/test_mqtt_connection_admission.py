"""MQTT connection admission at the FIFO broker/connection boundary."""
import asyncio
import gc
import weakref
from copy import deepcopy

import pytest

from blackbull.actor import Actor
from blackbull.mqtt.broker import (
    Attach, BrokerActor, ClientPuback, ClientPubcomp, ClientPublish,
    ClientPubrec, ClientPubrel, ClientSubscribe, ClientUnsubscribe, Close, Detach, Send,
)
from blackbull.mqtt.connection import MQTT5Actor, serve_connection
from blackbull.mqtt.messages import (
    MQTTAuth, MQTTConnect, MQTTConnack, MQTTDisconnect, MQTTPingreq, MQTTPingresp,
    MQTTPublish, MQTTSubscribe, MQTTUnsubscribe, ProtocolLevel, ReasonCode,
    decode_packet, encode_packet,
)
from blackbull.mqtt.tap import TapActor
from blackbull.server.protocol_registry import ProtocolContext
from blackbull.server.recipient import AbstractReader
from blackbull.server.sender import AbstractWriter

pytestmark = pytest.mark.asyncio


class Peer(Actor):
    def __init__(self):
        super().__init__()
        self.messages = []

    async def send(self, message):
        self.messages.append(message)

    def packets(self):
        return [m.packet for m in self.messages if isinstance(m, Send)]


class Writer(AbstractWriter):
    def __init__(self):
        self.packets = []

    async def write(self, data: bytes) -> None:
        self.packets.append(decode_packet(data)[0])

    async def drain(self) -> None:
        pass


class Reader(AbstractReader):
    def __init__(self, *packets):
        self.data = b''.join(encode_packet(p) for p in packets)
        self.waiting = asyncio.Event()

    async def read(self, n: int) -> bytes:
        if self.data:
            data, self.data = self.data[:n], self.data[n:]
            return data
        self.waiting.set()
        await asyncio.Event().wait()
        return b''


def context():
    return ProtocolContext(peername=None, sockname=None, ssl=False,
                           aggregator=None, connection_id='admission', protocol='mqtt')


def connect(client_id='publisher', **kw):
    return MQTTConnect(client_id=client_id, clean_start=kw.pop('clean_start', True),
                       keep_alive=0, **kw)


async def attach(broker, peer, client_id, **kw):
    await broker._handle(Attach(sender=peer, connect=connect(client_id, **kw)))


async def subscribe(broker, peer, topic='sentinel'):
    await broker._handle(ClientSubscribe(sender=peer, subscribe=MQTTSubscribe(
        packet_id=1, subscriptions=[(topic, 2)])))


async def drain(broker):
    while not broker._inbox.empty():
        message = broker._inbox.get_nowait()
        try:
            await broker._handle(message)
        finally:
            broker._inbox.task_done()


@pytest.mark.parametrize('rejection', ['none', 'quota', 'version'])
@pytest.mark.parametrize('qos', [0, 1, 2])
async def test_pipelined_publish_requires_accepted_connect(rejection, qos):
    broker = BrokerActor(max_sessions=1 if rejection == 'quota' else 0)
    observer = Peer()
    await attach(broker, observer, 'observer')
    await subscribe(broker, observer)
    observer.messages.clear()
    conn = MQTT5Actor(Writer(), broker, context())
    packet = connect(proto_level=ProtocolLevel.V3_1_1 if rejection == 'version'
                     else ProtocolLevel.V5_0)
    await conn._forward(packet)
    await conn._forward(MQTTPublish(topic='sentinel', payload=b'local-control',
                                   qos=qos, packet_id=7 if qos else None, retain=True))
    await drain(broker)
    accepted = rejection == 'none'
    deliveries = [p.payload for p in observer.packets() if isinstance(p, MQTTPublish)]
    assert deliveries == ([b'local-control'] if accepted else [])
    late = Peer()
    if not accepted:
        await broker._handle(Detach(sender=observer))
    await attach(broker, late, 'late')
    await subscribe(broker, late)
    assert [p.payload for p in late.packets() if isinstance(p, MQTTPublish)] == (
        [b'local-control'] if accepted else [])
    broker.close()


@pytest.mark.parametrize('retirement', ['detach', 'takeover', 'duplicate', 'bad_subscribe',
                                        'bad_publish'])
@pytest.mark.parametrize('command', ['publish', 'subscribe', 'unsubscribe', 'puback',
                                     'pubrec', 'pubrel', 'pubcomp'])
async def test_queued_commands_cannot_change_retired_or_replacement_session(retirement, command):
    broker = BrokerActor()
    old, observer = Peer(), Peer()
    await attach(broker, observer, 'observer')
    await subscribe(broker, observer)
    await attach(broker, old, 'owner', properties={'session_expiry_interval': 60})
    await subscribe(broker, old, 'preserve')
    for pid, qos, payload in [(8, 1, b'q1'), (9, 2, b'q2-rec'), (10, 2, b'q2-comp')]:
        await broker._handle(ClientPublish(sender=observer, publish=MQTTPublish(
            topic='preserve', payload=payload, qos=qos, packet_id=pid)))
    outbound = {p.payload: p.packet_id for p in old.packets() if isinstance(p, MQTTPublish)}
    await broker._handle(ClientPubrec(sender=old, packet_id=outbound[b'q2-comp']))
    await broker._handle(ClientPublish(sender=old, publish=MQTTPublish(
        topic='unobserved', payload=b'inbound', qos=2, packet_id=22)))
    if retirement == 'detach':
        await broker._handle(Detach(sender=old))
    elif retirement == 'takeover':
        await attach(broker, Peer(), 'owner', clean_start=False,
                     properties={'session_expiry_interval': 60})
    elif retirement == 'duplicate':
        await attach(broker, old, 'different')
    elif retirement == 'bad_subscribe':
        await broker._handle(ClientSubscribe(sender=old, subscribe=MQTTSubscribe(
            packet_id=2, subscriptions=[('$share/group/t', 0)],
            subscription_options=[{'no_local': True}])))
    else:
        await broker._handle(ClientPublish(sender=old, publish=MQTTPublish(
            topic='bad/+', payload=b'invalid')))
    before = deepcopy(broker._sessions)
    old.messages.clear()
    observer.messages.clear()
    messages = {
        'publish': ClientPublish(sender=old, publish=MQTTPublish(
            topic='sentinel', payload=b'late', qos=2, packet_id=17, retain=True)),
        'subscribe': ClientSubscribe(sender=old, subscribe=MQTTSubscribe(
            packet_id=18, subscriptions=[('late', 0)])),
        'unsubscribe': ClientUnsubscribe(sender=old, unsubscribe=MQTTUnsubscribe(
            packet_id=19, topics=['preserve'])),
        'puback': ClientPuback(sender=old, packet_id=outbound[b'q1']),
        'pubrec': ClientPubrec(sender=old, packet_id=outbound[b'q2-rec']),
        'pubrel': ClientPubrel(sender=old, packet_id=22),
        'pubcomp': ClientPubcomp(sender=old, packet_id=outbound[b'q2-comp']),
    }
    await broker.send(messages[command])
    await drain(broker)
    assert broker._sessions == before
    assert old.packets() == []
    assert observer.packets() == []
    assert not broker._retained
    broker.close()


@pytest.mark.parametrize('end', ['rejected', 'detached', 'takeover'])
async def test_connection_cannot_reenter_after_retirement(end):
    broker, old = BrokerActor(), Peer()
    if end == 'rejected':
        await attach(broker, old, 'owner', proto_level=ProtocolLevel.V3_1_1)
    else:
        await attach(broker, old, 'owner')
        if end == 'detached':
            await broker._handle(Detach(sender=old))
        else:
            await attach(broker, Peer(), 'owner')
    old.messages.clear()
    before = deepcopy(broker._sessions)
    await attach(broker, old, 'new-id')
    assert not [p for p in old.packets() if isinstance(p, MQTTConnack)
                and p.reason_code == ReasonCode.SUCCESS]
    assert broker._sessions == before
    broker.close()


async def test_publish_before_connect_cannot_establish_a_connection():
    broker, peer = BrokerActor(), Peer()
    await broker._handle(ClientPublish(sender=peer, publish=MQTTPublish(
        topic='sentinel', payload=b'before', retain=True)))
    await attach(broker, peer, 'owner')
    assert not broker._retained
    assert not broker._clients
    broker.close()


@pytest.mark.parametrize('accepted', [False, True])
async def test_pipelined_stateless_reply_follows_admission(accepted):
    broker, writer = BrokerActor(max_sessions=1), Writer()
    if not accepted:
        await attach(broker, Peer(), 'occupant')
    reader = Reader(connect(), MQTTPingreq(), MQTTAuth(), MQTTDisconnect())
    task = asyncio.create_task(broker.run())
    try:
        async with asyncio.timeout(2):
            await serve_connection(reader, writer, context(), broker)
        packets = writer.packets
        assert isinstance(packets[0], MQTTConnack)
        assert [type(p) for p in packets] == (
            [MQTTConnack, MQTTPingresp, MQTTAuth] if accepted else [MQTTConnack])
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('accepted', [False, True])
@pytest.mark.parametrize('mode', ['actor', 'inline'])
async def test_taps_only_observe_broker_admitted_publish(accepted, mode):
    seen = []

    async def observe(message):
        seen.append(message.payload)

    handlers = [('#', observe)]
    tap = TapActor(handlers) if mode == 'actor' else None
    broker = BrokerActor(max_sessions=1)
    if not accepted:
        await attach(broker, Peer(), 'occupant')
    conn = MQTT5Actor(Writer(), broker, context(), tap=tap,
                      app_handlers=handlers if mode == 'inline' else None)
    tasks = [asyncio.create_task(broker.run())]
    if tap is not None:
        tasks.append(asyncio.create_task(tap.run()))
    try:
        await conn._forward(connect())
        async with asyncio.timeout(2):
            await conn._forward(MQTTPublish(topic='sentinel', payload=b'observed'))
            await broker._inbox.join()
        for _ in range(10):
            await asyncio.sleep(0)
        assert seen == ([b'observed'] if accepted else [])
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize('shutdown', [False, True])
async def test_pending_tap_admission_can_be_cancelled_or_broker_closed(shutdown):
    seen = []

    async def observe(message):
        seen.append(message.payload)

    broker = BrokerActor()
    conn = MQTT5Actor(Writer(), broker, context(), app_handlers=[('#', observe)])
    await attach(broker, conn, 'owner')
    task = asyncio.create_task(conn._forward(MQTTPublish(topic='sentinel', payload=b'pending')))
    try:
        await asyncio.sleep(0)
        assert not task.done()
        assert broker._inbox.qsize() == 1
        if shutdown:
            broker.close()
            async with asyncio.timeout(1):
                await task
        else:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await drain(broker)
            await broker._handle(ClientPublish(sender=conn, publish=MQTTPublish(
                topic='control', payload=b'healthy', retain=True)))
            assert broker._retained['control'].payload == b'healthy'
        assert seen == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        broker.close()


async def test_duplicate_connect_closes_once_without_changing_client_id():
    broker, observer, peer = BrokerActor(), Peer(), Peer()
    await attach(broker, observer, 'observer')
    await subscribe(broker, observer)
    await attach(broker, peer, 'owner', will_topic='sentinel', will_payload=b'will')
    await attach(broker, peer, 'new-id')
    await broker._handle(Detach(sender=peer, graceful=False))
    assert [p.reason_code for p in peer.packets() if isinstance(p, MQTTConnack)] == [0]
    assert [p.reason_code for p in peer.packets() if isinstance(p, MQTTDisconnect)] == [0x82]
    assert len([m for m in peer.messages if isinstance(m, Close)]) == 1
    assert [p.payload for p in observer.packets() if isinstance(p, MQTTPublish)] == [b'will']
    assert 'owner' not in broker._clients
    assert 'new-id' not in broker._sessions
    broker.close()


async def test_admission_history_does_not_keep_retired_actors_alive():
    broker = BrokerActor()
    peer = Peer()
    await attach(broker, peer, 'owner')
    await broker._handle(Detach(sender=peer))
    reference = weakref.ref(peer)
    del peer
    gc.collect()
    assert reference() is None
    await attach(broker, Peer(), 'owner')
    assert 'owner' in broker._clients
    broker.close()


@pytest.mark.parametrize('first', [False, True])
@pytest.mark.parametrize('packet', [MQTTConnack(), MQTTPingresp()])
async def test_wrong_direction_packet_cannot_be_ignored_to_keep_admission(first, packet):
    broker = BrokerActor()
    conn = MQTT5Actor(Writer(), broker, context())
    if not first:
        await conn._forward(connect())
    await conn._forward(packet)
    if first:
        await conn._forward(connect())
    await conn._forward(MQTTPublish(topic='sentinel', payload=b'late', retain=True))
    await drain(broker)
    assert not broker._retained
    assert not broker._clients
    broker.close()

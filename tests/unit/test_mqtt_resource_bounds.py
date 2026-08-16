"""MQTT resource bounds — the surface that had no total caps and no cap log.

Every other protocol in this tree answers the same three questions: how
big may one unit be, how much may accumulate, and how long may it take.
MQTT answered none of them.  A peer could declare a 256 MiB packet and
dribble it, subscribe and never acknowledge so the broker held every
matching message forever, and retain one message per topic without limit
— and none of it appeared in ``blackbull.caps``, so an operator could not
see any of it happen.

The tests here drive broker and framer behaviour, never codec
round-trips: what matters is the packet the broker emits and the state it
refuses to grow.
"""
import asyncio
import contextlib

import pytest

from blackbull.actor import Actor
from blackbull.mqtt.broker import (
    Attach, BrokerActor, ClientPuback, ClientPublish, ClientSubscribe,
    Close, Send,
)
from blackbull.mqtt.connection import MQTT5Actor, PacketFramer, PacketTooLarge
from blackbull.mqtt.messages import (
    MQTTConnack, MQTTConnect, MQTTDisconnect, MQTTPublish, MQTTSubscribe,
    ReasonCode, encode_packet, encode_variable_byte_integer,
)
from blackbull.server.protocol_registry import ProtocolContext
from blackbull.server.recipient import AbstractReader
from blackbull.server.sender import AbstractWriter

pytestmark = pytest.mark.asyncio


class RecordingConn(Actor):
    """A fake connection actor that records what the broker sends it."""

    def __init__(self) -> None:
        super().__init__()
        self.outbox = []

    async def send(self, msg) -> None:  # override: record instead of enqueue
        self.outbox.append(msg)

    def packets(self) -> list:
        return [m.packet for m in self.outbox if isinstance(m, Send)]

    def publishes(self) -> list:
        return [p for p in self.packets() if isinstance(p, MQTTPublish)]


class _Reader(AbstractReader):
    """Feeds pre-built bytes, then reports EOF."""

    def __init__(self, data: bytes = b''):
        self._buf = bytearray(data)
        self._eof = False

    def feed(self, data: bytes) -> None:
        self._buf += data

    async def read(self, n: int) -> bytes:
        if not self._buf:
            self._eof = True
            return b''
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    async def readexactly(self, n: int) -> bytes:
        return await self.read(n)

    async def readuntil(self, sep: bytes) -> bytes:  # pragma: no cover
        raise NotImplementedError

    def at_eof(self) -> bool:
        return self._eof and not self._buf


class _Writer(AbstractWriter):
    def __init__(self):
        self.written = bytearray()
        self.closed = False

    async def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


def _ctx():
    return ProtocolContext(peername=('127.0.0.1', 5), sockname=('0.0.0.0', 1883),
                           ssl=False, aggregator=None, connection_id='c',
                           protocol='mqtt')


def _oversized_header(declared: int, packet_type: int = 0x30) -> bytes:
    """A fixed header declaring *declared* body bytes, and nothing else.

    The point of building it by hand: the test must never allocate the
    body it claims to send, or the test itself would be the memory
    problem it is checking for.
    """
    return bytes([packet_type]) + encode_variable_byte_integer(declared)


async def _attach(broker, conn, **kw):
    await broker._handle(Attach(
        connect=MQTTConnect(client_id=kw.pop('client_id', 'c1'),
                            clean_start=kw.pop('clean_start', True),
                            keep_alive=kw.pop('keep_alive', 60), **kw),
        sender=conn))


async def _subscribe(broker, conn, topic='t', qos=1):
    await broker._handle(ClientSubscribe(
        subscribe=MQTTSubscribe(packet_id=1, subscriptions=[(topic, qos)]),
        sender=conn))


async def _publish(broker, source, topic='t', payload=b'x', qos=0, retain=False,
                   packet_id=None):
    await broker._handle(ClientPublish(
        publish=MQTTPublish(topic=topic, payload=payload, qos=qos,
                            packet_id=packet_id, retain=retain),
        sender=source))


# ===========================================================================
# G3 — one packet may not be unbounded
# ===========================================================================

class TestPacketSizeBound:
    async def test_declared_oversize_is_refused_without_buffering(self):
        """The header alone must decide it — the body is never accumulated.

        A peer that declares 256 MiB and then dribbles is the attack; a
        framer that waits for the whole packet before judging it has
        already lost, whatever it does next.
        """
        framer = PacketFramer(max_packet_size=4096)
        framer.feed(_oversized_header(64 * 1024 * 1024) + b'\x00' * 100)

        with pytest.raises(PacketTooLarge) as exc:
            list(framer)
        assert exc.value.declared > 4096
        assert exc.value.maximum == 4096
        assert len(framer.buffered) < 4096, (
            f'{len(framer.buffered)} bytes buffered for a packet already known '
            f'to be over the cap'
        )

    async def test_packet_exactly_at_the_cap_is_accepted(self):
        payload = b'p' * 200
        packet = encode_packet(MQTTPublish(topic='t', payload=payload, qos=0))
        framer = PacketFramer(max_packet_size=len(packet))
        framer.feed(packet)

        decoded = list(framer)
        assert len(decoded) == 1
        assert decoded[0].payload == payload

    async def test_one_byte_over_the_cap_is_refused(self):
        packet = encode_packet(MQTTPublish(topic='t', payload=b'p' * 200, qos=0))
        framer = PacketFramer(max_packet_size=len(packet) - 1)
        framer.feed(packet)

        with pytest.raises(PacketTooLarge):
            list(framer)

    async def test_zero_disables_the_cap(self):
        packet = encode_packet(MQTTPublish(topic='t', payload=b'p' * 200, qos=0))
        framer = PacketFramer(max_packet_size=0)
        framer.feed(packet)
        assert len(list(framer)) == 1

    async def test_connection_answers_disconnect_0x95_and_closes(self):
        """§3.14.2.1 — 0x95 Packet Too Large, then the connection ends."""
        reader = _Reader(_oversized_header(64 * 1024 * 1024) + b'\x00' * 50)
        writer = _Writer()
        broker = BrokerActor()
        actor = MQTT5Actor(writer, broker, _ctx(), max_packet_size=4096)
        drain = asyncio.create_task(actor.run())
        try:
            await actor.read_loop(reader)
            await asyncio.sleep(0)
        finally:
            drain.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await drain

        sent = bytes(writer.written)
        assert sent, 'connection closed silently; the peer learns nothing'
        assert sent[0] >> 4 == 14, f'expected DISCONNECT (type 14), got {sent[0] >> 4}'
        assert ReasonCode.PACKET_TOO_LARGE in sent, (
            f'DISCONNECT did not carry 0x95: {sent!r}')

    async def test_cap_hit_is_logged(self, caplog):
        framer = PacketFramer(max_packet_size=4096)
        framer.feed(_oversized_header(64 * 1024 * 1024))

        with caplog.at_level('WARNING', logger='blackbull.caps'):
            with pytest.raises(PacketTooLarge):
                list(framer)

        hits = [r for r in caplog.records
                if getattr(r, 'cap', None) == 'mqtt_max_packet_size']
        assert hits, 'MQTT is still invisible in blackbull.caps'
        assert hits[0].protocol == 'mqtt'
        assert hits[0].limit == 4096

    async def test_connack_advertises_the_limits(self):
        broker = BrokerActor()
        conn = RecordingConn()
        await _attach(broker, conn)

        connacks = [p for p in conn.packets() if isinstance(p, MQTTConnack)]
        assert connacks, 'no CONNACK'
        props = connacks[0].properties
        assert props.get('maximum_packet_size') == 1024 * 1024, (
            'a client cannot honour a limit the broker never states')
        assert props.get('receive_maximum') == 64


# ===========================================================================
# G3b — receive_maximum decoded but never enforced
# ===========================================================================

class TestReceiveMaximum:
    async def test_in_flight_is_capped_at_the_clients_receive_maximum(self):
        """§4.9 — never more unacknowledged PUBLISH packets than the client allows."""
        broker = BrokerActor()
        sub, pub = RecordingConn(), RecordingConn()
        await _attach(broker, sub, client_id='sub',
                      properties={'receive_maximum': 2})
        await _attach(broker, pub, client_id='pub')
        await _subscribe(broker, sub, 't', qos=1)

        for i in range(5):
            await _publish(broker, pub, 't', payload=bytes([i]), qos=1, packet_id=i + 1)

        assert len(sub.publishes()) == 2, (
            f'sent {len(sub.publishes())} unacknowledged PUBLISH packets to a '
            f'client that allowed 2')

    async def test_an_ack_releases_exactly_one_queued_message(self):
        broker = BrokerActor()
        sub, pub = RecordingConn(), RecordingConn()
        await _attach(broker, sub, client_id='sub',
                      properties={'receive_maximum': 1})
        await _attach(broker, pub, client_id='pub')
        await _subscribe(broker, sub, 't', qos=1)

        for i in range(3):
            await _publish(broker, pub, 't', payload=bytes([i]), qos=1, packet_id=i + 1)
        assert len(sub.publishes()) == 1

        first = sub.publishes()[0]
        await broker._handle(ClientPuback(packet_id=first.packet_id, sender=sub))
        assert len(sub.publishes()) == 2, 'the ACK released no queued message'
        assert sub.publishes()[1].payload == b'\x01', 'released out of order'

    async def test_qos0_is_not_flow_controlled(self):
        """§4.9 bounds QoS>0 only; throttling QoS 0 would invent a rule."""
        broker = BrokerActor()
        sub, pub = RecordingConn(), RecordingConn()
        await _attach(broker, sub, client_id='sub',
                      properties={'receive_maximum': 1})
        await _attach(broker, pub, client_id='pub')
        await _subscribe(broker, sub, 't', qos=0)

        for _ in range(5):
            await _publish(broker, pub, 't', qos=0)

        assert len(sub.publishes()) == 5

    async def test_backlog_is_bounded_and_refuses_the_newest(self, caplog):
        """The queue that holds the excess is itself a total that needs a cap."""
        broker = BrokerActor(max_queued=3)
        sub, pub = RecordingConn(), RecordingConn()
        await _attach(broker, sub, client_id='sub',
                      properties={'receive_maximum': 1})
        await _attach(broker, pub, client_id='pub')
        await _subscribe(broker, sub, 't', qos=1)

        with caplog.at_level('WARNING', logger='blackbull.caps'):
            for i in range(10):
                await _publish(broker, pub, 't', payload=bytes([i]), qos=1,
                               packet_id=i + 1)

        session = broker._sessions['sub']
        assert len(session['outbound_queue']) == 3, (
            f"backlog grew to {len(session['outbound_queue'])} with a cap of 3")
        # The oldest survive: a client is owed what it was promised first.
        assert [bytes(h.publish.payload) for h in session['outbound_queue']] == \
            [b'\x01', b'\x02', b'\x03']

        hits = [r for r in caplog.records
                if getattr(r, 'cap', None) == 'mqtt_max_queued_messages']
        assert hits, 'a dropped message that nobody can observe'
        assert hits[0].limit == 3


# ===========================================================================
# G3c — the retained store is permanent, so it needs a total
# ===========================================================================

class TestRetainedBound:
    async def test_new_topic_is_refused_at_the_cap(self, caplog):
        broker = BrokerActor(max_retained=2)
        pub = RecordingConn()
        await _attach(broker, pub)

        with caplog.at_level('WARNING', logger='blackbull.caps'):
            for i in range(4):
                await _publish(broker, pub, f't{i}', payload=b'v', retain=True)

        assert len(broker._retained) == 2, (
            f'retained store grew to {len(broker._retained)} with a cap of 2')
        assert set(broker._retained) == {'t0', 't1'}

        hits = [r for r in caplog.records
                if getattr(r, 'cap', None) == 'mqtt_max_retained']
        assert hits and hits[0].limit == 2

    async def test_updating_an_existing_topic_still_works_at_the_cap(self):
        """A client must never be locked out of correcting its own state."""
        broker = BrokerActor(max_retained=2)
        pub = RecordingConn()
        await _attach(broker, pub)
        await _publish(broker, pub, 't0', payload=b'first', retain=True)
        await _publish(broker, pub, 't1', payload=b'other', retain=True)

        await _publish(broker, pub, 't0', payload=b'second', retain=True)

        assert broker._retained['t0'].payload == b'second'
        assert len(broker._retained) == 2

    async def test_deleting_still_works_at_the_cap(self):
        """§3.3.2.3 — a zero-length retained payload deletes, and deletion is
        the one operation that must never be refused for lack of room."""
        broker = BrokerActor(max_retained=2)
        pub = RecordingConn()
        await _attach(broker, pub)
        await _publish(broker, pub, 't0', payload=b'v', retain=True)
        await _publish(broker, pub, 't1', payload=b'v', retain=True)

        await _publish(broker, pub, 't0', payload=b'', retain=True)

        assert 't0' not in broker._retained
        await _publish(broker, pub, 't2', payload=b'v', retain=True)
        assert 't2' in broker._retained, 'freed room was not reusable'

    async def test_zero_disables_the_cap(self):
        broker = BrokerActor(max_retained=0)
        pub = RecordingConn()
        await _attach(broker, pub)
        for i in range(50):
            await _publish(broker, pub, f't{i}', payload=b'v', retain=True)
        assert len(broker._retained) == 50


# ===========================================================================
# The framer's resync cost (audit ⚠, not a numbered gap)
# ===========================================================================

class TestResyncCost:
    async def test_junk_prefix_costs_linear_work(self):
        """Dropping one byte and re-decoding from the start is O(n²).

        Asserted as a decode count rather than a wall clock: the quadratic
        version calls the decoder once per dropped byte, so the counter
        separates the two implementations by three orders of magnitude on
        this input while staying immune to how fast the machine is.
        """
        import blackbull.mqtt.connection as conn_mod

        calls = 0
        real = conn_mod.decode_packet

        def counting(data):
            nonlocal calls
            calls += 1
            return real(data)

        junk = bytes([0x00]) * 4000          # type 0 — reserved, never valid
        good = encode_packet(MQTTPublish(topic='t', payload=b'x', qos=0))
        framer = PacketFramer()
        framer.feed(junk + good)

        conn_mod.decode_packet = counting
        try:
            decoded = list(framer)
        finally:
            conn_mod.decode_packet = real

        assert len(decoded) == 1 and decoded[0].topic == 't', (
            'resync must still find the packet after the junk')
        assert calls <= 40, (
            f'{calls} decode attempts for a 4000-byte junk prefix — the framer '
            f're-decodes per dropped byte, which is quadratic in the junk length'
        )

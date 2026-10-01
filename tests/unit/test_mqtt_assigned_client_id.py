"""§3.2.2.3.7 [MQTT-3.2.2-16] — zero-length Client Identifier assignment."""

from __future__ import annotations

import asyncio

import pytest

from blackbull.actor import Actor
from blackbull.mqtt.broker import BrokerActor, Attach, Detach, Send

from blackbull.mqtt.messages import (
    MQTTConnect,
    MQTTConnack,
    MQTTDisconnect,
    ReasonCode,
)

pytestmark = pytest.mark.asyncio


class RecordingConn(Actor):
    def __init__(self) -> None:
        super().__init__()
        self.outbox = []

    async def send(self, msg) -> None:
        self.outbox.append(msg)

    def packets(self):
        return [m.packet for m in self.outbox if isinstance(m, Send)]


def _connect(client_id='', clean_start=True, **kw):
    return MQTTConnect(client_id=client_id, clean_start=clean_start,
                       keep_alive=kw.pop('keep_alive', 60), **kw)


async def _attach(broker, conn, **kw):
    await broker._handle(Attach(connect=_connect(**kw), sender=conn))


async def _detach(broker, conn):
    await broker._handle(Detach(graceful=True, sender=conn))


def _assigned(connack):
    return (connack.properties or {}).get('assigned_client_identifier')


class TestAssignedIdentifier:
    async def test_an_empty_client_id_gets_an_identifier_in_the_connack(self):
        broker, conn = BrokerActor(), RecordingConn()
        await _attach(broker, conn)
        ack = conn.packets()[0]
        assert isinstance(ack, MQTTConnack)
        assert ack.reason_code == ReasonCode.SUCCESS
        assert _assigned(ack)

    async def test_the_assigned_identifier_takes_over_no_existing_connection(self):
        broker = BrokerActor()
        holder, empty = RecordingConn(), RecordingConn()
        await _attach(broker, holder, client_id='auto-1')
        await _attach(broker, empty)
        assert _assigned(empty.packets()[0]) != 'auto-1'
        assert all(not isinstance(p, MQTTDisconnect) for p in holder.packets())

    async def test_the_assigned_identifier_skips_a_live_session(self):
        broker, live = BrokerActor(), RecordingConn()
        await _attach(broker, live, client_id='auto-1')
        fresh = RecordingConn()
        await _attach(broker, fresh)
        assigned = _assigned(fresh.packets()[0])
        assert assigned and assigned != 'auto-1'

    async def test_the_assigned_identifier_skips_an_offline_session(self):
        broker, first = BrokerActor(), RecordingConn()
        await _attach(broker, first, client_id='auto-1', clean_start=False,
                      properties={'session_expiry_interval': 3600})
        await _detach(broker, first)
        fresh = RecordingConn()
        await _attach(broker, fresh)
        assigned = _assigned(fresh.packets()[0])
        assert assigned and assigned != 'auto-1'

    async def test_two_empty_ids_never_collide(self):
        broker = BrokerActor()
        a, b = RecordingConn(), RecordingConn()
        await _attach(broker, a)
        await _attach(broker, b)
        first, second = _assigned(a.packets()[0]), _assigned(b.packets()[0])
        assert first and second and first != second

    async def test_two_queued_empty_id_connects_both_get_identifiers(self):
        broker = BrokerActor()
        a, b = RecordingConn(), RecordingConn()
        await broker.send(Attach(connect=_connect(), sender=a))
        await broker.send(Attach(connect=_connect(), sender=b))
        drained = asyncio.Event()
        await broker.send(Detach(graceful=True, sender=RecordingConn(),
                                 processed=drained))
        task = asyncio.create_task(broker.run())
        try:
            await asyncio.wait_for(drained.wait(), 5)
        finally:
            task.cancel()
        first, second = _assigned(a.packets()[0]), _assigned(b.packets()[0])
        assert first and second and first != second
        assert not any(isinstance(p, MQTTDisconnect)
                       for p in a.packets() + b.packets())

    async def test_an_empty_id_with_clean_start_zero_still_starts_fresh(self):
        broker, conn = BrokerActor(), RecordingConn()
        await _attach(broker, conn, clean_start=False)
        ack = conn.packets()[0]
        assert ack.reason_code == ReasonCode.SUCCESS
        assert ack.session_present is False
        assert _assigned(ack)

    async def test_the_client_can_reconnect_with_the_assigned_identifier(self):
        broker, conn = BrokerActor(), RecordingConn()
        await _attach(broker, conn,
                      properties={'session_expiry_interval': 3600})
        assigned = _assigned(conn.packets()[0])
        await _detach(broker, conn)
        again = RecordingConn()
        await _attach(broker, again, client_id=assigned, clean_start=False)
        assert again.packets()[0].session_present is True

    async def test_the_default_expiry_session_does_not_outlive_the_connection(self):
        broker, conn = BrokerActor(), RecordingConn()
        await _attach(broker, conn)
        assigned = _assigned(conn.packets()[0])
        await _detach(broker, conn)
        assert broker._sessions == {}
        again = RecordingConn()
        await _attach(broker, again, client_id=assigned, clean_start=False)
        assert again.packets()[0].session_present is False

    async def test_a_clean_start_empty_id_starts_a_fresh_session(self):
        broker, conn = BrokerActor(), RecordingConn()
        await _attach(broker, conn,
                      properties={'session_expiry_interval': 3600})
        assigned = _assigned(conn.packets()[0])
        await _detach(broker, conn)
        again = RecordingConn()
        await _attach(broker, again, client_id=assigned, clean_start=True)
        assert again.packets()[0].session_present is False


class TestQuota:
    async def test_a_full_table_leaves_nothing_behind_for_an_empty_id(self):
        broker = BrokerActor()
        broker._max_sessions = 1
        taken = RecordingConn()
        await _attach(broker, taken, client_id='c1')
        refused = RecordingConn()
        await _attach(broker, refused)
        ack = refused.packets()[0]
        assert isinstance(ack, MQTTConnack)
        assert ack.reason_code == ReasonCode.QUOTA_EXCEEDED
        assert set(broker._sessions) == {'c1'}

    async def test_a_free_slot_admits_an_empty_id_at_the_cap(self):
        broker = BrokerActor()
        broker._max_sessions = 2
        await _attach(broker, RecordingConn(), client_id='c1')
        conn = RecordingConn()
        await _attach(broker, conn)
        ack = conn.packets()[0]
        assert ack.reason_code == ReasonCode.SUCCESS
        assert set(broker._sessions) == {'c1', _assigned(ack)}


class TestExplicitIdentifiersAreUnchanged:
    async def test_an_explicit_identifier_still_takes_over(self):
        broker = BrokerActor()
        first, second = RecordingConn(), RecordingConn()
        await _attach(broker, first, client_id='c1')
        await _attach(broker, second, client_id='c1')
        assert any(isinstance(p, MQTTDisconnect)
                   and p.reason_code == ReasonCode.SESSION_TAKEN_OVER
                   for p in first.packets())
        assert second.packets()[0].reason_code == ReasonCode.SUCCESS

    async def test_an_auto_looking_explicit_identifier_is_an_ordinary_name(self):
        broker = BrokerActor()
        first, second = RecordingConn(), RecordingConn()
        await _attach(broker, first, client_id='auto-2')
        await _attach(broker, second, client_id='auto-2')
        assert any(isinstance(p, MQTTDisconnect) for p in first.packets())
        assert _assigned(second.packets()[0]) is None

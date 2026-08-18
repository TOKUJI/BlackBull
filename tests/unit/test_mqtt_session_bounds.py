"""MQTT session state — the row whose three triad columns were all empty.

``docs/about/security-model.md`` published this row as bounded on all
three axes.  None of the three held:

* **one unit** — a session's subscription list was appended to without
  limit, so one connected client could grow the broker's memory (and the
  per-PUBLISH routing walk) without ever opening a second connection.
* **total** — ``BrokerActor._sessions`` had no cap.  §3.1.2.11.2 makes
  ``session_expiry_interval = 0xFFFFFFFF`` mean *never expires*, so a
  peer cycling distinct Client Identifiers could pin one dict entry per
  identifier, legitimately and permanently.
* **time** — ``_expiry`` was written on CONNECT and read in exactly one
  place, a ``<= 0`` test at detach.  Nothing ever removed a session whose
  interval had elapsed.

The tests drive the broker and assert what it emits and what state it
refuses to grow, never codec round-trips.
"""
import asyncio

import pytest

from blackbull.actor import Actor
from blackbull.mqtt.broker import (
    Attach, BrokerActor, ClientSubscribe, Close, Detach, Send,
)
from blackbull.mqtt.messages import (
    MQTTConnack, MQTTConnect, MQTTPublish, MQTTSubscribe, ReasonCode,
)

pytestmark = pytest.mark.asyncio

#: §3.1.2.11.2 — the one interval that means "this session never expires".
_NEVER = 0xFFFFFFFF


class RecordingConn(Actor):
    """A fake connection actor that records what the broker sends it."""

    def __init__(self) -> None:
        super().__init__()
        self.outbox = []

    async def send(self, msg) -> None:  # override: record instead of enqueue
        self.outbox.append(msg)

    def packets(self) -> list:
        return [m.packet for m in self.outbox if isinstance(m, Send)]

    def connack(self) -> MQTTConnack:
        acks = [p for p in self.packets() if isinstance(p, MQTTConnack)]
        assert acks, 'no CONNACK'
        return acks[0]

    def closes(self) -> list:
        return [m for m in self.outbox if isinstance(m, Close)]


async def _attach(broker, conn, *, client_id='c1', clean_start=True,
                  expiry=None, **kw):
    props = dict(kw.pop('properties', {}))
    if expiry is not None:
        props['session_expiry_interval'] = expiry
    await broker._handle(Attach(
        connect=MQTTConnect(client_id=client_id, clean_start=clean_start,
                            keep_alive=60, properties=props, **kw),
        sender=conn))


async def _subscribe(broker, conn, topic='t', qos=0):
    await broker._handle(ClientSubscribe(
        subscribe=MQTTSubscribe(packet_id=1, subscriptions=[(topic, qos)]),
        sender=conn))
    return conn


async def _detach(broker, conn, *, graceful=True, expiry=None):
    await broker._handle(Detach(graceful=graceful, sender=conn,
                                session_expiry_interval=expiry))


# ===========================================================================
# one unit — a single session's subscription list
# ===========================================================================

class TestSubscriptionBound:
    async def test_a_new_filter_is_refused_at_the_cap(self, caplog):
        """One client, one connection, unbounded memory — until now."""
        broker = BrokerActor(max_subscriptions=3)
        conn = RecordingConn()
        await _attach(broker, conn)

        with caplog.at_level('WARNING', logger='blackbull.caps'):
            for i in range(6):
                await _subscribe(broker, conn, f't/{i}')

        session = broker._sessions['c1']
        assert len(session['subscriptions']) == 3, (
            f"subscriptions grew to {len(session['subscriptions'])} "
            f'with a cap of 3')

        hits = [r for r in caplog.records
                if getattr(r, 'cap', None) == 'mqtt_max_subscriptions']
        assert hits, 'a refused subscription that nobody can observe'
        assert hits[0].limit == 3
        assert hits[0].protocol == 'mqtt'

    async def test_the_refusal_reaches_the_client_in_the_suback(self):
        """§3.9.3 — 0x97 is a valid SUBACK reason code; silence is not."""
        broker = BrokerActor(max_subscriptions=1)
        conn = RecordingConn()
        await _attach(broker, conn)
        await _subscribe(broker, conn, 'a')
        await _subscribe(broker, conn, 'b')

        subacks = [p for p in conn.packets() if hasattr(p, 'reason_codes')]
        assert subacks[-1].reason_codes == [ReasonCode.QUOTA_EXCEEDED], (
            'a client told its subscription succeeded will wait forever '
            'for messages that are never routed to it')

    async def test_replacing_an_existing_filter_still_works_at_the_cap(self):
        """§3.8.4 — a re-SUBSCRIBE replaces; it occupies no new slot.

        Locking a client out of changing the QoS of a subscription it
        already holds would leave it worse off than one that never
        subscribed, and buys no memory back.
        """
        broker = BrokerActor(max_subscriptions=2)
        conn = RecordingConn()
        await _attach(broker, conn)
        await _subscribe(broker, conn, 'a', qos=0)
        await _subscribe(broker, conn, 'b', qos=0)

        await _subscribe(broker, conn, 'a', qos=1)

        session = broker._sessions['c1']
        assert len(session['subscriptions']) == 2
        assert dict((f, q) for f, q, _ in session['subscriptions'])['a'] == 1

    async def test_zero_disables_the_cap(self):
        broker = BrokerActor(max_subscriptions=0)
        conn = RecordingConn()
        await _attach(broker, conn)
        for i in range(20):
            await _subscribe(broker, conn, f't/{i}')
        assert len(broker._sessions['c1']['subscriptions']) == 20


# ===========================================================================
# total — the number of sessions
# ===========================================================================

class TestSessionTableBound:
    async def test_a_new_client_is_refused_at_the_cap(self, caplog):
        """The attack the audit's own grid said was answered.

        ``0xFFFFFFFF`` is not an abuse of the protocol — §3.1.2.11.2
        defines it as "does not expire".  Without a total, honouring it
        is unbounded memory.
        """
        broker = BrokerActor(max_sessions=3)
        with caplog.at_level('WARNING', logger='blackbull.caps'):
            for i in range(6):
                conn = RecordingConn()
                await _attach(broker, conn, client_id=f'c{i}', expiry=_NEVER)
                await _detach(broker, conn)

        assert len(broker._sessions) == 3, (
            f'session table grew to {len(broker._sessions)} with a cap of 3')

        hits = [r for r in caplog.records
                if getattr(r, 'cap', None) == 'mqtt_max_sessions']
        assert hits, 'a refused session that nobody can observe'
        assert hits[0].limit == 3

    async def test_the_refusal_reaches_the_client_as_connack_0x97(self):
        broker = BrokerActor(max_sessions=1)
        first = RecordingConn()
        await _attach(broker, first, client_id='a', expiry=_NEVER)
        await _detach(broker, first)

        second = RecordingConn()
        await _attach(broker, second, client_id='b', expiry=_NEVER)

        assert second.connack().reason_code == ReasonCode.QUOTA_EXCEEDED
        assert second.connack().session_present is False
        assert second.closes(), 'refused the session but left the connection up'

    async def test_a_refused_client_leaves_no_state_behind(self):
        """A rejection that half-registers the client is a second leak."""
        broker = BrokerActor(max_sessions=1)
        first = RecordingConn()
        await _attach(broker, first, client_id='a', expiry=_NEVER)
        await _detach(broker, first)

        second = RecordingConn()
        await _attach(broker, second, client_id='b', expiry=_NEVER,
                      will_topic='w', will_payload=b'x')

        assert 'b' not in broker._sessions
        assert 'b' not in broker._clients
        assert 'b' not in broker._wills
        assert id(second) not in broker._client_by_conn

    async def test_a_resuming_client_is_admitted_at_the_cap(self):
        """It occupies a slot already counted — refusing it frees nothing."""
        broker = BrokerActor(max_sessions=2)
        for cid in ('a', 'b'):
            conn = RecordingConn()
            await _attach(broker, conn, client_id=cid, expiry=_NEVER)
            await _detach(broker, conn)

        again = RecordingConn()
        await _attach(broker, again, client_id='a', clean_start=False,
                      expiry=_NEVER)

        assert again.connack().reason_code == ReasonCode.SUCCESS
        assert again.connack().session_present is True

    async def test_an_expired_session_frees_its_slot(self):
        """The cap binds live state, not state that should already be gone."""
        broker = BrokerActor(max_sessions=1)
        gone = RecordingConn()
        await _attach(broker, gone, client_id='old', expiry=1)
        await _detach(broker, gone)
        # Reach past the wall clock rather than sleeping for it.
        broker._sessions['old']['_expires_at'] = \
            asyncio.get_running_loop().time() - 0.001

        fresh = RecordingConn()
        await _attach(broker, fresh, client_id='new', expiry=_NEVER)

        assert fresh.connack().reason_code == ReasonCode.SUCCESS
        assert 'old' not in broker._sessions

    async def test_zero_disables_the_cap(self):
        broker = BrokerActor(max_sessions=0)
        for i in range(12):
            conn = RecordingConn()
            await _attach(broker, conn, client_id=f'c{i}', expiry=_NEVER)
            await _detach(broker, conn)
        assert len(broker._sessions) == 12


# ===========================================================================
# time — how long a detached session may persist
# ===========================================================================

class TestSessionExpiry:
    async def test_an_elapsed_session_is_removed_with_its_subscriptions(self):
        broker = BrokerActor()
        conn = RecordingConn()
        await _attach(broker, conn, client_id='c', expiry=1)
        await _subscribe(broker, conn, 'sport/#')
        await _detach(broker, conn)
        assert 'c' in broker._sessions, 'removed before its interval elapsed'

        broker._sessions['c']['_expires_at'] = \
            asyncio.get_running_loop().time() - 0.001
        broker._sweep_expired()

        assert 'c' not in broker._sessions

    async def test_the_sweep_actually_fires_on_its_own(self):
        """The timer, not a caller, is what makes the time column real."""
        broker = BrokerActor()
        task = asyncio.create_task(broker.run())
        try:
            conn = RecordingConn()
            await broker.send(Attach(
                connect=MQTTConnect(client_id='c', clean_start=True,
                                    keep_alive=60,
                                    properties={'session_expiry_interval': 1}),
                sender=conn))
            await broker.send(Detach(graceful=True, sender=conn))
            await asyncio.sleep(0)
            assert 'c' in broker._sessions
            # Bring the deadline forward and re-arm the way a detach would.
            broker._sessions['c']['_expires_at'] = \
                asyncio.get_running_loop().time() + 0.05
            broker._arm_expiry_timer()

            await asyncio.sleep(0.25)
            assert 'c' not in broker._sessions, (
                'the expiry timer never fired — the time column is still '
                'a claim, not a bound')
        finally:
            task.cancel()

    async def test_no_timer_is_armed_when_nothing_can_expire(self):
        """An MQTT extension with no expiring session pays no idle wakeup."""
        broker = BrokerActor()
        conn = RecordingConn()
        await _attach(broker, conn, client_id='c', expiry=_NEVER)
        await _detach(broker, conn)
        assert broker._expiry_timer is None

    async def test_close_disarms_the_timer(self):
        """A cancelled broker must not leave a handle armed on the loop.

        The callback holds a bound method of the broker, so an armed timer
        outlives the actor it would post to — it keeps the broker alive in
        the loop's timer heap and then fires into an inbox nobody reads.
        """
        broker = BrokerActor()
        conn = RecordingConn()
        await _attach(broker, conn, client_id='c', expiry=60)
        await _detach(broker, conn)
        assert broker._expiry_timer is not None, 'nothing was armed to disarm'

        broker.close()

        assert broker._expiry_timer is None

    async def test_expiry_zero_still_ends_the_session_at_detach(self):
        broker = BrokerActor()
        conn = RecordingConn()
        await _attach(broker, conn, client_id='c', expiry=0)
        await _detach(broker, conn)
        assert 'c' not in broker._sessions

    async def test_a_reconnect_shortens_the_interval_it_declares(self):
        """§3.1.2.11.2 — the CONNECT sets the interval; it does not raise it.

        Taking ``max(old, new)`` meant one connection at 0xFFFFFFFF pinned
        the session for the process's lifetime, and the client could never
        take it back.
        """
        broker = BrokerActor()
        first = RecordingConn()
        await _attach(broker, first, client_id='c', expiry=_NEVER)
        await _detach(broker, first)

        second = RecordingConn()
        await _attach(broker, second, client_id='c', clean_start=False, expiry=0)
        await _detach(broker, second)

        assert 'c' not in broker._sessions, (
            'a client that asked for its session to end on disconnect '
            'still has one')

    async def test_an_expired_session_does_not_come_back_as_session_present(self):
        """An expired session that replays is worse than one that is gone.

        ``session_present=True`` tells the client its subscriptions and
        its unacknowledged messages survived; replaying them from a
        session the broker had promised to discard resurrects deliveries
        the client already accounted for.
        """
        broker = BrokerActor()
        conn = RecordingConn()
        await _attach(broker, conn, client_id='c', expiry=1)
        await _subscribe(broker, conn, 't', qos=1)
        broker._sessions['c']['pending_qos1_out'][7] = MQTTPublish(
            topic='t', payload=b'stale', qos=1, packet_id=7)
        await _detach(broker, conn)
        broker._sessions['c']['_expires_at'] = \
            asyncio.get_running_loop().time() - 0.001

        again = RecordingConn()
        await _attach(broker, again, client_id='c', clean_start=False, expiry=1)

        assert again.connack().session_present is False
        assert not [p for p in again.packets() if isinstance(p, MQTTPublish)], (
            'replayed a message from an expired session')

    async def test_disconnect_may_shorten_the_interval(self):
        """§3.14.2.2.2 — DISCONNECT carries a Session Expiry Interval too."""
        broker = BrokerActor()
        conn = RecordingConn()
        await _attach(broker, conn, client_id='c', expiry=_NEVER)
        await _detach(broker, conn, expiry=0)
        assert 'c' not in broker._sessions

    async def test_disconnect_cannot_resurrect_an_ephemeral_session(self):
        """§3.14.2.2.2 — 0 in CONNECT then non-zero in DISCONNECT is a
        Protocol Error, so the session still ends."""
        broker = BrokerActor()
        conn = RecordingConn()
        await _attach(broker, conn, client_id='c', expiry=0)
        await _detach(broker, conn, expiry=_NEVER)
        assert 'c' not in broker._sessions

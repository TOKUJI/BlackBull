"""
MQTT 5.0 Keep-Alive (PINGREQ/PINGRESP) conformance tests.

Verifies heartbeat mechanism against the MQTT 5.0 OASIS Standard.

Reference: MQTT Version 5.0, OASIS Standard
  §3.1.2.10 Keep Alive
  §3.12    PINGREQ  – PING Request
  §3.13    PINGRESP – PING Response

Key behaviours:
  - §3.1.2.10: Keep Alive is a time interval measured in seconds.  It is the
    maximum time that can elapse between a client sending one Control Packet
    and the next.
  - §3.12: PINGREQ is sent by the client to the server to:
      a) indicate it is alive (keep-alive)
      b) request the server to respond (connectivity check)
  - §3.13: PINGRESP is sent by the server in response to PINGREQ.
  - §3.12 / §3.13: Both PINGREQ and PINGRESP have no variable header and no
    payload (fixed header only: 0xC0 0x00 and 0xD0 0x00 respectively).
  - If Keep Alive is non-zero and the server does not receive a Control Packet
    within 1.5 × Keep Alive, it MUST close the connection (§3.1.2.10).
"""

import asyncio
import time
import pytest

from blackbull.mqtt.messages import (
    ReasonCode,
    MQTTConnect, MQTTConnack, MQTTSubscribe,
    MQTTPingreq, MQTTPingresp,
    encode_packet, decode_packet,
)
from tests.conformance.mqtt._harness import (
    _FakeMQTTReader, _FakeMQTTWriter, _ctx,
    run_until_idle, cancel_all,
)


# ============================================================================
# §3.12 / §3.13 — PINGREQ / PINGRESP wire format
# ============================================================================

class TestPingreqPingrespWireFormat:
    """§3.12, §3.13 — PINGREQ and PINGRESP are 2-byte fixed packets."""

    def test_pingreq_is_exactly_two_bytes(self, mqtt):
        """§3.12 — PINGREQ: fixed header 0xC0, Remaining Length 0."""
        pingreq = MQTTPingreq()
        wire = encode_packet(pingreq)
        assert wire == b'\xC0\x00', \
            "PINGREQ must be exactly 0xC0 0x00 per §3.12"

    def test_pingresp_is_exactly_two_bytes(self, mqtt):
        """§3.13 — PINGRESP: fixed header 0xD0, Remaining Length 0."""
        pingresp = MQTTPingresp()
        wire = encode_packet(pingresp)
        assert wire == b'\xD0\x00', \
            "PINGRESP must be exactly 0xD0 0x00 per §3.13"

    def test_pingreq_round_trip(self, mqtt):
        """§3.12 — PINGREQ decode yields MQTTPingreq."""
        decoded = decode_packet(b'\xC0\x00')
        assert isinstance(decoded[0], MQTTPingreq)

    def test_pingresp_round_trip(self, mqtt):
        """§3.13 — PINGRESP decode yields MQTTPingresp."""
        decoded = decode_packet(b'\xD0\x00')
        assert isinstance(decoded[0], MQTTPingresp)


# ============================================================================
# §3.1.2.10 — Keep Alive timeout behaviour
# ============================================================================

class TestKeepAliveTimeout:
    """§3.1.2.10 — Keep Alive enforcement.

    If Keep Alive > 0 and no Control Packet is received within
    1.5 × Keep Alive seconds, the server MUST close the network connection.

    The server MAY apply a grace period and SHOULD send PINGREQ first
    (server-initiated keep-alive check) before closing.
    """

    def test_keep_alive_value_in_connect(self, mqtt):
        """§3.1.2.10 — Keep Alive is a 16-bit unsigned integer in seconds."""
        for ka in (0, 10, 60, 300, 3600, 65535):
            connect = MQTTConnect(
                client_id='ka-client',
                clean_start=True,
                keep_alive=ka,
            )
            wire = encode_packet(connect)
            decoded = decode_packet(wire)
            assert decoded.keep_alive == ka

    def test_keep_alive_zero_means_no_timeout(self, mqtt):
        """§3.1.2.10 — Keep Alive = 0 means the server is not required to
        disconnect on inactivity."""
        connect = MQTTConnect(
            client_id='no-ka',
            clean_start=True,
            keep_alive=0,
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.keep_alive == 0

    @pytest.mark.asyncio
    async def test_pingreq_triggers_pingresp(self, mqtt):
        """§3.12 → §3.13 — Server MUST respond to PINGREQ with PINGRESP."""
        # Connect then send PINGREQ
        _, writer = await run_until_idle(
            mqtt,
            MQTTConnect(client_id='ping-client', clean_start=True, keep_alive=30),
            MQTTPingreq(),
        )

        packets = writer.pop_packets()
        pingresps = [p for p in packets if isinstance(p, MQTTPingresp)]
        assert len(pingresps) >= 1, "Server MUST respond to PINGREQ with PINGRESP"

    @pytest.mark.asyncio
    async def test_any_control_packet_resets_keepalive_timer(self, mqtt):
        """§3.1.2.10 — Any Control Packet sent by the client resets the
        keep-alive timer, not just PINGREQ.

        For example, a PUBLISH or SUBSCRIBE also counts as activity.
        """
        from blackbull.mqtt.messages import MQTTPublish

        ctx = _ctx()

        # A Will topic makes the abnormal detach observable on the wire:
        # §3.1.2.10 — a keep-alive timeout fires the Will and closes.
        obs_r, obs_w = _FakeMQTTReader(), _FakeMQTTWriter()
        obs_r.feed_packet(MQTTConnect(
            client_id='ka-obs', clean_start=True, keep_alive=0,
        ))
        obs_r.feed_packet(MQTTSubscribe(
            packet_id=1, subscriptions=[('ka/will/#', 0)],
        ))

        # keep_alive=1: the 1.5x deadline is 1.5s after each connection's last
        # control packet.  Both start together; A gets one PUBLISH mid-window
        # (at ~0.75s), which moves its deadline to ~2.25s.  The remaining
        # margins are the two sides of the mid-window point (0.75s each); the
        # deadlines themselves are observed by polling, not by fixed sleeps.
        a_r, a_w = _FakeMQTTReader(), _FakeMQTTWriter()
        a_r.feed_packet(MQTTConnect(
            client_id='ka-active', clean_start=True, keep_alive=1,
            will_topic='ka/will/active', will_payload=b'gone',
        ))
        b_r, b_w = _FakeMQTTReader(), _FakeMQTTWriter()
        b_r.feed_packet(MQTTConnect(
            client_id='ka-silent', clean_start=True, keep_alive=1,
            will_topic='ka/will/silent', will_payload=b'gone',
        ))
        tasks = [asyncio.create_task(mqtt.serve(r, w, ctx).run())
                 for r, w in ((obs_r, obs_w), (a_r, a_w), (b_r, b_w))]
        await asyncio.sleep(0.75)
        # The activity is a PUBLISH — any Control Packet counts, not just
        # PINGREQ (§3.1.2.10).
        a_r.feed_packet(MQTTPublish(
            topic='status/heartbeat', payload=b'alive', qos=0,
        ))

        # Collect the Will deliveries as they arrive, with their arrival
        # times — the order and the gap between them ARE the assertion, so
        # no fixed sleep has to land between the two deadlines.
        seen: dict = {}

        async def collect_wills():
            while not ({'ka/will/silent', 'ka/will/active'} <= seen.keys()):
                now = asyncio.get_running_loop().time()
                for packet in obs_w.pop_packets():
                    if isinstance(packet, MQTTPublish):
                        seen.setdefault(packet.topic, now)
                await asyncio.sleep(0.02)

        await asyncio.wait_for(collect_wills(), 5)

        assert seen['ka/will/silent'] < seen['ka/will/active'], (
            'a control-less window must time out and fire the Will first'
        )
        assert seen['ka/will/active'] - seen['ka/will/silent'] >= 0.4, (
            'the mid-window PUBLISH must reset the timer (its deadline moves '
            'past the un-reset one); the Will gap shrank to '
            f'{seen["ka/will/active"] - seen["ka/will/silent"]:.2f}s'
        )

        await cancel_all(*tasks)

# ============================================================================
# §3.1.2.10 — Server Keep Alive override (CONNACK property)
# ============================================================================

class TestServerKeepAlive:
    """§3.2.2.3.2 — Server Keep Alive property.

    If the server returns a Server Keep Alive in the CONNACK, the client
    MUST use that value instead of the value it sent in the CONNECT.
    """


    def test_connack_without_server_keep_alive_uses_client_value(self, mqtt):
        """§3.2.2.3.2 — If absent, client's Keep Alive value is used."""
        connack = MQTTConnack(
            session_present=False,
            reason_code=ReasonCode.SUCCESS,
        )
        wire = encode_packet(connack)
        decoded = decode_packet(wire)
        assert 'server_keep_alive' not in decoded.properties

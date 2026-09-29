"""Architecture tests for MQTT protocol detection on the Non-ASGI bridge.

The procedural ``MQTTActor`` was replaced by the ``BrokerActor`` +
``MQTT5Actor`` topology; the actor-level behaviour those tests used to
cover now lives in ``tests/unit/test_mqtt_broker_actor.py`` and
``tests/unit/test_mqtt_connection_actor.py``, with the full wire contract in
``tests/conformance/mqtt/``.  What remains uniquely here is shared-port protocol
detection.
"""

import pytest


class TestMQTTProtocolDetection:
    """MQTT protocol detection via the Non-ASGI bridge."""

    @pytest.mark.parametrize('data,expected', [
        pytest.param(b'\x10\x00\x00\x04MQTT', True, id='recognizes-connect-byte'),
        pytest.param(b'GET / HTTP/1.1\r\n', False, id='rejects-http-first-line'),
        pytest.param(b'PRI * HTTP/2.0\r\n', False, id='rejects-http2-preface'),
    ])
    def test_mqtt_detector_recognizes_connect_byte(self, data, expected):
        """The MQTTProtocolDetector classifies the prologue byte sequence."""
        from blackbull.mqtt import MQTTProtocolDetector
        detector = MQTTProtocolDetector()
        assert detector.detect(data, None) is expected

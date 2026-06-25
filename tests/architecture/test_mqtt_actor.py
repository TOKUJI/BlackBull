"""Architecture tests for MQTT protocol detection on the Non-ASGI bridge.

Sprint 53 replaced the procedural ``MQTTActor`` with the ``BrokerActor`` +
``MQTT5Actor`` topology; the actor-level behaviour those tests used to
cover now lives in ``tests/unit/test_mqtt_broker_actor.py`` and
``tests/unit/test_mqtt_connection_actor.py``, with the full wire contract in
``tests/conformance/mqtt/``.  What remains uniquely here is shared-port protocol
detection.
"""


class TestMQTTProtocolDetection:
    """MQTT protocol detection via the Non-ASGI bridge."""

    def test_mqtt_detector_recognizes_connect_byte(self):
        """The MQTTProtocolDetector recognizes the MQTT CONNECT first byte (0x10)."""
        from blackbull.mqtt import MQTTProtocolDetector
        detector = MQTTProtocolDetector()
        # First byte of CONNECT is 0x10
        assert detector.detect(b'\x10\x00\x00\x04MQTT', None) is True

    def test_mqtt_detector_rejects_http_first_line(self):
        """The MQTTProtocolDetector rejects HTTP request lines."""
        from blackbull.mqtt import MQTTProtocolDetector
        detector = MQTTProtocolDetector()
        assert detector.detect(b'GET / HTTP/1.1\r\n', None) is False

    def test_mqtt_detector_rejects_http2_preface(self):
        """The MQTTProtocolDetector rejects the HTTP/2 preface."""
        from blackbull.mqtt import MQTTProtocolDetector
        detector = MQTTProtocolDetector()
        assert detector.detect(b'PRI * HTTP/2.0\r\n', None) is False

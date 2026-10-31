"""Caps on how many bytes one unit, or one total, may hold."""
import pytest

from blackbull.protocol.frame_types import FrameTypes
from blackbull.server.ws_codec import WSOpcode
from tests.conformance.http2._harness import _make_h2_frame

from blackbull.mqtt.messages import MQTTDisconnect, MQTTPublish

from ._drive import (client_frame, client_h2, client_http1, client_ws, connect, h2,
                     h2_data, h2_request, h2_response, head, http1, mqtt,
                     mqtt_subscriber_fate,
                     server_frame, ws)
from ._table import Row, check, check_under, table

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(30)]

ROWS = [
    Row('header_max_line', 'http1', '431', setting='header_max_line',
        env={'BB_HEADER_MAX_LINE': '64'},
        provoke=lambda: http1(head(b'X-Long: ' + b'v' * 200))),
    Row('header_max_total', 'http1', '431', setting='header_max_total',
        env={'BB_HEADER_MAX_TOTAL': '256'},
        provoke=lambda: http1(head(*(b'X-%d: %s' % (i, b'v' * 40) for i in range(20))))),
    Row('header_max_total', 'http2', 'GOAWAY ENHANCE_YOUR_CALM', setting='header_max_total',
        documented='GOAWAY ENHANCE_YOUR_CALM', env={'BB_HEADER_MAX_TOTAL': '256'},
        provoke=lambda: h2(h2_request(1, (b'x-long', b'v' * 500)))),
    Row('max_body_size', 'http1', '413', setting='max_body_size',
        env={'BB_MAX_BODY_SIZE': '10'},
        provoke=lambda: http1(head(b'Content-Length: 100', method=b'POST') + b'x' * 100)),
    Row('h1_chunk_line_length', 'http1', '400',
        provoke=lambda: http1(head(b'Transfer-Encoding: chunked', method=b'POST')
                              + b'1;' + b'e' * 9000 + b'\r\n')),
    Row('ws_max_frame_payload', 'ws', 'close 1009', setting='ws_max_frame_payload', documented='1009',
        env={'BB_WS_MAX_FRAME_PAYLOAD': '16'},
        provoke=lambda: ws(client_frame(b'x' * 100)),
        under=lambda: ws(client_frame(b'x' * 10),
                         client_frame((1000).to_bytes(2, 'big'), WSOpcode.CLOSE)),
        under_answer='close 1000'),
    Row('ws_max_message_size', 'ws', 'close 1009', setting='ws_max_message_size', documented='1009',
        env={'BB_WS_MAX_MESSAGE_SIZE': '16'},
        provoke=lambda: ws(client_frame(b'x' * 10, fin=False),
                           client_frame(b'x' * 10, WSOpcode.CONTINUATION))),
    Row('max_body_size', 'http2', ':status 413', setting='max_body_size', documented='413',
        env={'BB_MAX_BODY_SIZE': '10'},
        provoke=lambda: h2(h2_request(1, (b'content-length', b'100')))),
    Row('h2_inbound_window_budget', 'http2', 'RST_STREAM ENHANCE_YOUR_CALM',
        env={'BB_H2_INITIAL_WINDOW_SIZE': '1024'},
        provoke=lambda: h2(h2_request(1, path=b'/hold'),
                           h2_data(1, b'x' * 1024), h2_data(1, b'x'))),
    Row('h2_priority_update_buffer', 'http2', 'ignored',
        env={'BB_H2_MAX_CONCURRENT_STREAMS': '1'},
        provoke=lambda: h2(_priority_update(3), _priority_update(5), probe=True)),
    Row('mqtt_max_packet_size', 'mqtt', 'DISCONNECT 0x95', setting='mqtt_max_packet_size',
        documented='0x95', env={'BB_MQTT_MAX_PACKET_SIZE': '64'},
        provoke=lambda: mqtt([connect(), MQTTPublish(topic='t', payload=b'x' * 200)],
                             answer=MQTTDisconnect)),
    Row('client_body_max_total', 'http1', 'ResponseTooLarge', setting='client_body_max_total',
        env={'BB_CLIENT_BODY_MAX_TOTAL': '10'},
        provoke=lambda: client_http1(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n' + b'x' * 100)),
    Row('client_body_max_total', 'http2', 'ResponseTooLarge', setting='client_body_max_total',
        env={'BB_CLIENT_BODY_MAX_TOTAL': '10'},
        provoke=lambda: client_h2(h2_response(1), h2_data(1, b'x' * 100, end_stream=True))),
    Row('client_head_max_total', 'http1', 'ResponseTooLarge', setting='client_head_max_total',
        env={'BB_CLIENT_HEAD_MAX_TOTAL': '128'},
        provoke=lambda: client_http1(b'HTTP/1.1 200 OK\r\n' + b''.join(
            b'X-%d: %s\r\n' % (i, b'v' * 20) for i in range(10)) + b'Content-Length: 0\r\n\r\n')),
    Row('client_head_max_total', 'http2', 'ResponseTooLarge', setting='client_head_max_total',
        env={'BB_CLIENT_HEAD_MAX_TOTAL': '128'},
        provoke=lambda: client_h2(h2_response(1, *((b'x-%d' % i, b'v' * 20) for i in range(10)),
                                              end_stream=True))),
    Row('client_head_max_line', 'http1', 'ResponseTooLarge', setting='client_head_max_line',
        env={'BB_CLIENT_HEAD_MAX_LINE': '32'},
        provoke=lambda: client_http1(b'HTTP/1.1 200 OK\r\nX-Long: ' + b'v' * 100
                                     + b'\r\nContent-Length: 0\r\n\r\n')),
    Row('client_h2_max_frame_size', 'http2', 'GOAWAY FRAME_SIZE_ERROR',
        setting='client_h2_max_frame_size', documented='FRAME_SIZE_ERROR',
        env={'BB_CLIENT_H2_MAX_FRAME_SIZE': '1024'},
        provoke=lambda: client_h2(h2_response(1), h2_data(1, b'x' * 2000), report='sent')),
    Row('client_h2_max_header_list_size', 'http2', 'GOAWAY COMPRESSION_ERROR',
        setting='client_h2_max_header_list_size', documented='COMPRESSION_ERROR',
        env={'BB_CLIENT_H2_MAX_HEADER_LIST_SIZE': '64'},
        provoke=lambda: client_h2(h2_response(1, (b'x-big', b'v' * 200), end_stream=True),
                                  report='sent')),
    Row('client_ws_max_frame_payload', 'ws', 'close 1009', setting='client_ws_max_frame_payload',
        documented='1009', env={'BB_CLIENT_WS_MAX_FRAME_PAYLOAD': '16'},
        provoke=lambda: client_ws(server_frame(b'x' * 100))),
    Row('client_ws_max_message_size', 'ws', 'close 1009', setting='client_ws_max_message_size',
        documented='1009', env={'BB_CLIENT_WS_MAX_MESSAGE_SIZE': '16'},
        provoke=lambda: client_ws(server_frame(b'x' * 10, fin=False),
                                  server_frame(b'x' * 10, WSOpcode.CONTINUATION))),
    Row('mqtt_broker_inbox_max_bytes', 'mqtt', 'DISCONNECT 0x97',
        setting='mqtt_broker_inbox_max_bytes', documented='DISCONNECT 0x97',
        env={'BB_MQTT_BROKER_INBOX_MAX_BYTES': '64'},
        provoke=lambda: mqtt([connect(), MQTTPublish(topic='t', payload=b'x' * 200)],
                             answer=MQTTDisconnect)),
    Row('mqtt_connection_inbox_max_bytes', 'mqtt', 'closed',
        setting='mqtt_connection_inbox_max_bytes', documented='ends only that connection',
        env={'BB_MQTT_CONNECTION_INBOX_MAX_BYTES': '1000'},
        provoke=lambda: mqtt_subscriber_fate(MQTTPublish(topic='t', payload=b'x' * 2000))),
]


def _priority_update(sid: int) -> bytes:
    """RFC 9218 §7.1 PRIORITY_UPDATE for an idle stream, sent on stream 0."""
    return _make_h2_frame(FrameTypes.PRIORITY_UPDATE, 0, 0,
                          sid.to_bytes(4, 'big') + b'u=1')


@table(*ROWS)
async def test_exceeding_a_size_cap_is_answered_as_documented(row, monkeypatch, caplog):
    await check(row, monkeypatch, caplog)



@table(*ROWS, under=True)
async def test_size_within_a_cap_is_not_recorded(row, monkeypatch, caplog):
    await check_under(row, monkeypatch, caplog)

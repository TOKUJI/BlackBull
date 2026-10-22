"""Caps on how slowly, or how often, a peer may do something."""
import pytest

from blackbull.protocol.frame_types import FrameTypes
from blackbull.server import deadline
from blackbull.server.ws_codec import WSOpcode
from tests.conformance.http2._harness import _make_h2_frame

from ._drive import (client_frame, client_h2, client_http1, h2, h2_data, h2_request,
                     h2_response, head, http1_seen, ws)
from ._table import Row, check, table

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(30)]

_SLOW = {'BB_MIN_BODY_RATE': '1000', 'BB_MIN_BODY_RATE_GRACE': '0.2'}
_CLIENT_SLOW = {'BB_CLIENT_MIN_BODY_RATE': '1000', 'BB_CLIENT_MIN_BODY_RATE_GRACE': '0.2'}


@pytest.fixture(autouse=True)
def _fine_scanner(monkeypatch):
    monkeypatch.setattr(deadline, 'TICK_S', 0.01)


def _ping(n: int) -> bytes:
    return _make_h2_frame(FrameTypes.PING, 0, 0, bytes([n]) * 8)


ROWS = [
    Row('min_body_rate', 'http1', 'http.disconnect, closed', setting='min_body_rate',
        documented='disconnects and closes', env=_SLOW,
        provoke=lambda: http1_seen(head(b'Content-Length: 100', method=b'POST', target=b'/seen'),
                                   b'a', b'b', b'c', pause=0.15)),
    Row('min_body_rate', 'http2', 'RST_STREAM ENHANCE_YOUR_CALM', setting='min_body_rate',
        documented='ENHANCE_YOUR_CALM', env=_SLOW,
        provoke=lambda: h2(h2_request(1, path=b'/hold'), h2_data(1, b'a'), h2_data(1, b'b'),
                           h2_data(1, b'c'), pause=0.15)),
    Row('client_min_body_rate', 'http1', 'TimeoutError', setting='client_min_body_rate',
        documented='abandons its connection', env=_CLIENT_SLOW,
        provoke=lambda: client_http1(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n',
                                     b'a', b'b', b'c', pause=0.15, hold=True)),
    Row('client_min_body_rate', 'http2', 'RST_STREAM CANCEL', setting='client_min_body_rate',
        documented='RST_STREAM(CANCEL)', env=_CLIENT_SLOW,
        provoke=lambda: client_h2(h2_response(1), h2_data(1, b'a'), h2_data(1, b'b'),
                                  h2_data(1, b'c'), pause=0.15, report='sent')),
    Row('frame_rate', 'http2', 'GOAWAY ENHANCE_YOUR_CALM', setting='frame_rate_limit',
        documented='GOAWAY(ENHANCE_YOUR_CALM)', env={'BB_FRAME_RATE_LIMIT': '2'},
        provoke=lambda: h2(*(_ping(i) for i in range(3)))),
    Row('frame_rate', 'ws', 'close 1008', setting='frame_rate_limit',
        documented='WS close 1008', env={'BB_FRAME_RATE_LIMIT': '2'},
        provoke=lambda: ws(*(client_frame(b'', WSOpcode.PING) for _ in range(3)))),
]


@table(*ROWS)
async def test_exceeding_a_rate_cap_is_answered_as_documented(row, monkeypatch, caplog):
    await check(row, monkeypatch, caplog)

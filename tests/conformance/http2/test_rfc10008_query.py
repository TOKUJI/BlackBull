"""RFC 10008 — The HTTP QUERY method, over HTTP/2 framing.

``:method`` is an arbitrary token in HTTP/2 (RFC 9113 §8.3.1), so QUERY
needs no protocol-level support — these tests pin that a QUERY request
dispatches through HTTP2Actor to a registered BlackBull route and that the
DATA payload reaches the handler as the request body.
"""
import pytest
from unittest.mock import AsyncMock

from blackbull import BlackBull, QUERY, read_body
from blackbull.protocol.frame_types import FrameTypes, DataFrameFlags

from .test_http2_dispatch import (_make_h2_frame, _make_headers_frame,
                                  _make_h2_actor)


def _query_app(captured: dict) -> BlackBull:
    app = BlackBull()

    @app.route(path='/search', methods=[QUERY])
    async def search(scope, receive, send):
        captured['method'] = scope['method']
        captured['body'] = await read_body(receive)
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'text/plain')]})
        await send({'type': 'http.response.body', 'body': b'ok'})

    return app


@pytest.mark.asyncio
class TestQueryOverHTTP2:
    async def test_query_dispatches_with_data_payload(self):
        captured: dict = {}
        h_frame = _make_headers_frame(stream_id=1, end_stream=False,
                                      method=b'QUERY', path=b'/search')
        d_frame = _make_h2_frame(FrameTypes.DATA, DataFrameFlags.END_STREAM,
                                 1, b'select *')

        handler, _ = _make_h2_actor(_query_app(captured))
        handler.receive = AsyncMock(side_effect=[h_frame, d_frame, None])
        await handler.run()

        assert captured.get('method') == 'QUERY'
        assert captured.get('body') == b'select *'

    async def test_query_without_body_dispatches(self):
        captured: dict = {}
        h_frame = _make_headers_frame(stream_id=1, end_stream=True,
                                      method=b'QUERY', path=b'/search')

        handler, _ = _make_h2_actor(_query_app(captured))
        handler.receive = AsyncMock(side_effect=[h_frame, None])
        await handler.run()

        assert captured.get('method') == 'QUERY'
        assert captured.get('body') == b''

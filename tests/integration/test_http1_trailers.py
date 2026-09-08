"""Socket-level HTTP/1.1 response trailer coverage."""

import socket

import pytest

from blackbull import BlackBull

from .conftest import live_server


def _make_app() -> BlackBull:
    app = BlackBull()

    @app.route(path='/trailers')
    async def trailers(scope, receive, send):
        await send({
            'type': 'http.response.start',
            'status': 200,
            'headers': [(b'content-length', b'5')],
            'trailers': True,
        })
        await send({
            'type': 'http.response.body',
            'body': b'hello',
            'more_body': False,
        })
        await send({
            'type': 'http.response.trailers',
            'headers': [(b'x-checksum', b'ok')],
            'more_trailers': False,
        })

    return app


@pytest.mark.integration
def test_trailers_complete_the_chunked_response_on_the_wire():
    with live_server(_make_app()) as live:
        with socket.create_connection(('127.0.0.1', live.port), timeout=2) as client:
            client.sendall(
                b'GET /trailers HTTP/1.1\r\n'
                b'Host: 127.0.0.1\r\n'
                b'Connection: close\r\n\r\n'
            )
            chunks = []
            while chunk := client.recv(4096):
                chunks.append(chunk)

    response = b''.join(chunks)
    header_block, body = response.split(b'\r\n\r\n', 1)
    assert b'transfer-encoding: chunked' in header_block.lower()
    assert b'content-length:' not in header_block.lower()
    assert body == b'5\r\nhello\r\n0\r\nx-checksum: ok\r\n\r\n'

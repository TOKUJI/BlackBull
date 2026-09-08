"""HTTP/1.1 response framing observed through a real TCP connection."""

import pytest

from .conftest import send_raw


pytestmark = pytest.mark.integration


def _get(path: str) -> bytes:
    return (f'GET {path} HTTP/1.1\r\nHost: localhost\r\n'
            'Connection: close\r\n\r\n').encode()


def test_known_length_stream_uses_content_length_without_transfer_encoding(
        h1_app):
    response = send_raw('127.0.0.1', h1_app.port,
                        _get('/framing-known-stream'))

    assert response.status == 200
    assert response.headers_named(b'content-length') == [b'2']
    assert response.headers_named(b'transfer-encoding') == []
    assert response.body == b'ab'


def test_no_content_response_has_no_framing_or_body(h1_app):
    response = send_raw('127.0.0.1', h1_app.port, _get('/framing-204'))

    assert response.status == 204
    assert response.headers_named(b'content-length') == []
    assert response.headers_named(b'transfer-encoding') == []
    assert response.body == b''


def test_reset_content_response_has_an_explicit_zero_length(h1_app):
    response = send_raw('127.0.0.1', h1_app.port, _get('/framing-205'))

    assert response.status == 205
    assert response.headers_named(b'content-length') == [b'0']
    assert response.headers_named(b'transfer-encoding') == []
    assert response.body == b''

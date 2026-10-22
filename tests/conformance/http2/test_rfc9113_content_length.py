"""RFC 9113 §8.1.1: a ``content-length`` that is not one canonical number is
malformed, on both roles, as HTTP/1.1 refuses it."""
import pytest

from tests.caps._drive import client_h2, h2, h2_data, h2_request, h2_response

pytestmark = pytest.mark.asyncio

#: values -> (server answer, client answer) for a 5-byte body
CASES = {
    'one': ((b'5',), 'complete', 'ok'),
    'repeated': ((b'5', b'5'), 'complete', 'ok'),
    'leading-zero': ((b'05',), 'RST_STREAM PROTOCOL_ERROR', 'ProtocolError'),
    'list': ((b'5, 5',), 'RST_STREAM PROTOCOL_ERROR', 'ProtocolError'),
    'sign': ((b'+5',), 'RST_STREAM PROTOCOL_ERROR', 'ProtocolError'),
    'fields-disagree': ((b'5', b'9'), 'RST_STREAM PROTOCOL_ERROR', 'ProtocolError'),
}


@pytest.mark.parametrize('values,server,_client', CASES.values(), ids=CASES.keys())
async def test_the_server_reads_the_length(values, server, _client):
    fields = [(b'content-length', v) for v in values]
    assert await h2(h2_request(1, *fields), h2_data(1, b'hello', end_stream=True)) == server


@pytest.mark.parametrize('values,_server,client', CASES.values(), ids=CASES.keys())
async def test_the_client_reads_the_length(values, _server, client):
    fields = [(b'content-length', v) for v in values]
    assert await client_h2(h2_response(1, *fields), h2_data(1, b'hello', end_stream=True)) == client

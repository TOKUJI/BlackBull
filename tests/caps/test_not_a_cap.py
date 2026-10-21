"""Refusals that look like a cap but are not one, and must not be recorded as one."""
import asyncio
import logging

import pytest

from blackbull.client.http1 import HTTP1ResponseRecipient
from blackbull.protocol.frame_types import FrameTypes
from tests.conformance.http2._harness import _make_h2_frame
from tests.harness import paced

from ._drive import client_h2

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(30)]


def _caps(caplog) -> list[str]:
    return [r.cap for r in caplog.records if r.name == 'blackbull.caps']


@pytest.mark.parametrize('prefix,cap', [
    (b'HTTP/1.1 200 OK\r\nx-half: ', 'client_head_timeout'),
    (b'HTTP/1.1 200 OK\r\ncontent-length: 10\r\n\r\nab', 'client_body_timeout'),
], ids=['head', 'body'])
async def test_a_readers_own_timeout_is_not_the_clients_cap(prefix, cap, monkeypatch, caplog):
    """Only the cap's own deadline may be recorded; here the deadline is long
    and the transport itself raises ``TimeoutError``."""
    caplog.set_level(logging.WARNING, logger='blackbull.caps')
    monkeypatch.setenv(f'BB_{cap.upper()}', '30.0')

    async def transport_says_so(gap):
        if gap:
            raise TimeoutError('the transport said so, not the cap')

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(HTTP1ResponseRecipient().receive(
            paced([(0, prefix), (1, b'')], transport_says_so)), 3.0)
    assert cap not in _caps(caplog)


async def test_an_invalid_header_block_is_not_the_header_list_cap(monkeypatch, caplog):
    """Index 0 names no table entry: the block is malformed, not too large."""
    caplog.set_level(logging.WARNING, logger='blackbull.caps')
    monkeypatch.setenv('BB_CLIENT_H2_MAX_HEADER_LIST_SIZE', '2048')
    answer = await client_h2(_make_h2_frame(FrameTypes.HEADERS, 4, 1, b'\x80'), report='sent')
    assert answer == 'GOAWAY COMPRESSION_ERROR'
    assert 'client_h2_max_header_list_size' not in _caps(caplog)

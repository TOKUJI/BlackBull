"""One HTTP/2 stream's DATA as the WebSocket codec's byte stream, in both roles.

Credit returns as octets are consumed, padding included, so the peer can hold
at most one window unconsumed and DATA past it is refused.
"""
import asyncio

import pytest

from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import DataFrameFlags, FrameTypes
from blackbull.server.http2_ws import HTTP2StreamReader
from blackbull.server.recipient import IncompleteReadError

pytestmark = pytest.mark.asyncio

WINDOW = 100


def _data(payload: bytes, *, end: bool = False, pad: int = 0):
    flags = DataFrameFlags.END_STREAM if end else 0
    if pad:
        flags |= DataFrameFlags.PADDED
        payload = bytes([pad]) + payload + bytes(pad)
    return FrameFactory().create(FrameTypes.DATA, flags, 1, data=payload)


def _reader():
    credited: list[int] = []

    async def credit(n: int) -> None:
        credited.append(n)

    return HTTP2StreamReader(WINDOW, credit), credited


async def test_arrival_alone_returns_no_credit():
    reader, credited = _reader()
    assert reader.put_DATAFrame(_data(b'x' * 40))
    await asyncio.sleep(0)
    assert credited == []


async def test_consumed_octets_are_credited_with_their_padding_before_a_wait():
    reader, credited = _reader()
    assert reader.put_DATAFrame(_data(b'abcd', pad=5))      # 10 window octets
    assert await reader.readexactly(4) == b'abcd'
    parked = asyncio.create_task(reader.readexactly(1))
    await asyncio.sleep(0)
    assert sum(credited) == 10
    parked.cancel()


async def test_data_past_the_window_is_refused():
    reader, _ = _reader()
    assert reader.put_DATAFrame(_data(b'x' * 90, pad=9))   # exactly the window
    assert not reader.put_DATAFrame(_data(b'y'))


async def test_a_read_longer_than_the_window_completes():
    """The peer sends only what it is credited for; the read needs 2.5 windows."""
    sent = 0

    def send(n: int) -> None:
        nonlocal sent
        n = min(n, 250 - sent)
        if n:
            assert reader.put_DATAFrame(_data(b'z' * n))
            sent += n

    async def credit(n: int) -> None:
        send(n)

    reader = HTTP2StreamReader(WINDOW, credit)
    send(WINDOW)
    assert await asyncio.wait_for(reader.readexactly(250), 1) == b'z' * 250


async def test_end_stream_is_eof_after_the_buffered_octets():
    reader, _ = _reader()
    reader.put_DATAFrame(_data(b'ab', end=True))
    assert await reader.readexactly(2) == b'ab'
    with pytest.raises(IncompleteReadError):
        await reader.readexactly(1)


async def test_disconnect_wakes_a_parked_read():
    reader, _ = _reader()
    parked = asyncio.create_task(reader.readexactly(1))
    await asyncio.sleep(0)
    reader.put_disconnect()
    with pytest.raises(IncompleteReadError):
        await asyncio.wait_for(parked, 1)


async def test_released_credit_covers_every_octet_not_yet_credited():
    reader, credited = _reader()
    reader.put_DATAFrame(_data(b'abc', pad=2))              # 6 window octets
    assert await reader.readexactly(1) == b'a'
    assert reader.take_uncredited() + sum(credited) == 6
    assert reader.take_uncredited() == 0


@pytest.mark.parametrize('call', [
    lambda r: r.readuntil(b'\r\n'),
    lambda r: r.readuntil(b'\r\n', 64),
    lambda r: r.read_head(0),
    lambda r: r.read_head(1024),
], ids=['readuntil', 'bounded-readuntil', 'read_head', 'bounded-read_head'])
async def test_websocket_payload_has_no_lines_to_read(call):
    reader, _ = _reader()
    reader.put_DATAFrame(_data(b'GET / HTTP/1.1\r\nhost: x\r\n\r\n', end=True))
    with pytest.raises(NotImplementedError):
        await asyncio.wait_for(call(reader), 1)



async def test_a_parked_read_has_had_every_received_octet_credited():
    """While a read waits, every buffered octet is committed to it, so all that
    has arrived is credited — across a cancelled read, partial reads and
    padding — and nothing is credited twice."""
    reader, credited = _reader()
    received = 0

    def put(payload: bytes, pad: int = 0) -> None:
        nonlocal received
        assert reader.put_DATAFrame(_data(payload, pad=pad))
        received += len(payload) + (pad + 1 if pad else 0)

    async def park(n: int) -> asyncio.Task:
        task = asyncio.create_task(reader.readexactly(n))
        await asyncio.sleep(0)
        return task

    put(b'a' * 10, pad=4)
    abandoned = await park(30)
    assert sum(credited) == received
    abandoned.cancel()
    assert await reader.readexactly(4) == b'aaaa'
    reparked = await park(20)                   # six octets already committed
    assert sum(credited) == received
    reparked.cancel()
    put(b'b' * 20)
    assert await reader.readexactly(7) == b'aaaaaab'
    put(b'c' * 5, pad=3)
    parked = await park(40)
    assert sum(credited) == received
    parked.cancel()
    assert reader.take_uncredited() == 0


async def test_half_a_window_consumed_is_credited_without_waiting():
    reader, credited = _reader()
    reader.put_DATAFrame(_data(b'x' * 60))
    await reader.readexactly(40)
    assert credited == []
    await reader.readexactly(10)
    assert credited == [50]


async def test_data_that_arrives_while_crediting_completes_the_read():
    """The credit is what lets the peer send the rest; the read must notice."""
    async def credit(n: int) -> None:
        reader.put_DATAFrame(_data(b'y' * 10))

    reader = HTTP2StreamReader(WINDOW, credit)
    reader.put_DATAFrame(_data(b'x' * 10))
    assert await asyncio.wait_for(reader.readexactly(20), 1) == b'x' * 10 + b'y' * 10


async def test_eof_mid_read_reports_what_did_arrive():
    reader, _ = _reader()
    reader.put_DATAFrame(_data(b'ab', end=True))
    with pytest.raises(IncompleteReadError) as caught:
        await reader.readexactly(3)
    assert caught.value.partial == b'ab'


async def test_the_window_counts_every_frame_still_uncredited():
    reader, _ = _reader()
    assert reader.put_DATAFrame(_data(b'x' * 40))
    assert reader.put_DATAFrame(_data(b'x' * 40))
    assert not reader.put_DATAFrame(_data(b'x' * 30))

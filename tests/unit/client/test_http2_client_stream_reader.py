"""A client stream handed to a reader, observed on the wire a real peer sees.

Credit follows consumption at both levels; a stream that overruns its window
or is reset ends without taking the connection, or its credit, with it.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.client.http2 import HTTP2Client
from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import (ErrorCodes, FrameTypes,
                                            SettingFrameFlags)
from blackbull.server.recipient import IncompleteReadError
from tests import harness

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(10)]

_FACTORY = FrameFactory()


def _data(stream_id: int, payload: bytes) -> bytes:
    return _FACTORY.create(FrameTypes.DATA, 0, stream_id, data=payload).save()


def _rst(stream_id: int) -> bytes:
    return _FACTORY.rst_stream(stream_id, ErrorCodes.CANCEL).save()


class _Origin:
    """Sends *script* once released, and records every frame the client sends."""

    def __init__(self, *script: bytes) -> None:
        self.script = b''.join(script)
        self.release = asyncio.Event()
        self.frames: list[tuple[bytes, int, bytes]] = []
        self._changed = asyncio.Event()

    async def __call__(self, reader, writer) -> None:
        await reader.readexactly(24)
        writer.write(_FACTORY.create(
            FrameTypes.SETTINGS, SettingFrameFlags.INIT, 0).save())
        await writer.drain()
        await self.release.wait()
        writer.write(self.script)
        await writer.drain()
        try:
            while True:
                head = await reader.readexactly(9)
                payload = await reader.readexactly(int.from_bytes(head[:3], 'big'))
                self.frames.append((head[3:4], int.from_bytes(head[5:], 'big'),
                                    payload))
                self._changed.set()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass

    def credit(self, stream_id: int) -> int:
        return sum(int.from_bytes(p, 'big') for t, s, p in self.frames
                   if t == FrameTypes.WINDOW_UPDATE.value and s == stream_id)

    def resets(self) -> list[tuple[int, ErrorCodes]]:
        return [(s, ErrorCodes(int.from_bytes(p, 'big'))) for t, s, p in self.frames
                if t == FrameTypes.RST_STREAM.value]

    async def until(self, seen) -> None:
        async with asyncio.timeout(2):
            while not seen():
                self._changed.clear()
                await self._changed.wait()


async def _attached(client: HTTP2Client, stream_id: int):
    client.register_raw_stream(stream_id)
    return await client.attach_stream_reader(stream_id)


async def test_consumption_credits_the_stream_and_the_connection():
    origin = _Origin(_data(1, b'x' * 16384), _data(1, b'x' * 16384),
                     _data(1, b'x' * 7232))
    async with harness.peer(origin) as port:
        async with HTTP2Client('127.0.0.1', port) as client:
            reader = await _attached(client, 1)
            origin.release.set()
            assert await reader.readexactly(40000) == b'x' * 40000
            idle = asyncio.create_task(reader.readexactly(1))   # the codec, between messages
            await origin.until(lambda: origin.credit(0))
            idle.cancel()
    assert (origin.credit(1), origin.credit(0)) == (40000, 40000)


async def test_an_unregistered_stream_is_credited_only_on_the_connection():
    origin = _Origin(_data(1, b'x' * 16384), _data(1, b'x' * 16384),
                     _data(1, b'x' * 7232))
    async with harness.peer(origin) as port:
        async with HTTP2Client('127.0.0.1', port) as client:
            reader = await _attached(client, 1)
            origin.release.set()
            await reader.readexactly(40000)       # the first wait credited 16384
            client.unregister_raw_stream(1)
            idle = asyncio.create_task(reader.readexactly(1))
            await origin.until(lambda: origin.credit(0) == 40000)
            idle.cancel()
    assert (origin.credit(1), origin.credit(0)) == (16384, 40000)


async def test_data_past_the_window_resets_only_that_stream():
    origin = _Origin(*(_data(1, b'x' * 16384) for _ in range(4)),
                     _data(3, b'ok'))
    async with harness.peer(origin) as port:
        async with HTTP2Client('127.0.0.1', port) as client:
            overrun = await _attached(client, 1)
            sibling = await _attached(client, 3)
            origin.release.set()
            assert await sibling.readexactly(2) == b'ok'
            await origin.until(lambda: origin.resets())
            with pytest.raises(IncompleteReadError):
                await overrun.readexactly(65536)
            await origin.until(lambda: origin.credit(0) >= 49152)
    assert origin.resets() == [(1, ErrorCodes.FLOW_CONTROL_ERROR)]


async def test_a_reset_stream_returns_its_unread_credit_to_the_connection():
    origin = _Origin(_data(1, b'x' * 16384), _data(1, b'x' * 16384),
                     _data(1, b'x' * 7232), _rst(1), _data(3, b'ok'))
    async with harness.peer(origin) as port:
        async with HTTP2Client('127.0.0.1', port) as client:
            reset = await _attached(client, 1)
            sibling = await _attached(client, 3)
            origin.release.set()
            assert await sibling.readexactly(2) == b'ok'
            with pytest.raises(IncompleteReadError):
                await reset.readexactly(40001)
            await origin.until(lambda: origin.credit(0))
    assert (origin.credit(1), origin.credit(0)) == (0, 40000)

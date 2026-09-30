"""Shared in-process fakes and actor-driving helpers for the MQTT tests.

`_FakeMQTTReader` / `_FakeMQTTWriter` / `_ctx` are the wire-level fakes; the
helpers below replace the connect/run-sleep-cancel boilerplate that was
copied into every test and its fixed-sleep synchronisation.
"""
from __future__ import annotations

import asyncio

from blackbull.mqtt.messages import encode_packet, decode_packet
from blackbull.server.protocol_registry import ProtocolContext
from blackbull.server.sender import AbstractWriter
from blackbull.server.recipient import AbstractReader


class _FakeMQTTReader(AbstractReader):
    def __init__(self, data: bytes = b''):
        self._buf = bytearray(data)

    async def read(self, n: int) -> bytes:
        if not self._buf:
            # Simulate read timeout (real MQTT actor would use a deadline)
            await asyncio.sleep(0.01)
            return b''
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    def feed(self, data: bytes) -> None:
        self._buf.extend(data)

    def feed_packet(self, packet) -> None:
        self._buf.extend(encode_packet(packet))


class _FakeMQTTWriter(AbstractWriter):
    def __init__(self):
        self.written = bytearray()

    async def write(self, data: bytes) -> None:
        self.written.extend(data)

    def pop_packets(self) -> list:
        packets = []
        offset = 0
        buf = bytes(self.written)
        while offset < len(buf):
            packet, consumed = decode_packet(buf[offset:])
            packets.append(packet)
            offset += consumed
        self.written = self.written[offset:]
        return packets


def _ctx():
    return ProtocolContext(
        peername=('127.0.0.1', 54321),
        sockname=('0.0.0.0', 1883),
        ssl=False,
        aggregator=None,
        connection_id='test-conn',
        protocol='mqtt',
    )


async def wait_idle(reader: "_FakeMQTTReader", writer: "_FakeMQTTWriter",
                    *, timeout: float = 2.0) -> None:
    """Wait until the actor has drained every fed byte and gone quiet.

    Idle = the reader is out of bytes and the writer's byte count is unchanged
    across two polls — no fixed sleep, so no timing margin to be wrong about.
    """
    async def _poll():
        last_len, stable = -1, 0
        while stable < 2:
            if not reader._buf and len(writer.written) == last_len:
                stable += 1
            else:
                stable = 0
            last_len = len(writer.written)
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout)


async def cancel_all(*tasks) -> None:
    """Cancel and reap actor tasks (the repeated end-of-test block)."""
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass


async def run_until_idle(mqtt, *packets, timeout: float = 2.0):
    """The connect/subscribe/cancel flow in one place.

    A fake-backed actor consumes *packets* end to end (run until idle), then
    is cancelled.  Returns ``(reader, writer)`` for the assertions.
    """
    reader = _FakeMQTTReader()
    writer = _FakeMQTTWriter()
    for packet in packets:
        reader.feed_packet(packet)
    actor = mqtt.serve(reader, writer, _ctx())
    task = asyncio.create_task(actor.run())
    try:
        await wait_idle(reader, writer, timeout=timeout)
    finally:
        await cancel_all(task)
    return reader, writer

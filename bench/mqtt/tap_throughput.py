r"""MQTT tap-throughput benchmark (Sprint 53 baseline; reused unchanged in 54).

Measures how a *slow* ``on_message`` tap affects end-to-end delivery, by driving
the real ``BrokerActor`` + ``serve_connection`` over in-memory pipes:

  publisher --PUBLISH--> broker --deliver--> subscriber   (latency measured here)
       \--inline tap (await sleep(tap_delay))

The tap runs inline on the publisher's connection (the Sprint 53 contract), so a
slow tap serialises that connection's reads.  Sweeping ``tap_delay`` shows the
coupling: throughput falls and p99 latency rises while tap coverage stays 100 %.

Sprint 54 adds a decoupled ``TapActor`` (drop-newest overflow); re-run this same
script then with ``MQTTExtension(tap_mode='actor')`` to get the controlled
side-by-side (see bench/sprint-logs/sprint-54.md).

Run:  python bench/mqtt/tap_throughput.py [--messages N]
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import platform
import statistics
import struct
import sys
import time

from blackbull.mqtt import BrokerActor, serve_connection
from blackbull.mqtt.messages import (
    MQTTConnect, MQTTPublish, MQTTSubscribe, MQTTMessage,
    IncompletePacket, MQTTDecodeError,
    encode_packet, decode_packet,
)
from blackbull.server.protocol_registry import ProtocolContext
from blackbull.server.recipient import AbstractReader
from blackbull.server.sender import AbstractWriter

TAP_DELAYS = (0.0, 0.001, 0.005, 0.025)   # seconds
_TOPIC = 'sensors/room/temperature'


def _ctx(conn_id: str) -> ProtocolContext:
    return ProtocolContext(peername=('127.0.0.1', 0), sockname=('0.0.0.0', 1883),
                           ssl=False, aggregator=None, connection_id=conn_id,
                           protocol='mqtt')


class _PipeReader(AbstractReader):
    """In-memory reader: blocks while idle, returns b'' (EOF) once closed."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._closed = False
        self._event = asyncio.Event()

    async def read(self, n: int) -> bytes:
        while not self._buf and not self._closed:
            self._event.clear()
            await self._event.wait()
        if not self._buf:
            return b''
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    def feed(self, data: bytes) -> None:
        self._buf.extend(data)
        self._event.set()

    def close(self) -> None:
        self._closed = True
        self._event.set()

    def at_eof(self) -> bool:
        return self._closed and not self._buf


class _TimingWriter(AbstractWriter):
    """Subscriber writer: decodes delivered PUBLISHes and records latency."""

    def __init__(self) -> None:
        self._partial = bytearray()
        self.latencies: list[float] = []   # seconds, publish -> receipt

    async def write(self, data: bytes) -> None:
        now = time.perf_counter()
        self._partial.extend(data)
        while self._partial:
            try:
                msg, consumed = decode_packet(bytes(self._partial))
            except (IncompletePacket, MQTTDecodeError, ValueError):
                break
            del self._partial[:consumed]
            if isinstance(msg, MQTTPublish):
                sent = struct.unpack('<d', msg.payload[:8])[0]
                self.latencies.append(now - sent)

    async def drain(self) -> None:
        pass


class _NullWriter(AbstractWriter):
    async def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass


async def _run_once(tap_delay: float, n: int, timeout: float = 120.0) -> dict:
    broker = BrokerActor()
    broker_task = asyncio.create_task(broker.run())
    tasks = [broker_task]
    readers = []
    try:
        # --- subscriber on sensors/# ---
        sub_reader, sub_writer = _PipeReader(), _TimingWriter()
        readers.append(sub_reader)
        tasks.append(asyncio.create_task(
            serve_connection(sub_reader, sub_writer, _ctx('sub'), broker)))
        sub_reader.feed(encode_packet(
            MQTTConnect(client_id='sub', clean_start=True, keep_alive=0)))
        sub_reader.feed(encode_packet(
            MQTTSubscribe(packet_id=1, subscriptions=[('sensors/#', 0)])))
        await asyncio.sleep(0.05)

        # --- publisher with an inline slow tap ---
        taps = {'count': 0}

        async def tap(_msg):
            taps['count'] += 1
            if tap_delay:
                await asyncio.sleep(tap_delay)

        pub_reader = _PipeReader()
        readers.append(pub_reader)
        tasks.append(asyncio.create_task(
            serve_connection(pub_reader, _NullWriter(), _ctx('pub'), broker,
                             app_handlers=[('#', tap)])))
        pub_reader.feed(encode_packet(
            MQTTConnect(client_id='pub', clean_start=True, keep_alive=0)))
        await asyncio.sleep(0.02)

        # --- publish N messages "as fast as possible" ---
        start = time.perf_counter()
        blob = bytearray()
        for _ in range(n):
            payload = struct.pack('<d', time.perf_counter()) + b'.'
            blob += encode_packet(MQTTPublish(topic=_TOPIC, payload=bytes(payload),
                                              qos=0))
        pub_reader.feed(bytes(blob))

        while len(sub_writer.latencies) < n:
            if time.perf_counter() - start > timeout:
                break
            await asyncio.sleep(0.005)
        elapsed = time.perf_counter() - start
    finally:
        for r in readers:
            r.close()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    received = len(sub_writer.latencies)
    lat_ms = sorted(x * 1000 for x in sub_writer.latencies)
    return {
        'tap_delay': tap_delay,
        'received': received,
        'throughput': received / elapsed if elapsed else 0.0,
        'p50': statistics.median(lat_ms) if lat_ms else 0.0,
        'p99': (lat_ms[min(len(lat_ms) - 1, int(len(lat_ms) * 0.99))]
                if lat_ms else 0.0),
        'coverage': taps['count'] / n if n else 0.0,
    }


async def _main(n: int) -> None:
    print(f"# MQTT tap-throughput — {platform.python_implementation()} "
          f"{platform.python_version()} on {platform.system()}; "
          f"uvloop={'uvloop' in sys.modules}; messages={n}\n")
    header = f"{'tap_delay':>10} | {'throughput':>12} | {'p50 ms':>8} | {'p99 ms':>8} | {'coverage':>8}"
    print(header)
    print('-' * len(header))
    for delay in TAP_DELAYS:
        r = await _run_once(delay, n)
        print(f"{delay * 1000:>7.0f}ms | {r['throughput']:>10.0f}/s | "
              f"{r['p50']:>8.2f} | {r['p99']:>8.2f} | {r['coverage'] * 100:>6.0f}%")


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--messages', type=int, default=500,
                    help='messages per tap_delay (default 500; '
                         'kept modest because inline taps serialise at 1/tap_delay)')
    args = ap.parse_args()
    asyncio.run(_main(args.messages))

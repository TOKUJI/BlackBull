"""Unit tests for the ``BaseSender._write_many`` size gate.

Background (protocol-layer audit 2026-07-12): the v0.33.1 → v0.51.0 HttpArena
regression was root-caused to protocol senders handing small ``(head, body)``
/ ``(header, payload)`` pairs to ``transport.writelines``.  On CPython's
selector transport, ``writelines`` costs more than ``join + write()`` for
small payloads (memoryview allocations, ``sendmsg`` setup, and — under
backpressure — a send attempt plus writer re-registration on *every* call).
Measured breakeven on a drained socketpair: join wins ≤ 16 KiB, vectored wins
≥ 64 KiB.

The fix moves the transport strategy into ``BaseSender._write_many``: parts
totalling at most ``_VECTORED_JOIN_THRESHOLD`` bytes are joined and sent via
``write()``; larger payloads keep the vectored ``writelines`` path (saves the
full-body memcpy on the static-file cache-hit path).  Protocol senders keep
expressing *what* they have (``_write_many((a, b))``), not *how* to send it.
"""
from __future__ import annotations

import pytest

from blackbull.server.sender import (
    _VECTORED_JOIN_THRESHOLD,
    AbstractWriter,
    HTTP1Sender,
)


class _RecordingWriter(AbstractWriter):
    """AbstractWriter double that records which write method was used."""

    def __init__(self, fail_with: Exception | None = None):
        self.write_calls: list[bytes] = []
        self.writelines_calls: list[tuple[bytes, ...]] = []
        self._fail_with = fail_with

    async def write(self, data: bytes) -> None:
        if self._fail_with is not None:
            raise self._fail_with
        self.write_calls.append(bytes(data))

    async def writelines(self, parts) -> None:
        if self._fail_with is not None:
            raise self._fail_with
        self.writelines_calls.append(tuple(bytes(p) for p in parts))


def _sender(writer: AbstractWriter) -> HTTP1Sender:
    # HTTP1Sender is the lightest concrete BaseSender; _write_many lives on
    # the base class.
    return HTTP1Sender(writer)


@pytest.mark.asyncio
async def test_small_parts_are_joined_into_a_single_write():
    writer = _RecordingWriter()
    head, body = b'HTTP/1.1 200 OK\r\n\r\n', b'ok'

    await _sender(writer)._write_many((head, body))

    assert writer.write_calls == [head + body]
    assert writer.writelines_calls == []


@pytest.mark.asyncio
async def test_total_at_threshold_still_joins():
    writer = _RecordingWriter()
    head = b'H' * 100
    body = b'x' * (_VECTORED_JOIN_THRESHOLD - 100)

    await _sender(writer)._write_many((head, body))

    assert writer.write_calls == [head + body]
    assert writer.writelines_calls == []


@pytest.mark.asyncio
async def test_total_above_threshold_stays_vectored():
    writer = _RecordingWriter()
    head = b'H' * 100
    body = b'x' * _VECTORED_JOIN_THRESHOLD  # total = threshold + 100

    await _sender(writer)._write_many((head, body))

    assert writer.write_calls == []
    assert writer.writelines_calls == [(head, body)]


@pytest.mark.asyncio
async def test_join_path_wire_bytes_match_vectored_path():
    """Byte-level equivalence: both paths must put identical bytes on the wire."""
    small = _RecordingWriter()
    large = _RecordingWriter()
    head = b'H' * 64

    await _sender(small)._write_many((head, b'x' * 16))
    await _sender(large)._write_many((head, b'x' * (_VECTORED_JOIN_THRESHOLD + 1)))

    assert small.write_calls[0] == head + b'x' * 16
    assert b''.join(large.writelines_calls[0]) == head + b'x' * (_VECTORED_JOIN_THRESHOLD + 1)


@pytest.mark.asyncio
async def test_join_path_keeps_peer_close_tolerance():
    """_guarded_write semantics survive the gate: a peer reset on the joined
    write marks the sender closed and drops the write silently."""
    writer = _RecordingWriter(fail_with=ConnectionResetError('peer reset'))
    sender = _sender(writer)

    await sender._write_many((b'head', b'body'))   # must not raise

    assert sender._closed is True

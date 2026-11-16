"""Differential HTTP/2 oracle.

Run one broken-client scenario against a server, normalise what came back,
and compare two servers' outcomes.  The observer reads frames itself and
decodes field blocks with ``hpack`` directly, so BlackBull's own parser never
judges a peer.  A divergence is not automatically a defect; read its category
and the RFC requirement behind the scenario.
"""
from __future__ import annotations

import asyncio
import enum
import time
from dataclasses import dataclass

from hpack import Decoder

from ._scenario import Abort, HalfClose, Sleep, half_close
from .scenario_h2 import frame_bytes
from .scenario_h2_client import CLIENT_PREFACE, SendFrame, SendHeaders, SendPreface, SendRawBytes

__all__ = [
    'ACCEPTED_CATEGORIES',
    'Category',
    'H2Outcome',
    'PER_SCENARIO_TIMEOUT_S',
    'STEPS',
    'categorize',
    'run_scenario',
]


class Category(str, enum.Enum):
    """How two outcomes of the same scenario compare."""
    OK = 'OK'                            # the same answers
    BOTH_REJECTED = 'BOTH_REJECTED'      # the same rejection, or both ended the connection
    STATUS_DIFFER = 'STATUS_DIFFER'      # both answered a stream, with different statuses
    BODY_DIFFER = 'BODY_DIFFER'          # same status, different body
    REJECT_DIFFER = 'REJECT_DIFFER'      # one answered or stayed silent where the other rejected
    ERROR_DIFFER = 'ERROR_DIFFER'        # both rejected, with a different code or scope
    TEARDOWN_DIFFER = 'TEARDOWN_DIFFER'  # the same answers, but only one ended the connection
    TIMEOUT_DIFFER = 'TIMEOUT_DIFFER'    # one side timed out where the other did not
    INCONCLUSIVE = 'INCONCLUSIVE'        # nothing observed to compare, or both timed out
    HARNESS_ERROR = 'HARNESS_ERROR'      # the harness failed on either side


ACCEPTED_CATEGORIES: frozenset[Category] = frozenset({Category.OK, Category.BOTH_REJECTED})

_ERRORS = ('NO_ERROR', 'PROTOCOL_ERROR', 'INTERNAL_ERROR', 'FLOW_CONTROL_ERROR',
           'SETTINGS_TIMEOUT', 'STREAM_CLOSED', 'FRAME_SIZE_ERROR', 'REFUSED_STREAM',
           'CANCEL', 'COMPRESSION_ERROR', 'CONNECT_ERROR', 'ENHANCE_YOUR_CALM',
           'INADEQUATE_SECURITY', 'HTTP_1_1_REQUIRED')
_REJECTIONS = frozenset({'rejected', 'reset', 'connection_error'})


def _error_name(code: int) -> str:
    return _ERRORS[code] if code < len(_ERRORS) else f'0x{code:x}'


@dataclass(frozen=True)
class H2Outcome:
    """One side's normalised outcome.

    ``streams`` holds ``(stream_id, kind, detail)`` in stream order.  *kind* is
    ``response`` (detail ``(status, body)``), ``rejected`` (RST_STREAM
    PROTOCOL_ERROR or a 400 answer — RFC 9113 §8.1.1 allows either for a
    malformed request), ``reset`` (detail: another RST_STREAM code, or a field
    block the observer could not accept),
    ``connection_error`` (unanswered when a GOAWAY with an error arrived),
    ``timeout`` (a client that gave up), or ``open`` (no answer).
    ``goaway`` is the first GOAWAY's code; ``teardown`` is ``closed`` (by FIN
    or reset: unread data turns one into the other, and asyncio reports them
    differently across versions) or ``open``; ``error`` is a harness failure.
    """
    streams: tuple = ()
    goaway: str | None = None
    teardown: str = 'open'
    error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, 'streams', tuple(sorted(
            (sid, 'rejected', None) if kind == 'response' and detail[0] == 400
            else (sid, kind, detail)
            for sid, kind, detail in self.streams)))


def _stream_category(a: tuple, b: tuple) -> Category | None:
    (ka, da), (kb, db) = a, b
    if 'timeout' in (ka, kb):
        return Category.INCONCLUSIVE if ka == kb else Category.TIMEOUT_DIFFER
    if ka == kb:
        if ka == 'response':
            if da[0] != db[0]:
                return Category.STATUS_DIFFER
            return Category.BODY_DIFFER if da[1] != db[1] else None
        return Category.ERROR_DIFFER if da != db else None
    if ka in _REJECTIONS and kb in _REJECTIONS:
        return Category.ERROR_DIFFER
    return Category.REJECT_DIFFER


def _ended(outcome: H2Outcome) -> bool:
    """An error GOAWAY, or a connection closed with a stream unanswered."""
    if outcome.goaway not in (None, 'NO_ERROR'):
        return True
    return outcome.teardown != 'open' and any(k == 'open' for _, k, _ in outcome.streams)


def categorize(a: H2Outcome, b: H2Outcome) -> Category:
    """Compare two outcomes of one scenario; symmetric in *a* and *b*."""
    if a.error is not None or b.error is not None:
        return Category.HARNESS_ERROR
    sa = {sid: (kind, detail) for sid, kind, detail in a.streams}
    sb = {sid: (kind, detail) for sid, kind, detail in b.streams}
    # Under one shared connection error, whether a stream was answered before
    # the GOAWAY is timing (RFC 9113 §5.4.1), not a difference.
    shared_error = a.goaway == b.goaway and a.goaway not in (None, 'NO_ERROR')
    for sid in sorted(sa.keys() | sb.keys()):
        pair = sa.get(sid, ('open', None)), sb.get(sid, ('open', None))
        if shared_error and {pair[0][0], pair[1][0]} == {'response', 'connection_error'}:
            continue
        differ = _stream_category(*pair)
        if differ is not None:
            return differ
    if a.goaway != b.goaway:
        return Category.ERROR_DIFFER
    if a.goaway in (None, 'NO_ERROR') and a.teardown != b.teardown:
        return Category.TEARDOWN_DIFFER
    if _ended(a) or any(k in _REJECTIONS for k, _ in sa.values()):
        return Category.BOTH_REJECTED
    if any(k == 'response' for k, _ in sa.values()):
        return Category.OK
    return Category.INCONCLUSIVE


# ---------------------------------------------------------------------------
# The observer
# ---------------------------------------------------------------------------

#: Per-scenario wall-clock budget, steps and quiet wait together.
PER_SCENARIO_TIMEOUT_S = 15.0

_WRITES = (SendPreface, SendFrame, SendHeaders, SendRawBytes)
#: The step types ``run_scenario`` runs.
STEPS = _WRITES + (Sleep, HalfClose, Abort)
_DATA, _HEADERS, _RST, _SETTINGS, _PING, _GOAWAY, _WINDOW_UPDATE, _CONTINUATION = (
    0, 1, 3, 4, 6, 7, 8, 9)


def _opened_streams(scenario) -> set[int]:
    """Streams the scenario's HEADERS steps open; raw bytes are not re-read as frames."""
    return {step.stream_id for step in scenario.steps
            if isinstance(step, SendHeaders)
            or (isinstance(step, SendFrame) and step.frame_type[0] == _HEADERS)}


def _frame(kind: int, flags: int, sid: int, payload: bytes = b'') -> bytes:
    return frame_bytes(len(payload), bytes([kind]), flags, sid, payload)


def _at_frame_boundary(wire: bytes) -> bool:
    """Whether what the scenario wrote so far ends between frames."""
    if not wire.startswith(CLIENT_PREFACE):
        # Part of the preface is not a boundary; bytes that are no preface at
        # all have no frames to keep whole.
        return not wire or not CLIENT_PREFACE.startswith(wire)
    i = len(CLIENT_PREFACE)
    while i + 9 <= len(wire):
        i += 9 + int.from_bytes(wire[i:i + 3], 'big')
    return i == len(wire)


def _unpad(flags: int, payload: bytes, *, priority: bool = False) -> bytes:
    if flags & 0x8 and payload:
        payload = payload[1:len(payload) - payload[0]]
    if priority and flags & 0x20:
        payload = payload[5:]
    return payload


class _Observer:
    """Read every frame the server sends; answer as a peer would.

    SETTINGS and PING are acknowledged and consumed DATA is credited back.  An
    answer waits while the scenario is part-way through a frame, so it never
    splits one, and is dropped once the scenario has half-closed.
    """

    def __init__(self, reader, writer) -> None:
        self.reader, self.writer = reader, writer
        self.at_boundary = True
        self.half_closed = False
        self.pending = bytearray()
        self.failure: str | None = None
        self.decoder = Decoder()
        self.headers: dict[int, dict] = {}
        self.block: tuple[int, int, bytearray] | None = None
        self.bodies: dict[int, bytearray] = {}
        self.final: dict[int, tuple] = {}
        self.goaway: str | None = None
        self.teardown = 'open'
        self.last_frame = time.monotonic()

    async def run(self) -> None:
        try:
            while True:
                header = await self.reader.readexactly(9)
                length = int.from_bytes(header[:3], 'big')
                payload = await self.reader.readexactly(length) if length else b''
                self.last_frame = time.monotonic()
                self._frame(header[3], header[4],
                            int.from_bytes(header[5:9], 'big') & 0x7fffffff, payload)
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            self.teardown = 'closed'
        except Exception as exc:  # noqa: BLE001 — the observer's own fault, not the peer's
            self.failure = repr(exc)

    def _send(self, data: bytes) -> None:
        self.pending += data
        if self.at_boundary:
            self.flush()

    def flush(self) -> None:
        if self.pending and not self.half_closed and not self.writer.is_closing():
            self.writer.write(bytes(self.pending))
        self.pending.clear()

    def _frame(self, kind: int, flags: int, sid: int, payload: bytes) -> None:
        if self.block is not None and (kind != _CONTINUATION or sid != self.block[0]):
            # RFC 9113 §6.10: nothing may come between a field block's frames.
            self.final.setdefault(self.block[0],
                                  (self.block[0], 'reset', 'INTERLEAVED_FIELD_BLOCK'))
            self.block = None
        if kind == _SETTINGS and not flags & 0x1:
            self._send(_frame(_SETTINGS, 0x1, 0))
        elif kind == _PING and not flags & 0x1:
            self._send(_frame(_PING, 0x1, 0, payload))
        elif kind == _GOAWAY and self.goaway is None:
            self.goaway = _error_name(int.from_bytes(payload[4:8], 'big'))
        elif kind == _RST and sid not in self.final:
            code = int.from_bytes(payload[:4], 'big')
            self.final[sid] = ((sid, 'rejected', None) if code == 1
                               else (sid, 'reset', _error_name(code)))
        elif kind == _HEADERS:
            self.block = (sid, flags, bytearray(_unpad(flags, payload, priority=True)))
            self._end_block(flags)
        elif kind == _CONTINUATION and self.block is not None:
            self.block[2].extend(payload)
            self._end_block(flags)
        elif kind == _DATA:
            if payload:   # padding counts against the window too (§6.9.1)
                # A stream that just ended needs no more credit; only the connection does.
                credit = len(payload).to_bytes(4, 'big')
                self._send(_frame(_WINDOW_UPDATE, 0, 0, credit)
                           + (b'' if flags & 0x1 else _frame(_WINDOW_UPDATE, 0, sid, credit)))
            self.bodies.setdefault(sid, bytearray()).extend(_unpad(flags, payload))
            if flags & 0x1:
                self._finish(sid)

    def _end_block(self, flags: int) -> None:
        if not flags & 0x4:
            return
        sid, first_flags, block = self.block
        self.block = None
        try:
            fields = dict(self.decoder.decode(bytes(block)))
        except Exception:  # noqa: BLE001 — a block hpack refuses is the peer's fault
            self.final.setdefault(sid, (sid, 'reset', 'UNDECODABLE_FIELD_BLOCK'))
            return
        if sid not in self.headers and fields.get(':status', '200').startswith('1'):
            return
        self.headers.setdefault(sid, fields)
        if first_flags & 0x1:
            self._finish(sid)

    def _finish(self, sid: int) -> None:
        status = int(self.headers.get(sid, {}).get(':status', 0))
        self.final.setdefault(sid, (sid, 'response', (status, bytes(self.bodies.get(sid, b'')))))

    def outcome(self, opened: set[int]) -> H2Outcome:
        ended = self.goaway not in (None, 'NO_ERROR')
        streams = [self.final.get(sid) or (sid, 'connection_error' if ended else 'open', None)
                   for sid in opened | self.final.keys()]
        return H2Outcome(streams=tuple(streams), goaway=self.goaway, teardown=self.teardown,
                         error=self.failure)


async def run_scenario(host: str, port: int, scenario, *, quiet: float = 1.0,
                       budget: float = PER_SCENARIO_TIMEOUT_S,
                       connect_timeout: float = 2.0, ssl=None) -> H2Outcome:
    """Run *scenario* (a ``ScenarioH2Client``) and return what the server did.

    Only write steps, ``Sleep``, ``HalfClose`` and ``Abort`` run here: the
    observer reads everything, so a read step has nothing to do.  The run ends
    *quiet* seconds after the last frame, at end of stream, or at *budget*.
    """
    for step in scenario.steps:
        if not isinstance(step, STEPS):
            raise TypeError(f'{type(step).__name__} is not run by the oracle: '
                            'it observes every frame itself')
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ssl), connect_timeout)
    except (OSError, TimeoutError) as exc:
        return H2Outcome(error=repr(exc))
    observer = _Observer(reader, writer)
    task = asyncio.ensure_future(observer.run())
    deadline = time.monotonic() + budget
    wire = bytearray()
    try:
        for step in scenario.steps:
            if isinstance(step, _WRITES):
                data = step.wire()
                interval = getattr(step, 'byte_interval', 0.0)
                for chunk in ([data[i:i + 1] for i in range(len(data))] if interval else [data]):
                    wire += chunk
                    writer.write(chunk)
                    observer.at_boundary = _at_frame_boundary(bytes(wire))
                    if observer.at_boundary:
                        observer.flush()
                    await writer.drain()
                    if interval:
                        await asyncio.sleep(interval)
            elif isinstance(step, Sleep):
                await asyncio.sleep(step.duration)
            elif isinstance(step, HalfClose):
                observer.half_closed = half_close(writer)
            else:
                writer.transport.abort()
                break
        observer.last_frame = max(observer.last_frame, time.monotonic())
        while not task.done() and time.monotonic() < deadline:
            idle = time.monotonic() - observer.last_frame
            if idle >= quiet:
                break
            await asyncio.wait({task}, timeout=min(quiet - idle, deadline - time.monotonic()))
    except (ConnectionResetError, BrokenPipeError):
        # The server ended the connection mid-scenario; the observer has
        # recorded that, and it is the outcome being measured.
        pass
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        writer.transport.abort()
    return observer.outcome(_opened_streams(scenario))

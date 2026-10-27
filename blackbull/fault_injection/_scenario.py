"""The one fault-scenario model: steps, their JSON Lines form, and the driver.

Every cell walks the same steps — send, sleep, wait, expect, read, close.
What differs by protocol and role is how a send step reaches the wire (its
``wire()``, assembled here and never by the production serialisers; see
docs/about/architecture.md) and how a driver reads and writes its transport.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import ssl
import time
import typing
import warnings
from dataclasses import dataclass, field, fields

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The steps every vocabulary shares
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SendRawBytes:
    """Push arbitrary bytes at the peer — the escape hatch for anything a
    typed step will not build.

    ``byte_interval > 0`` sends one byte at a time with that delay: when the
    bytes are legal and the *pacing* is the fault (slowloris, a trickled
    status line), this is the only way to say so.
    """
    data: bytes
    byte_interval: float = 0.0

    def wire(self) -> bytes:
        return self.data


@dataclass(frozen=True)
class Sleep:
    """Idle for ``duration`` seconds without reading or writing."""
    duration: float


@dataclass(frozen=True)
class ReadResponse:
    """Read one message: an HTTP/1.1 response, or one HTTP/2 frame.

    On ``timeout`` the scenario ends with ``timed_out`` set and the step
    uncounted; the executor never raises.
    """
    timeout: float = 5.0


@dataclass(frozen=True)
class Abort:
    """Hard-close the connection (``transport.abort`` → RST on Linux).

    Terminal: later steps do not run.
    """


@dataclass(frozen=True)
class HalfClose:
    """Shut down the sending direction only (FIN), keep reading.

    **Not terminal** — later steps still run, which is the whole point.
    ``Abort`` is not a substitute: it sends RST, discarding what is buffered
    and leaving nothing to read.  ``result.half_closed`` says whether the
    transport accepted it; TLS cannot.
    """


@dataclass(frozen=True)
class _Matching:
    match: dict = field(default_factory=dict)
    timeout: float = 5.0


class _WaitFor(_Matching):
    """Read until a message satisfies ``match``; a **filter**.

    Messages that do not match are read, counted in ``wait_skipped`` and
    passed over.  A timeout sets ``wait_timed_out`` and the next step runs.
    """


class _Expect(_Matching):
    """Read exactly one message and record ``(match, matched)``; a **guard**.

    Nothing is skipped and the scenario moves on either way, so a scenario
    whose premise silently failed does not look like a pass.
    """


def half_close(writer) -> bool:
    """Send write-side FIN while retaining reads; return whether it succeeded."""
    if writer is None:
        return False
    transport = getattr(writer, 'transport', writer)
    try:
        if not transport.can_write_eof():
            return False
        transport.write_eof()
    except Exception:  # pragma: no cover - the peer may already be gone
        logger.debug('half_close raced the peer')
        return False
    return True


# ---------------------------------------------------------------------------
# JSON Lines — one format for every vocabulary
# ---------------------------------------------------------------------------

#: Written on the ``HEADER`` line.  A file without it is the v1 format.
FORMAT = 2


def to_json(scenario) -> str:
    """One ``HEADER`` line carrying the scenario's fields, then one line per
    step named by its class.  Bytes are hex: fault payloads are rarely
    UTF-8 and are read against a packet capture by eye."""
    head = {'op': 'HEADER', 'format': FORMAT}
    head.update((f.name, _out(getattr(scenario, f.name)))
                for f in fields(scenario) if f.name != 'steps')
    lines = [json.dumps(head)]
    for step in scenario.steps:
        codecs = getattr(type(step), '_codecs', {})
        record = {'op': type(step).__name__}
        for f in fields(step):
            value = getattr(step, f.name)
            record[f.name] = (codecs[f.name][0](value) if f.name in codecs
                              else _out(value))
        lines.append(json.dumps(record))
    return '\n'.join(lines)


def from_json(cls, src: str):
    """Parse what [`to_json`][] wrote into a *cls* scenario.

    The v1 format each vocabulary wrote before this one still reads, with a
    ``DeprecationWarning``.
    """
    records = [json.loads(line) for line in src.splitlines() if line.strip()]
    heads = [r for r in records if r.get('op') == 'HEADER']
    records = [r for r in records if r.get('op') != 'HEADER']
    head = heads[-1] if heads else {}
    version = head.get('format', 1)
    if version == 1:
        warnings.warn(
            f'{cls.__name__} JSON in the v1 format is deprecated; rewrite it '
            f'with to_json(from_json(text)).  Reading v1 is removed no earlier '
            f'than v0.84.0 (and not before 2026-11-28).',
            DeprecationWarning, stacklevel=3)
        records = [_from_v1(r, *cls._v1) for r in records]
    elif version != FORMAT:
        raise ValueError(f'scenario format {version} is not one this version reads')
    hints = typing.get_type_hints(cls)
    kinds = {k.__name__: k for k in
             typing.get_args(typing.get_args(hints['steps'])[0])}
    steps = []
    for record in records:
        op = record.pop('op')
        kind = kinds.get(op)
        if kind is None:
            raise ValueError(f'unknown step op for {cls.__name__}: {op!r}')
        steps.append(_build(kind, record))
    head.pop('op', None)
    head.pop('format', None)
    return _build(cls, head, steps=tuple(steps))


def _out(value):
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, tuple):
        return [_out(v) for v in value]
    return value


def _build(cls, record: dict, **given):
    hints = typing.get_type_hints(cls)
    codecs = getattr(cls, '_codecs', {})
    for name, value in record.items():
        given[name] = (codecs[name][1](value) if name in codecs
                       else _in(value, hints[name]))
    return cls(**given)


def _in(value, hint):
    if hint is dict:
        # JSON has no tuples, and ``header``'s pair must compare equal to
        # the scenario it came from.
        value = dict(value or {})
        if isinstance(value.get('header'), list):
            value['header'] = tuple(value['header'])
        return value
    if value is None:
        return None
    if hint is float:
        return float(value)
    if hint is bytes or bytes in typing.get_args(hint):
        return bytes.fromhex(value)
    if typing.get_origin(hint) is tuple:
        args = typing.get_args(hint)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_in(v, args[0]) for v in value)
        return tuple(_in(v, a) for v, a in zip(value, args))
    return value


def _from_v1(record: dict, base64_payloads: bool, ops: dict) -> dict:
    """Rewrite one v1 step record as the current format."""
    record = dict(record)
    op = record.pop('op', None)
    if op not in ops:
        raise ValueError(f'unknown step op: {op!r}')

    def hexed(text):
        return base64.b64decode(text).hex() if base64_payloads else text

    if isinstance(record.get('data'), str):
        record['data'] = hexed(record['data'])
    if isinstance(record.get('frame_type'), int):
        record['frame_type'] = bytes([record['frame_type']]).hex()
    if 'frame' in record:
        record['frame'] = frame = dict(record['frame'])
        for key in ('payload', 'append_data', 'data'):
            if key in frame:
                frame[key] = hexed(frame[key])
    return {'op': ops[op], **record}


# ---------------------------------------------------------------------------
# The driver — one step walk for all four cells
# ---------------------------------------------------------------------------


class _Driver:
    """Walk a scenario over one connection, folding outcomes into ``result``.

    A subclass supplies the transport: ``_write``, ``_read`` (a message, or
    ``None`` at end of stream; raises ``TimeoutError``), ``_matches``,
    ``_close``, ``_half_close``, and the result bookkeeping ``_seen``,
    ``_completed`` and ``_ended``.
    """

    def __init__(self, scenario, result) -> None:
        self.vocabulary = typing.get_args(typing.get_args(
            typing.get_type_hints(type(scenario))['steps'])[0])
        self.result = result

    async def run(self, steps) -> None:
        result = self.result
        for step in steps:
            if not isinstance(step, self.vocabulary):
                raise TypeError(f'unknown step type: {type(step).__name__}')
            if isinstance(step, Sleep):
                await asyncio.sleep(step.duration)
            elif isinstance(step, HalfClose):
                result.half_closed = self._half_close()
            elif isinstance(step, ReadResponse):
                try:
                    message = await self._read(step.timeout)
                except TimeoutError as exc:
                    result.timed_out = True
                    result.exception = repr(exc)
                    return
                if message is not None:
                    self._seen(message, kept=True)
            elif isinstance(step, _WaitFor):
                await self._wait(step)
            elif isinstance(step, _Expect):
                await self._expect(step)
            elif isinstance(step, Abort) or getattr(step, 'graceful', False):
                await self._ended(step)
                return
            else:
                await self._write(step.wire(), getattr(step, 'byte_interval', 0.0))
            self._completed(step)

    async def _wait(self, step: _WaitFor) -> None:
        result = self.result
        deadline = time.monotonic() + step.timeout
        while True:
            remaining = deadline - time.monotonic()
            message = None
            if remaining > 0:
                try:
                    message = await self._read(remaining)
                except TimeoutError:
                    pass
            if message is None:
                result.wait_timed_out = True
                return
            matched = self._matches(message, step.match)
            self._seen(message, kept=matched)
            if matched:
                return
            result.wait_skipped += 1

    async def _expect(self, step: _Expect) -> None:
        result = self.result
        try:
            message = await self._read(step.timeout)
        except TimeoutError:
            message = None
        if message is None:
            result.wait_timed_out = True
            result.expectations.append((dict(step.match), False))
            return
        self._seen(message, kept=True)
        result.expectations.append(
            (dict(step.match), self._matches(message, step.match)))

    def _seen(self, message, *, kept: bool) -> None:
        """Record a message read; *kept* is False for one a filter passed over."""

    def _completed(self, step) -> None:
        self.result.steps_completed += 1


class _ClientDriver(_Driver):
    """The broken-client half: a step that ends the walk is not counted."""

    async def execute(self, steps):
        t0 = time.monotonic()
        try:
            await self.run(steps)
        except Exception as exc:  # noqa: BLE001 — the executor never raises
            self.result.exception = repr(exc)
        finally:
            self.result.elapsed_s = time.monotonic() - t0
        return self.result

    async def _ended(self, step) -> None:
        await self._close(graceful=False)
        self.result.aborted = True


class _ServerConnection(_Driver):
    """The broken-server half, over one accepted connection."""

    def __init__(self, scenario, result, reader, writer) -> None:
        super().__init__(scenario, result)
        self.reader = reader
        self.writer = writer

    async def open(self) -> None:
        """Whatever precedes the steps — HTTP/2's preface and SETTINGS."""

    def release(self) -> None:
        """Undo :meth:`open`."""

    def failed(self, exc: Exception) -> None:
        self.result.exception = repr(exc)

    async def _ended(self, step) -> None:
        graceful = not isinstance(step, Abort)
        data = step.wire() if graceful else b''
        if data:
            await self._write(data, 0.0)
        await self._close(graceful=graceful)
        self._completed(step)
        self.result.terminated = True

    async def _write(self, data: bytes, byte_interval: float) -> None:
        # Each paced byte is drained before the pause, or the peer sees one
        # burst after the total delay and the scenario tests nothing.
        writer, result = self.writer, self.result
        chunks = ([data[i:i + 1] for i in range(len(data))]
                  if byte_interval > 0 else [data])
        for chunk in chunks:
            writer.write(chunk)
            await writer.drain()
            result.server_bytes_sent += len(chunk)
            if byte_interval > 0:
                await asyncio.sleep(byte_interval)

    async def _close(self, *, graceful: bool) -> None:
        try:
            if graceful:
                self.writer.close()
                await self.writer.wait_closed()
            else:
                self.writer.transport.abort()
        except Exception:  # pragma: no cover - the peer may already be gone
            logger.debug('%s: close raced the peer', type(self).__name__)

    def _half_close(self) -> bool:
        return half_close(self.writer)


class _FaultServer:
    """Serve one scenario to every client that connects.

    Refuses a production context and a non-loopback bind without
    ``allow_remote=True``.  ``stop()`` cancels connections still mid-scenario:
    a scenario ending in a long ``Sleep`` is how "the server never answers"
    is spelled, and teardown must not wait it out.
    """

    #: Raised when the server refuses to run.
    Error: type[RuntimeError]
    #: What the misbehaviour is, for the refusal message.
    _wrong = ''

    def __init__(self, scenario, *, host: str = '127.0.0.1', port: int = 0,
                 allow_remote: bool = False,
                 ssl_context: ssl.SSLContext | None = None):
        from ..env import Environment, get_settings  # noqa: PLC0415
        name = type(self).__name__
        cfg = get_settings()
        if cfg.production or cfg.env == Environment.PRODUCTION:
            raise self.Error(
                f'{name} refuses to run in a production context '
                '(BLACKBULL_ENV=production or BB_PRODUCTION set).  This is a '
                f'testing-only instrument that deliberately emits wrong '
                f'{self._wrong} responses; running it in a production process '
                'would expose your service to the same misbehaviour.  Run it '
                'only in a test harness.')
        if not allow_remote and host not in ('127.0.0.1', '::1', 'localhost'):
            raise self.Error(
                f'{name} refuses to bind to {host!r} without '
                'allow_remote=True.  Deliberate-misbehaviour mode is '
                'intended for local-loop tests only.')
        self.scenario = scenario
        self.host = host
        self.port = port
        self._ssl_context = ssl_context
        self._server: asyncio.base_events.Server | None = None
        self._handlers: set[asyncio.Task] = set()
        self.url: str | None = None
        self.last_result = None
        self._connection_done = asyncio.Event()

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_connection, self.host, self.port,
            ssl=self._ssl_context)
        self.port = self._server.sockets[0].getsockname()[1]
        scheme = 'https' if self._ssl_context is not None else 'http'
        self.url = f'{scheme}://{self.host}:{self.port}/'
        logger.debug('%s bound at %s', type(self).__name__, self.url)

    async def stop(self) -> None:
        """Stop accepting and cancel anything still mid-scenario."""
        for task in list(self._handlers):
            task.cancel()
        if self._handlers:
            await asyncio.gather(*self._handlers, return_exceptions=True)
            self._handlers.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        logger.debug('%s shut down', type(self).__name__)

    async def wait_for_connection_done(self, timeout: float = 5.0) -> None:
        """Block until a client has connected and the scenario finished."""
        await asyncio.wait_for(self._connection_done.wait(), timeout=timeout)

    def _connection(self, reader, writer) -> _ServerConnection:
        raise NotImplementedError

    async def _handle_connection(self, reader, writer) -> None:
        task = asyncio.current_task()
        self._handlers.add(task)
        conn = self._connection(reader, writer)
        result = conn.result
        t0 = time.monotonic()
        try:
            await conn.open()
            await conn.run(self.scenario.steps)
            if not result.terminated:
                # Out of steps: close cleanly, so a client waiting on a body
                # it will never get sees EOF instead of hanging.
                await conn._close(graceful=True)
        except Exception as exc:  # noqa: BLE001 — recorded on the result
            conn.failed(exc)
        finally:
            conn.release()
            self._handlers.discard(task)
            result.elapsed_s = time.monotonic() - t0
            self.last_result = result
            self._connection_done.set()
            if not writer.is_closing():
                writer.close()

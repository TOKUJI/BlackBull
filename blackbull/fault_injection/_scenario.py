"""The one fault-scenario model: steps, their JSON Lines form, and the driver.

Every cell walks the same steps — send, sleep, wait, expect, read, close.
What differs by protocol and role is how a send step reaches the wire (its
``wire()``, assembled here and never by the production serialisers; see
docs/about/architecture.md) and how a driver reads and writes its transport.
"""
from __future__ import annotations

import asyncio
import base64
import functools
import json
import logging
import ssl
import time
import typing
import warnings
from dataclasses import dataclass, field, fields

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SendRawBytes:
    """Send arbitrary bytes, for anything a typed step will not build.

    ``byte_interval > 0`` sends one byte at a time with that delay.
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
    uncounted.  End of stream sets ``peer_closed`` and the scenario goes on.
    """
    timeout: float = 5.0


@dataclass(frozen=True)
class Abort:
    """Hard-close the connection (``transport.abort`` → RST on Linux).

    Terminal: later steps do not run.
    """


@dataclass(frozen=True)
class HalfClose:
    """Shut down the sending direction only (FIN) and keep reading.

    Not terminal.  ``result.half_closed`` says whether the transport
    accepted it; TLS cannot.
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

    Nothing is skipped, and the scenario moves on either way.
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


#: Written on the ``HEADER`` line.  A file without it is the v1 format.
FORMAT = 2


def to_json(scenario) -> str:
    """One ``HEADER`` line carrying the scenario's fields, then one line per
    step named by its class.  Bytes are hex."""
    head = {'op': 'HEADER', 'format': FORMAT, **vars(scenario)}
    del head['steps']
    lines = [json.dumps(head)]
    for step in scenario.steps:
        record = {'op': type(step).__name__, **vars(step)}
        for name, encode in _codec(type(step))[0]:
            record[name] = encode(record[name])
        lines.append(json.dumps(record))
    return '\n'.join(lines)


def from_json(cls, src: str):
    """Parse what [`to_json`][] wrote into a *cls* scenario.

    The v1 format each vocabulary wrote before this one still reads, with a
    ``DeprecationWarning``.
    """
    head, records = {}, []
    for line in src.splitlines():
        if line.strip():
            record = json.loads(line)
            if record.get('op') == 'HEADER':
                head = record
            else:
                records.append(record)
    head.pop('op', None)
    version = head.pop('format', 1)
    if version == 1:
        warnings.warn(
            f'{cls.__name__} JSON in the v1 format is deprecated; rewrite it '
            f'with to_json(from_json(text)).  Reading v1 is removed no earlier '
            f'than v0.84.0 (and not before 2026-11-28).',
            DeprecationWarning, stacklevel=3)
        records = [_from_v1(r, *cls._v1) for r in records]
    elif version != FORMAT:
        raise ValueError(f'scenario format {version} is not one this version reads')
    kinds = _kinds(cls)
    steps = []
    for record in records:
        op = record.pop('op')
        kind = kinds.get(op)
        if kind is None:
            raise ValueError(f'unknown step op for {cls.__name__}: {op!r}')
        steps.append(_build(kind, record))
    return _build(cls, head, steps=tuple(steps))


def _out(value):
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, tuple):
        return [_out(v) for v in value]
    return value


@functools.cache
def _vocabulary(scenario_cls) -> tuple:
    hints = typing.get_type_hints(scenario_cls)
    return typing.get_args(typing.get_args(hints['steps'])[0])


@functools.cache
def _kinds(scenario_cls) -> dict:
    return {k.__name__: k for k in _vocabulary(scenario_cls)}


@functools.cache
def _codec(cls) -> tuple:
    """``(encoders, decoders)``: ``(field, fn)`` pairs for the fields JSON
    cannot carry as they are, a class's ``_codecs`` overriding."""
    hints = typing.get_type_hints(cls)
    overrides = getattr(cls, '_codecs', {})
    encoders, decoders = [], []
    for f in fields(cls):
        encode, decode = overrides.get(f.name) or (
            _encoder(hints[f.name]), _decoder(hints[f.name]))
        if encode is not None:
            encoders.append((f.name, encode))
        if decode is not None:
            decoders.append((f.name, decode))
    return tuple(encoders), tuple(decoders)


def _has_bytes(hint) -> bool:
    return hint is bytes or any(_has_bytes(a) for a in typing.get_args(hint))


def _encoder(hint):
    if hint is dict:
        return _match_out
    if hint is bytes or bytes in typing.get_args(hint):
        return _hex
    return _out if _has_bytes(hint) else None


def _hex(value):
    return None if value is None else value.hex()


def _build(cls, record: dict, **given):
    for name, decode in _codec(cls)[1]:
        if name in record:
            try:
                record[name] = decode(record[name])
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValueError(f'{cls.__name__}.{name}: {exc}') from exc
    return cls(**record, **given)


def _match_out(match: dict) -> dict:
    return {key: _tagged(value) for key, value in match.items()}


def _tagged(value):
    if isinstance(value, bytes):
        return {'hex': value.hex()}
    if isinstance(value, tuple):
        return {'tuple': [_tagged(v) for v in value]}
    if isinstance(value, list):
        return [_tagged(v) for v in value]
    return value


def _match_in(value) -> dict:
    value = {key: _untagged(v) for key, v in (value or {}).items()}
    # Older files store ``header`` as a list.
    if isinstance(value.get('header'), list):
        value['header'] = tuple(value['header'])
    return value


def _untagged(value):
    if isinstance(value, dict) and value.keys() == {'hex'}:
        return bytes.fromhex(value['hex'])
    if isinstance(value, dict) and value.keys() == {'tuple'}:
        return tuple(_untagged(v) for v in value['tuple'])
    if isinstance(value, list):
        return [_untagged(v) for v in value]
    return value


def _same(value):
    return value


def _decoder(hint):
    if hint is dict:
        return _match_in
    if hint is float:
        decode = float
    elif hint is bytes or bytes in typing.get_args(hint):
        decode = bytes.fromhex
    elif typing.get_origin(hint) is tuple:
        args = typing.get_args(hint)
        if len(args) == 2 and args[1] is Ellipsis:
            item = _decoder(args[0])
            decode = tuple if item is None else (
                lambda value: tuple(map(item, value)))
        else:
            items = [_decoder(a) or _same for a in args]
            decode = lambda value: tuple(d(v) for d, v in zip(items, value))  # noqa: E731
    else:
        return None
    return lambda value: None if value is None else decode(value)


# Unannotated: see tests/unit/test_deprecated_send_bytes_spellings.py::test_the_warning_is_attributed_to_the_callers_line.
def _deprecated_encoder(module, name):
    """The ``encode_*`` helpers each step's ``wire()`` replaced."""
    warnings.warn(
        f'{module}.{name} is deprecated; use the step\'s wire().  Removal no '
        f'earlier than v0.84.0 (and not before 2026-11-28).',
        DeprecationWarning, stacklevel=3)
    return _wire


def _wire(step) -> bytes:
    return step.wire()


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


_WRITE, _END = 'write', 'end'
#: The peer is gone, not merely done sending: the walk ends.
_PEER_GONE = (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError)


def _check_vocabulary(scenario) -> None:
    """Refuse, before any byte is written, a step the scenario cannot run."""
    for step in scenario.steps:
        if _action(type(scenario), type(step)) is None:
            raise TypeError(f'unknown step type: {type(step).__name__}')


@functools.cache
def _action(scenario_cls, step_cls):
    """What the driver does with a *step_cls* step: ``None`` for one outside
    the vocabulary, else the step base naming it, ``_END`` or ``_WRITE``."""
    if not issubclass(step_cls, _vocabulary(scenario_cls)):
        return None
    for base in (Sleep, HalfClose, ReadResponse, _WaitFor, _Expect):
        if issubclass(step_cls, base):
            return base
    if issubclass(step_cls, Abort) or getattr(step_cls, 'graceful', False):
        return _END
    return _WRITE


class _Driver:
    """Walk a scenario over one connection, folding outcomes into ``result``.

    Subclasses supply ``_write``, ``_read`` (a message, or ``None`` at end of
    stream; raises ``TimeoutError``), ``_matches``, ``_close``,
    ``_half_close``, ``_seen``, ``_completed`` and ``_ended``.
    """

    def __init__(self, scenario, result) -> None:
        self.scenario_cls = type(scenario)
        self.result = result

    async def run(self, steps) -> None:
        result = self.result
        for step in steps:
            action = _action(self.scenario_cls, type(step))
            if action is _WRITE:
                await self._write(step.wire(), getattr(step, 'byte_interval', 0.0))
            elif action is None:
                raise TypeError(f'unknown step type: {type(step).__name__}')
            elif action is Sleep:
                await asyncio.sleep(step.duration)
            elif action is HalfClose:
                result.half_closed = self._half_close()
            elif action is ReadResponse:
                try:
                    message = await self._read(step.timeout)
                except TimeoutError as exc:
                    result.timed_out = True
                    result.exception = repr(exc)
                    return
                if message is None:
                    result.peer_closed = True
                else:
                    self._seen(message, kept=True)
            elif action is _WaitFor:
                await self._wait(step)
            elif action is _Expect:
                await self._expect(step)
            else:
                await self._ended(step)
                return
            self._completed(step)

    async def _guarded_read(self, timeout: float):
        """The next message, or ``None`` once the miss is recorded."""
        try:
            message = await self._read(timeout)
        except TimeoutError:
            self.result.wait_timed_out = True
            return None
        if message is None:
            self.result.peer_closed = True
        return message

    async def _wait(self, step: _WaitFor) -> None:
        deadline = time.monotonic() + step.timeout
        while (message := await self._guarded_read(deadline - time.monotonic())) is not None:
            matched = self._matches(message, step.match)
            self._seen(message, kept=matched)
            if matched:
                return
            self.result.wait_skipped += 1

    async def _expect(self, step: _Expect) -> None:
        message = await self._guarded_read(step.timeout)
        matched = message is not None and self._matches(message, step.match)
        if message is not None:
            self._seen(message, kept=True)
        self.result.expectations.append((dict(step.match), matched))

    def _seen(self, message, *, kept: bool) -> None:
        """Record a message read; *kept* is False for one a filter passed over."""

    def _completed(self, step) -> None:
        self.result.steps_completed += 1


class _ClientDriver(_Driver):
    """The broken-client half: a step that ends the walk is not counted.

    Only ``peer_faults`` land on the result; any other exception is the
    harness's own and propagates.
    """

    peer_faults: tuple = (OSError, EOFError)

    def __init__(self, client, scenario, result) -> None:
        super().__init__(scenario, result)
        self.client = client

    def _half_close(self) -> bool:
        return half_close(self.client._raw_writer)

    async def _close(self, *, graceful: bool) -> None:
        transport = getattr(self.client._raw_writer, 'transport', None)
        if transport is not None:
            transport.abort()

    async def execute(self, scenario):
        _check_vocabulary(scenario)
        steps = scenario.steps
        t0 = time.monotonic()
        try:
            await self.run(steps)
        except _PEER_GONE:
            self.result.peer_closed = True
        except self.peer_faults as exc:
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
        # Each paced byte must reach the wire before the pause.
        writer, result = self.writer, self.result
        if byte_interval <= 0:
            writer.write(data)
            await writer.drain()
            result.server_bytes_sent += len(data)
            return
        for i in range(len(data)):
            writer.write(data[i:i + 1])
            await writer.drain()
            result.server_bytes_sent += 1
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


class _Accepted(asyncio.StreamReaderProtocol):
    """A connection accepted while its fault server stops is closed at once."""

    def __init__(self, server: _FaultServer, loop) -> None:
        super().__init__(asyncio.StreamReader(limit=2 ** 16, loop=loop),
                         server._handle_connection, loop=loop)
        self._owner = server
        self._served = False

    def connection_made(self, transport) -> None:
        if self._owner._stopping:
            transport.abort()
            return
        self._served = True
        super().connection_made(transport)

    def connection_lost(self, exc) -> None:
        if self._served:
            super().connection_lost(exc)


class _FaultServer:
    """Serve one scenario to every client that connects.

    Refuses a production context, a non-loopback bind without
    ``allow_remote=True`` and a step outside the scenario's vocabulary.
    ``stop()`` stops accepting, then cancels connections still mid-scenario
    and waits for them.  ``last_result`` is the most recently finished
    connection's result since ``start()``.
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
        _check_vocabulary(scenario)
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
        _check_vocabulary(self.scenario)
        self._stopping = False
        self.last_result = None
        self._connection_done.clear()
        loop = asyncio.get_running_loop()
        self._server = await loop.create_server(
            lambda: _Accepted(self, loop), self.host, self.port,
            ssl=self._ssl_context)
        self.port = self._server.sockets[0].getsockname()[1]
        scheme = 'https' if self._ssl_context is not None else 'http'
        self.url = f'{scheme}://{self.host}:{self.port}/'
        logger.debug('%s bound at %s', type(self).__name__, self.url)

    async def stop(self) -> None:
        """Stop accepting, then cancel and wait for every connection."""
        self._stopping = True
        if self._server is not None:
            loop = asyncio.get_running_loop()
            for sock in self._server.sockets:
                loop.remove_reader(sock.fileno())
            # A connection already taken off the listener gets its transport
            # within two loop turns; Server.close() before then leaks it.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self._server.close()
            if hasattr(self._server, 'abort_clients'):  # 3.13+
                self._server.abort_clients()
        while self._handlers:
            handlers = list(self._handlers)
            for task in handlers:
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None
        logger.debug('%s shut down', type(self).__name__)

    async def wait_for_connection_done(self, timeout: float = 5.0) -> None:
        """Block until a connection has finished since ``start()``."""
        await asyncio.wait_for(self._connection_done.wait(), timeout=timeout)

    def _connection(self, reader, writer) -> _ServerConnection:
        raise NotImplementedError

    async def _handle_connection(self, reader, writer) -> None:
        if self._stopping:
            writer.close()
            return
        task = asyncio.current_task()
        self._handlers.add(task)
        conn = self._connection(reader, writer)
        result = conn.result
        t0 = time.monotonic()
        try:
            await conn.open()
            await conn.run(self.scenario.steps)
            if not result.terminated:
                # Out of steps: the client must see EOF, not a hang.
                await conn._close(graceful=True)
        except _PEER_GONE:
            result.peer_closed = True
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

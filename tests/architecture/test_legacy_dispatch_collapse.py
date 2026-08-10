"""One dispatch path per protocol, whether or not an aggregator is present.

`_dispatch_request` used to fork: an aggregator meant `RequestActor` plus
`access_log._make_disconnect_detecting_receive`, and no aggregator meant a
direct `await self._app(...)` plus a second, near-identical disconnect wrapper
of its own.  Two implementations of the same cycle-sensitive plumbing, and the
second one was dead on every served connection — see
`test_aggregator_dispatcher_invariant.py`, which pins the reason.

Collapsing them changes two observable things on the no-aggregator path, both
asserted here:

1. **An app exception no longer escapes the actor.**  The legacy branch
   re-raised, so the exception unwound out of `HTTP1Actor.run()` and was caught
   two layers up by `ConnectionActor`; the aggregator branch swallows it and
   ends the keep-alive loop.  Now both do the latter.
2. **The access-log record is no longer built unconditionally.**  It was, only
   because the legacy branch read `log_record.mark(...)` without a None guard.
   With that branch gone, foreign ASGI apps stop paying for a record nothing
   consumes.
"""
from __future__ import annotations

import pytest

from blackbull import BlackBull
from blackbull.server.http1_actor import HTTP1Actor
from blackbull.server.recipient import AbstractReader, IncompleteReadError
from blackbull.server.sender import AbstractWriter

pytestmark = pytest.mark.asyncio


class _FakeWriter(AbstractWriter):
    def __init__(self) -> None:
        self.written = bytearray()

    async def write(self, data: bytes) -> None:
        self.written += data


class _FakeReader(AbstractReader):
    def __init__(self, data: bytes = b'') -> None:
        self._buf = bytearray(data)

    async def read(self, n: int = -1) -> bytes:
        if not self._buf:
            return b''
        chunk = bytes(self._buf[:n]) if n >= 0 else bytes(self._buf)
        del self._buf[:len(chunk)]
        return chunk

    async def readexactly(self, n: int) -> bytes:
        if len(self._buf) < n:
            raise IncompleteReadError(bytes(self._buf), n)
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    async def readuntil(self, sep: bytes = b'\n') -> bytes:
        i = self._buf.find(sep)
        if i < 0:
            raise IncompleteReadError(bytes(self._buf), len(self._buf) + 1)
        chunk = bytes(self._buf[:i + len(sep)])
        del self._buf[:i + len(sep)]
        return chunk


def _raw(method: str = 'GET', path: str = '/') -> bytes:
    return (f'{method} {path} HTTP/1.1\r\n'
            'Host: localhost:8000\r\n\r\n').encode()


async def _drive(app, raw: bytes = b'') -> _FakeWriter:
    """Serve one request with **no aggregator** — the collapsed path."""
    writer = _FakeWriter()
    actor = HTTP1Actor(
        _FakeReader(b''), writer, app, None,
        request=raw or _raw(),
        peername=('127.0.0.1', 54321),
        sockname=('0.0.0.0', 8000),
    )
    await actor.run()
    return writer


# ---------------------------------------------------------------------------
# The path still serves
# ---------------------------------------------------------------------------

async def test_a_foreign_app_is_still_served():
    """The production shape of `aggregator=None`: an ASGI app that is not a
    BlackBull instance, so there is no dispatcher and no aggregator."""
    async def foreign_app(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-length', b'2')]})
        await send({'type': 'http.response.body', 'body': b'ok'})

    writer = await _drive(foreign_app)
    assert b'200' in bytes(writer.written)
    assert bytes(writer.written).endswith(b'ok')


# ---------------------------------------------------------------------------
# Intended change 1 — a raising app is contained, not propagated
# ---------------------------------------------------------------------------

async def test_an_app_exception_does_not_escape_the_actor():
    """Uniform with the aggregator path.  The legacy branch let this unwind
    out of `run()`; a caller two layers up caught it and closed the same
    connection, so the containment point moves without the peer noticing."""
    async def raising_app(scope, receive, send):
        raise RuntimeError('handler blew up')

    await _drive(raising_app)   # must not raise


async def test_the_connection_stops_after_a_raising_app():
    """Contained is not the same as ignored — the keep-alive loop must end,
    or a broken handler would be re-entered on the next request."""
    calls = []

    async def raising_app(scope, receive, send):
        calls.append(1)
        raise RuntimeError('handler blew up')

    writer = _FakeWriter()
    # Two pipelined requests: the second must never reach the app.
    actor = HTTP1Actor(
        _FakeReader(_raw(path='/second')), writer, raising_app, None,
        request=_raw(path='/first'),
        peername=('127.0.0.1', 54321),
        sockname=('0.0.0.0', 8000),
    )
    await actor.run()
    assert calls == [1]


# ---------------------------------------------------------------------------
# Intended change 2 — the log record stops being unconditional
# ---------------------------------------------------------------------------

async def test_no_access_log_record_is_built_when_nothing_consumes_it(caplog):
    """The record was forced only by the legacy branch's unguarded
    `log_record.mark(...)`.  A foreign app with no access log and no listeners
    should not allocate one — nor the `conn.state` dict it forces.

    The access logger has to be silenced explicitly: pytest configures logging
    at DEBUG, which makes `blackbull.access` a live consumer and would leave
    this test asserting nothing.
    """
    import logging

    access = logging.getLogger('blackbull.access')
    previous = access.level
    access.setLevel(logging.WARNING)
    seen: list = []

    async def foreign_app(scope, receive, send):
        state = scope['state'] if isinstance(scope, dict) else scope.state
        seen.append('access_log' in state)
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-length', b'0')]})
        await send({'type': 'http.response.body', 'body': b''})

    try:
        await _drive(foreign_app)
    finally:
        access.setLevel(previous)
    assert seen == [False], 'a record was built for a consumer that does not exist'


async def test_the_access_log_still_reaches_a_foreign_app(caplog):
    """The other half, and the one a wrong gate breaks silently: the access
    logger is a consumer that exists whether or not there is an aggregator, so
    a foreign ASGI app must still be logged."""
    import logging

    async def foreign_app(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-length', b'0')]})
        await send({'type': 'http.response.body', 'body': b''})

    with caplog.at_level(logging.INFO, logger='blackbull.access'):
        await _drive(foreign_app, _raw(path='/logged'))
    assert any('/logged' in r.message for r in caplog.records), (
        'a foreign app with the access log enabled produced no access record')


# ---------------------------------------------------------------------------
# The duplication is actually gone
# ---------------------------------------------------------------------------

async def test_only_one_disconnect_wrapper_implementation_remains():
    """The point of the collapse.  Asserted against the source because the
    duplication was structural: two functions doing the same cycle-sensitive
    job, in different modules, that no behavioural test compared."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / 'blackbull'
    definitions = [
        f'{path.relative_to(root.parent)}:{i}'
        for path in root.rglob('*.py')
        for i, line in enumerate(path.read_text().splitlines(), 1)
        if line.lstrip().startswith('def ') and 'disconnect' in line
        and 'receive' in line
    ]
    assert len(definitions) == 1, (
        f'expected exactly one disconnect-detecting receive wrapper, '
        f'found {len(definitions)}: {definitions}')


async def test_the_surviving_wrapper_is_the_aggregator_one():
    """Not merely "one of them" — the one that runs in production."""
    from blackbull.server import access_log

    assert callable(access_log._make_disconnect_detecting_receive)
    assert not hasattr(HTTP1Actor, '_make_legacy_disconnect_receive')


# ---------------------------------------------------------------------------
# HTTP/2 — the same collapse, because the fork was the same fork
# ---------------------------------------------------------------------------
#
# `_spawn_stream_task` had the matching pair: a `StreamActor` when an
# aggregator was present, and a bare `_run_with_log(self.app(...))` task when
# it was not.  Two intended changes on the no-aggregator path, both asserted
# below:
#
# 1. **A failing stream is now reset.**  `_run_with_log` logged the exception
#    and returned, leaving the peer with a stream that simply stopped.
#    `StreamActor` sends RST_STREAM INTERNAL_ERROR, which is what RFC 9113
#    gives a peer to act on.
# 2. **The log record follows the same consumer gate as H/1.**  It was forced
#    whenever the aggregator was absent, only because `_run_with_log`'s
#    `finally` emitted without a None check.

def _h2_actor(app):
    """An HTTP2Actor with **no aggregator** and a mocked frame sink."""
    from unittest.mock import AsyncMock, MagicMock

    from blackbull.server.http2_actor import HTTP2Actor
    from blackbull.server.sender import AsyncioWriter

    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.close = MagicMock()
    actor = HTTP2Actor(None, AsyncioWriter(writer), app, aggregator=None)
    actor.send_frame = AsyncMock()
    return actor


def _h2_headers_frame(stream_id: int = 1) -> bytes:
    from hpack import Encoder

    from blackbull.protocol.frame_types import FrameTypes, HeaderFrameFlags

    block = Encoder().encode([
        (b':method', b'GET'), (b':path', b'/'),
        (b':scheme', b'http'), (b':authority', b'localhost'),
    ])
    flags = HeaderFrameFlags.END_HEADERS | HeaderFrameFlags.END_STREAM
    return (len(block).to_bytes(3, 'big') + FrameTypes.HEADERS
            + bytes([flags]) + stream_id.to_bytes(4, 'big') + block)


async def _drive_h2(app):
    from unittest.mock import AsyncMock

    actor = _h2_actor(app)
    actor.receive = AsyncMock(side_effect=[_h2_headers_frame(), None])
    await actor.run()
    return actor


async def test_h2_serves_a_foreign_app_without_an_aggregator():
    served = []

    async def foreign_app(scope, receive, send):
        served.append(True)
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-length', b'0')]})
        await send({'type': 'http.response.body', 'body': b''})

    await _drive_h2(foreign_app)
    assert served == [True]


async def test_h2_resets_the_stream_when_a_foreign_app_raises():
    """The intended change: the peer is told, instead of being left with a
    stream that stopped for no stated reason."""
    from blackbull.protocol.frame_types import ErrorCodes

    async def raising_app(scope, receive, send):
        raise RuntimeError('stream handler blew up')

    actor = await _drive_h2(raising_app)

    reset = [c for c in actor.send_frame.await_args_list
             if getattr(c.args[0], 'error_code', None) == ErrorCodes.INTERNAL_ERROR]
    assert reset, 'a raising stream must be reset with INTERNAL_ERROR'


async def test_h2_builds_no_log_record_when_nothing_consumes_it():
    import logging

    seen: list = []

    async def foreign_app(scope, receive, send):
        state = scope['state'] if isinstance(scope, dict) else scope.state
        seen.append('access_log' in state)
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-length', b'0')]})
        await send({'type': 'http.response.body', 'body': b''})

    access = logging.getLogger('blackbull.access')
    previous = access.level
    access.setLevel(logging.WARNING)
    try:
        await _drive_h2(foreign_app)
    finally:
        access.setLevel(previous)
    assert seen == [False]


async def test_the_h2_legacy_task_wrapper_is_gone():
    """`_run_with_log` existed only for the branch that no longer exists."""
    from blackbull.server import server as server_module

    assert not hasattr(server_module, '_run_with_log')

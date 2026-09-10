"""Lifecycle parity for HTTP/2 field sections split over CONTINUATION."""

import asyncio

import pytest
from hpack import Encoder
from hypothesis import HealthCheck, given, settings, strategies as st

from blackbull.connection import Connection
from blackbull.env import reset_settings_cache
from blackbull.event import EventDispatcher
from blackbull.event_aggregator import EventAggregator
from blackbull.protocol.frame_types import (
    DataFrameFlags,
    FrameTypes,
    HeaderFrameFlags,
)
from blackbull.protocol.stream import StreamState
from blackbull.server.http2_actor import HTTP2Actor
from blackbull.server.recipient import AbstractReader
from blackbull.server.sender import AbstractWriter


class _Reader(AbstractReader):
    def __init__(self, data: bytes):
        self._data = bytearray(data)

    async def read(self, n: int) -> bytes:
        chunk = bytes(self._data[:n])
        del self._data[:n]
        return chunk

    async def readuntil(self, separator: bytes) -> bytes:
        offset = self._data.find(separator)
        if offset < 0:
            raise asyncio.IncompleteReadError(bytes(self._data), None)
        end = offset + len(separator)
        chunk = bytes(self._data[:end])
        del self._data[:end]
        return chunk

    async def readexactly(self, n: int) -> bytes:
        if len(self._data) < n:
            raise asyncio.IncompleteReadError(bytes(self._data), n)
        chunk = bytes(self._data[:n])
        del self._data[:n]
        return chunk


class _Writer(AbstractWriter):
    def __init__(self):
        self.written = bytearray()
        self.closed = False

    async def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def close(self) -> None:
        self.closed = True


def _frame(kind: FrameTypes, flags: int, stream_id: int, payload: bytes) -> bytes:
    return (
        len(payload).to_bytes(3, 'big')
        + kind
        + bytes([flags])
        + stream_id.to_bytes(4, 'big')
        + payload
    )


def _field_section(
    block: bytes,
    *,
    stream_id: int,
    end_stream: bool,
    cuts: tuple[int, ...],
) -> bytes:
    """Encode one HEADERS field section using the supplied split positions."""
    end_stream_flag = HeaderFrameFlags.END_STREAM if end_stream else 0
    if not cuts:
        return _frame(
            FrameTypes.HEADERS,
            HeaderFrameFlags.END_HEADERS | end_stream_flag,
            stream_id,
            block,
        )

    boundaries = (0, *cuts, len(block))
    raw = _frame(
        FrameTypes.HEADERS,
        end_stream_flag,
        stream_id,
        block[boundaries[0]:boundaries[1]],
    )
    for index in range(1, len(boundaries) - 1):
        flags = (
            HeaderFrameFlags.END_HEADERS
            if index == len(boundaries) - 2
            else 0
        )
        raw += _frame(
            FrameTypes.CONTINUATION,
            flags,
            stream_id,
            block[boundaries[index]:boundaries[index + 1]],
        )
    return raw


def _request_block(*, with_body: bool, path: bytes = b'/continuation') -> bytes:
    return Encoder().encode([
        (b':method', b'POST'),
        (b':path', path),
        (b':scheme', b'https'),
        (b':authority', b'example.com'),
        (b'content-length', b'2' if with_body else b'0'),
    ])


def _target_view(target: Connection | dict) -> tuple[str, str, str]:
    if isinstance(target, Connection):
        return target.type, target.method, target.path
    return target['type'], target['method'], target['path']


async def _run_request(
    monkeypatch,
    *,
    with_body: bool,
    force_asgi: bool,
    cuts: tuple[int, ...],
    path: bytes = b'/continuation',
) -> dict:
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', '1' if force_asgi else '0')
    reset_settings_cache()

    block = _request_block(with_body=with_body, path=path)
    raw = _field_section(
        block,
        stream_id=1,
        end_stream=not with_body,
        cuts=cuts,
    )
    if with_body:
        raw += _frame(FrameTypes.DATA, DataFrameFlags.END_STREAM, 1, b'ok')

    calls = []
    events = []
    states = []
    actor = None

    async def app(target, receive, send):
        assert actor is not None
        calls.append(_target_view(target))
        states.append(actor.root_stream.children[1].state)
        events.append(await receive())
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})

    writer = _Writer()
    actor = HTTP2Actor(
        _Reader(raw),
        writer,
        app,
        EventAggregator(EventDispatcher()),
    )
    actor._frame_yield_every = 1
    await actor.run()
    event = events[0]
    return {
        'calls': calls,
        'state_at_dispatch': states[0],
        'event': {
            'type': event['type'],
            'body': event.get('body'),
            'more_body': event.get('more_body'),
        },
        'connection_closed': writer.closed,
        'goaway_sent': actor._goaway_sent,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize('force_asgi', [False, True], ids=['native', 'asgi'])
@pytest.mark.parametrize('with_body', [False, True], ids=['empty', 'data'])
async def test_split_initial_headers_preserve_request_lifecycle(
    monkeypatch,
    force_asgi: bool,
    with_body: bool,
) -> None:
    block = _request_block(with_body=with_body)
    single = await _run_request(
        monkeypatch,
        with_body=with_body,
        force_asgi=force_asgi,
        cuts=(),
    )
    split = await _run_request(
        monkeypatch,
        with_body=with_body,
        force_asgi=force_asgi,
        cuts=(len(block) // 2,),
    )

    assert split == single
    assert split['event'] == {
        'type': 'http.request',
        'body': b'ok' if with_body else b'',
        'more_body': False,
    }
    assert split['state_at_dispatch'] is (
        StreamState.OPEN if with_body else StreamState.HALF_CLOSED_REMOTE
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('force_asgi', [False, True], ids=['native', 'asgi'])
async def test_every_initial_header_split_position_preserves_lifecycle(
    monkeypatch,
    force_asgi: bool,
) -> None:
    block = _request_block(with_body=False)
    single = await _run_request(
        monkeypatch,
        with_body=False,
        force_asgi=force_asgi,
        cuts=(),
    )

    for cut in range(1, len(block)):
        split = await _run_request(
            monkeypatch,
            with_body=False,
            force_asgi=force_asgi,
            cuts=(cut,),
        )
        assert split == single, f'field block diverged at split position {cut}'


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    force_asgi=st.booleans(),
    with_body=st.booleans(),
    path_suffix=st.text(
        alphabet='abcdefghijklmnopqrstuvwxyz0123456789-',
        min_size=1,
        max_size=16,
    ),
    cut_seed=st.integers(min_value=1, max_value=10_000),
)
def test_generated_legal_header_splits_preserve_lifecycle(
    monkeypatch,
    force_asgi: bool,
    with_body: bool,
    path_suffix: str,
    cut_seed: int,
) -> None:
    path = f'/{path_suffix}'.encode()
    block = _request_block(with_body=with_body, path=path)
    cut = 1 + cut_seed % (len(block) - 1)

    async def compare() -> None:
        single = await _run_request(
            monkeypatch,
            with_body=with_body,
            force_asgi=force_asgi,
            cuts=(),
            path=path,
        )
        split = await _run_request(
            monkeypatch,
            with_body=with_body,
            force_asgi=force_asgi,
            cuts=(cut,),
            path=path,
        )
        assert split == single

    asyncio.run(compare())


@pytest.mark.asyncio
async def test_multiple_continuations_preserve_request_lifecycle(monkeypatch) -> None:
    block = _request_block(with_body=True)
    single = await _run_request(
        monkeypatch,
        with_body=True,
        force_asgi=False,
        cuts=(),
    )
    split = await _run_request(
        monkeypatch,
        with_body=True,
        force_asgi=False,
        cuts=(len(block) // 3, 2 * len(block) // 3),
    )

    assert split == single


async def _run_trailers(monkeypatch, *, force_asgi: bool, split: bool) -> dict:
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', '1' if force_asgi else '0')
    reset_settings_cache()

    encoder = Encoder()
    initial = encoder.encode([
        (b':method', b'POST'),
        (b':path', b'/trailers'),
        (b':scheme', b'https'),
        (b':authority', b'example.com'),
    ])
    trailers = encoder.encode([(b'x-review', b'done')])
    raw = _field_section(
        initial,
        stream_id=1,
        end_stream=False,
        cuts=(),
    )
    raw += _field_section(
        trailers,
        stream_id=1,
        end_stream=True,
        cuts=(len(trailers) // 2,) if split else (),
    )

    calls = []
    events = []

    async def app(target, receive, send):
        calls.append(_target_view(target))
        events.append(await receive())
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})

    writer = _Writer()
    actor = HTTP2Actor(
        _Reader(raw),
        writer,
        app,
        EventAggregator(EventDispatcher()),
    )
    await actor.run()
    return {
        'calls': calls,
        'events': events,
        'connection_closed': writer.closed,
        'goaway_sent': actor._goaway_sent,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize('force_asgi', [False, True], ids=['native', 'asgi'])
async def test_split_trailers_finish_existing_request_once(
    monkeypatch,
    force_asgi: bool,
) -> None:
    single = await _run_trailers(
        monkeypatch,
        force_asgi=force_asgi,
        split=False,
    )
    split = await _run_trailers(
        monkeypatch,
        force_asgi=force_asgi,
        split=True,
    )

    assert split == single
    assert len(split['calls']) == 1
    assert split['events'] == [
        {'type': 'http.request', 'body': b'', 'more_body': False},
    ]


@pytest.mark.asyncio
async def test_continuation_for_another_open_stream_is_connection_error() -> None:
    encoder = Encoder()
    blocks = [
        encoder.encode([
            (b':method', b'GET'),
            (b':path', path),
            (b':scheme', b'https'),
            (b':authority', b'example.com'),
        ])
        for path in (b'/one', b'/three', b'/five')
    ]
    raw = _field_section(
        blocks[0], stream_id=1, end_stream=False, cuts=())
    raw += _field_section(
        blocks[1], stream_id=3, end_stream=False, cuts=())
    cut = len(blocks[2]) // 2
    raw += _frame(FrameTypes.HEADERS, 0, 5, blocks[2][:cut])
    raw += _frame(
        FrameTypes.CONTINUATION,
        HeaderFrameFlags.END_HEADERS,
        3,
        blocks[2][cut:],
    )

    calls = []

    async def app(target, _receive, send):
        calls.append(_target_view(target))
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})

    writer = _Writer()
    actor = HTTP2Actor(
        _Reader(raw),
        writer,
        app,
        EventAggregator(EventDispatcher()),
    )
    await actor.run()

    assert actor._goaway_sent
    assert writer.closed
    # A connection-level framing failure is fatal: work accepted earlier in
    # the same loop turn must not start producing output after GOAWAY/close.
    # Graceful peer GOAWAY and EOF have separate positive controls which let
    # already-running responses drain.
    assert calls == []
    assert actor._stream_tasks == {}
    assert actor._senders == {}
    assert actor._recipients == {}
    assert actor.root_stream.children == {}
    assert actor._active_stream_count == 0

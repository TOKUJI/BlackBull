"""HTTP/2 stream ownership is released through one idempotent lifecycle."""

import asyncio
from http import HTTPStatus
from types import SimpleNamespace

import pytest
from hpack import Decoder, Encoder

from blackbull.connection import Connection
from blackbull.headers import Headers
from blackbull.protocol.frame_types import (
    DataFrameFlags,
    ErrorCodes,
    FrameTypes,
    HeaderFrameFlags,
)
from blackbull.server.http2_actor import HTTP2Actor, _CLOSED_STREAMS_CAP
from blackbull.server.http2_ws import HTTP2WSReader
from blackbull.server.recipient import AbstractReader
from blackbull.server.response import PriorityUpdateResponder, RstStreamResponder
from blackbull.server.sender import AbstractWriter


def _wire(frame_type: FrameTypes, flags: int, stream_id: int, payload: bytes) -> bytes:
    return (
        len(payload).to_bytes(3, "big")
        + frame_type
        + bytes([flags])
        + stream_id.to_bytes(4, "big")
        + payload
    )


def _headers(stream_id: int, *, end_stream: bool = True) -> bytes:
    block = Encoder().encode(
        [
            (b":method", b"GET"),
            (b":path", b"/"),
            (b":scheme", b"https"),
            (b":authority", b"example.test"),
        ]
    )
    flags = int(HeaderFrameFlags.END_HEADERS)
    if end_stream:
        flags |= int(HeaderFrameFlags.END_STREAM)
    return _wire(FrameTypes.HEADERS, flags, stream_id, block)


class _Reader(AbstractReader):
    def __init__(self, data: bytes) -> None:
        self.data = bytearray(data)

    async def read(self, n: int) -> bytes:
        return await self.readexactly(n)

    async def readuntil(self, separator: bytes) -> bytes:
        raise NotImplementedError

    async def readexactly(self, n: int) -> bytes:
        if len(self.data) < n:
            raise asyncio.IncompleteReadError(bytes(self.data), n)
        result = bytes(self.data[:n])
        del self.data[:n]
        return result


class _YieldingReader(_Reader):
    async def readexactly(self, n: int) -> bytes:
        await asyncio.sleep(0)
        return await super().readexactly(n)


class _Writer(AbstractWriter):
    def __init__(self) -> None:
        self.written = bytearray()

    async def write(self, data: bytes) -> None:
        self.written.extend(data)


class _FailingWriter(_Writer):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    async def write(self, data: bytes) -> None:
        raise OSError("transport write failed")

    async def close(self) -> None:
        self.closed = True


class _BlockingPushWriter(_Writer):
    def __init__(self) -> None:
        super().__init__()
        self.promise_written = asyncio.Event()
        self.release_promise = asyncio.Event()

    async def write(self, data: bytes) -> None:
        self.written.extend(data)
        if data[3:4] == FrameTypes.PUSH_PROMISE:
            self.promise_written.set()
            await self.release_promise.wait()


class _FailingResetControlSender:
    def __init__(self) -> None:
        self.promise_written = asyncio.Event()
        self.release_promise = asyncio.Event()
        self.reset_attempts = []

    async def __call__(self, frame) -> None:
        if frame.FrameType() == FrameTypes.PUSH_PROMISE:
            self.promise_written.set()
            await self.release_promise.wait()
        elif frame.FrameType() == FrameTypes.RST_STREAM:
            self.reset_attempts.append(frame.stream_id)
            raise OSError("reset transport write failed")


class _Recipient:
    def __init__(self, credit: int = 0) -> None:
        self.disconnected = False
        self.credit = credit
        self.credit_takes = 0

    def put_disconnect(self) -> None:
        self.disconnected = True

    def put_DATAFrame(self, frame) -> bool:
        return True

    def take_uncredited(self) -> int:
        self.credit_takes += 1
        credit, self.credit = self.credit, 0
        return credit


def _actor(
    *, reader: AbstractReader | None = None, writer: AbstractWriter | None = None, app=None
) -> HTTP2Actor:
    async def default_app(conn, receive, send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    return HTTP2Actor(reader, writer or _Writer(), app or default_app, aggregator=None)


def _frames(actor: HTTP2Actor) -> list:
    data = bytes(actor._writer.written)
    frames = []
    while data:
        length = int.from_bytes(data[:3], "big")
        end = 9 + length
        frames.append(actor.factory.load(data[:end]))
        data = data[end:]
    return frames


def _live_ownership(actor: HTTP2Actor) -> tuple[int, int, int, int, int, int]:
    return (
        len(actor.root_stream.children),
        len(actor._stream_tasks),
        len(actor._senders),
        len(actor._recipients),
        actor._active_stream_count,
        actor._ws_stream_count,
    )


async def _install_blocked_stream(actor: HTTP2Actor, stream_id: int, credit: int = 0):
    stream = actor.root_stream.add_child(stream_id)
    stream.on_headers_received(end_stream=False)
    recipient = _Recipient(credit)
    sender = actor.make_sender(stream_id)
    sender.connection_window_size = 0
    sender.stream_window_size = 0
    producer_done = asyncio.Event()

    async def producer() -> None:
        try:
            await sender._write_data(b"blocked", end_stream=True)
        finally:
            producer_done.set()

    task = asyncio.create_task(producer())
    await asyncio.sleep(0)
    actor._recipients[stream_id] = recipient
    actor._stream_tasks[stream_id] = task
    actor._active_stream_count = 1
    return recipient, sender, task, producer_done


@pytest.mark.asyncio
@pytest.mark.parametrize("peer_reset", [False, True])
async def test_reset_retires_every_owner_once_and_unparks_producer(peer_reset: bool):
    actor = _actor()
    recipient, sender, task, producer_done = await _install_blocked_stream(
        actor, 1, credit=7
    )
    reset = actor.factory.rst_stream(1, ErrorCodes.CANCEL)

    if peer_reset:
        await RstStreamResponder(reset).respond(actor)
    else:
        await actor.send_frame(reset)
    await asyncio.wait_for(producer_done.wait(), timeout=1)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert task.cancelled()
    assert sender._closed
    assert recipient.disconnected
    assert recipient.credit_takes == 1
    assert _live_ownership(actor) == (0, 0, 0, 0, 0, 0)
    assert actor._closed_streams[1] is True

    actor._make_done_cb(1)(task)
    assert recipient.credit_takes == 1
    assert _live_ownership(actor) == (0, 0, 0, 0, 0, 0)


@pytest.mark.asyncio
async def test_spawn_failure_releases_prepared_stream_without_creating_work():
    actor = _actor()
    stream = actor.root_stream.add_child(1)
    stream.on_headers_received(end_stream=True)
    recipient = _Recipient()
    actor._recipients[1] = recipient
    sender = actor.make_sender(1)

    task_group = asyncio.TaskGroup()
    await task_group.__aenter__()
    await task_group.__aexit__(None, None, None)

    conn = Connection(
        method="GET", path="/", raw_path=b"/", headers=Headers([])
    )
    actor._spawn_stream_task(task_group, 1, conn, recipient, sender, None)

    assert recipient.disconnected
    assert sender._closed
    assert _live_ownership(actor) == (0, 0, 0, 0, 0, 0)


@pytest.mark.asyncio
async def test_local_refusal_before_task_creation_leaves_only_closed_history():
    calls = 0

    async def app(conn, receive, send) -> None:
        nonlocal calls
        calls += 1

    actor = _actor(reader=_Reader(_headers(1)), app=app)
    actor.max_concurrent_streams = 0
    await actor.run()

    resets = [frame for frame in _frames(actor) if frame.FrameType() == FrameTypes.RST_STREAM]
    assert calls == 0
    assert len(resets) == 1
    assert _live_ownership(actor) == (0, 0, 0, 0, 0, 0)
    assert actor._closed_streams[1] is True
    assert actor._closed_peer_high_water == 1


@pytest.mark.asyncio
async def test_ownerless_local_reset_records_exact_id_without_advancing_watermark():
    calls = 0

    async def app(conn, receive, send) -> None:
        nonlocal calls
        calls += 1
        await send(b"ok")

    actor = _actor(app=app)
    await actor.send_frame(actor.factory.rst_stream(99, ErrorCodes.CANCEL))

    assert actor._closed_streams[99] is True
    assert actor._closed_peer_high_water == 0

    actor._reader = _YieldingReader(_headers(1) + _headers(99))
    await actor.run()

    assert calls == 1
    assert actor._closed_peer_high_water == 1
    assert [
        frame.error_code
        for frame in _frames(actor)
        if frame.FrameType() == FrameTypes.GOAWAY
    ] == [ErrorCodes.STREAM_CLOSED]


@pytest.mark.asyncio
async def test_request_end_stream_does_not_end_response_task():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def app(conn, receive, send) -> None:
        entered.set()
        await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    actor = _actor(app=app)
    frames = [_headers(1), None]

    async def receive():
        value = frames.pop(0)
        if value is None:
            await entered.wait()
        return value

    actor.receive = receive
    run = asyncio.create_task(actor.run())
    await asyncio.wait_for(entered.wait(), timeout=1)

    assert _live_ownership(actor) == (1, 1, 1, 1, 1, 0)
    release.set()
    await asyncio.wait_for(run, timeout=1)
    assert _live_ownership(actor) == (0, 0, 0, 0, 0, 0)


@pytest.mark.asyncio
async def test_reset_cancels_task_waiting_for_stream_semaphore():
    calls = 0

    async def app(conn, receive, send) -> None:
        nonlocal calls
        calls += 1

    headers = _headers(1, end_stream=False)
    reset = _wire(FrameTypes.RST_STREAM, 0, 1, int(ErrorCodes.CANCEL).to_bytes(4, "big"))
    actor = _actor(reader=_Reader(headers + reset), app=app)
    actor._stream_semaphore = asyncio.Semaphore(0)

    await asyncio.wait_for(actor.run(), timeout=1)

    assert calls == 0
    assert _live_ownership(actor) == (0, 0, 0, 0, 0, 0)


@pytest.mark.asyncio
async def test_request_timeout_uses_reset_retirement():
    async def app(conn, receive, send) -> None:
        await asyncio.Event().wait()

    actor = _actor(reader=_Reader(_headers(1)), app=app)
    actor._request_timeout = 0.01
    await asyncio.wait_for(actor.run(), timeout=1)

    resets = [frame for frame in _frames(actor) if frame.FrameType() == FrameTypes.RST_STREAM]
    assert len(resets) == 1
    assert resets[0].error_code == ErrorCodes.CANCEL
    assert _live_ownership(actor) == (0, 0, 0, 0, 0, 0)


@pytest.mark.asyncio
async def test_peer_goaway_allows_an_accepted_stream_to_finish_response():
    responded = asyncio.Event()

    async def app(conn, receive, send) -> None:
        await send(b"response")
        responded.set()

    goaway = _wire(
        FrameTypes.GOAWAY,
        0,
        0,
        (1).to_bytes(4, "big") + int(ErrorCodes.NO_ERROR).to_bytes(4, "big"),
    )
    actor = _actor(reader=_Reader(_headers(1) + goaway), app=app)
    await asyncio.wait_for(actor.run(), timeout=1)

    assert responded.is_set()
    data = [frame for frame in _frames(actor) if frame.FrameType() == FrameTypes.DATA]
    assert data and data[-1].payload == b"response"
    assert _live_ownership(actor) == (0, 0, 0, 0, 0, 0)


@pytest.mark.asyncio
async def test_fatal_connection_error_retires_blocked_stream_ownership():
    actor = _actor()
    _, _, task, producer_done = await _install_blocked_stream(actor, 1, credit=7)

    await actor._connection_error(ErrorCodes.PROTOCOL_ERROR, "fatal framing error")
    await asyncio.wait_for(producer_done.wait(), timeout=1)
    await asyncio.sleep(0)

    assert task.cancelled()
    assert [frame for frame in _frames(actor) if frame.FrameType() == FrameTypes.GOAWAY]
    assert _live_ownership(actor) == (0, 0, 0, 0, 0, 0)


@pytest.mark.asyncio
async def test_fatal_error_retires_ownership_when_goaway_write_fails():
    writer = _FailingWriter()
    actor = _actor(writer=writer)
    _, _, task, producer_done = await _install_blocked_stream(actor, 1, credit=7)

    async def fail_control_write(frame) -> None:
        raise OSError("transport write failed")

    actor._control_sender = fail_control_write

    await actor._connection_error(ErrorCodes.PROTOCOL_ERROR, "fatal framing error")
    await asyncio.wait_for(producer_done.wait(), timeout=1)
    await asyncio.sleep(0)

    assert task.cancelled()
    assert writer.closed
    assert actor._goaway_sent
    assert _live_ownership(actor) == (0, 0, 0, 0, 0, 0)


@pytest.mark.asyncio
async def test_local_reset_retires_before_a_failed_wire_write():
    actor = _actor()
    recipient, sender, task, producer_done = await _install_blocked_stream(
        actor, 1, credit=7
    )

    async def fail_control_write(frame) -> None:
        raise OSError("transport write failed")

    actor._control_sender = fail_control_write

    with pytest.raises(OSError, match="transport write failed"):
        await actor.send_frame(actor.factory.rst_stream(1, ErrorCodes.CANCEL))
    await asyncio.wait_for(producer_done.wait(), timeout=1)
    await asyncio.sleep(0)

    assert task.cancelled()
    assert sender._closed
    assert recipient.disconnected
    assert _live_ownership(actor) == (0, 0, 0, 0, 0, 0)
    assert actor._closed_streams[1] is True


@pytest.mark.asyncio
async def test_late_data_on_closed_stream_returns_connection_credit_without_owner():
    actor = _actor()
    actor._mark_closed(1, via_rst=False)
    late = _wire(FrameTypes.DATA, 0, 1, b"late")
    actor._reader = _Reader(late)

    await actor.run()
    await asyncio.sleep(0)

    resets = [
        frame for frame in _frames(actor)
        if frame.FrameType() == FrameTypes.RST_STREAM
    ]
    updates = [
        frame.window_size
        for frame in _frames(actor)
        if frame.FrameType() == FrameTypes.WINDOW_UPDATE
        and frame.stream_id == 0
    ]
    assert len(resets) == 1
    assert resets[0].error_code == ErrorCodes.STREAM_CLOSED
    assert len(b"late") in updates
    assert _live_ownership(actor) == (0, 0, 0, 0, 0, 0)


@pytest.mark.asyncio
async def test_retired_sender_drops_standalone_websocket_headers():
    actor = _actor()
    retired = actor.make_sender(1)
    sibling = actor.make_sender(3)
    headers = [(b"x-retired-stream", b"must-not-enter-hpack")]
    retired.retire_stream()

    await retired.send_response_headers(HTTPStatus.OK, headers)
    await sibling.send_response_headers(HTTPStatus.OK, headers)

    wire = bytes(actor._writer.written)
    length = int.from_bytes(wire[:3], "big")
    assert int.from_bytes(wire[5:9], "big") == 3
    decoded = Decoder().decode(wire[9 : 9 + length], raw=True)
    assert (b"x-retired-stream", b"must-not-enter-hpack") in decoded


@pytest.mark.asyncio
async def test_retirement_during_body_credit_wait_does_not_encode_unsent_trailers():
    actor = _actor()
    retired = actor.make_sender(1)
    sibling = actor.make_sender(3)
    trailer = (b"x-retired-trailer", b"must-not-enter-hpack")

    await retired(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [],
            "trailers": True,
        }
    )
    await retired(
        {"type": "http.response.body", "body": b"x", "more_body": True}
    )
    auto_flush = retired._auto_flush_task
    assert auto_flush is not None
    auto_flush.cancel()
    await asyncio.gather(auto_flush, return_exceptions=True)
    retired.connection_window_size = 0
    trailer_task = asyncio.create_task(
        retired(
            {
                "type": "http.response.trailers",
                "headers": [trailer],
                "more_trailers": False,
            }
        )
    )
    while retired._window_open is None:
        await asyncio.sleep(0)

    retired.retire_stream()
    await trailer_task
    await sibling.send_response_headers(HTTPStatus.OK, [trailer])

    wire = bytes(actor._writer.written)
    decoder = Decoder()
    sibling_headers = None
    while wire:
        length = int.from_bytes(wire[:3], "big")
        end = 9 + length
        if wire[3] == FrameTypes.HEADERS[0]:
            fields = decoder.decode(wire[9:end], raw=True)
            if int.from_bytes(wire[5:9], "big") == 3:
                sibling_headers = fields
        wire = wire[end:]
    assert sibling_headers is not None
    assert trailer in sibling_headers


@pytest.mark.asyncio
@pytest.mark.parametrize("recipient_kind", ["http", "websocket"])
async def test_consumed_credit_survives_reset_during_stream_window_write(
    recipient_kind: str,
):
    actor = _actor()
    stream = actor.root_stream.add_child(1)
    stream.on_headers_received(end_stream=False)
    actor.make_sender(1)
    frame = actor.factory.load(_wire(FrameTypes.DATA, 0, 1, b"ab"))

    if recipient_kind == "http":
        recipient = actor._make_stream_recipient(1)
        assert recipient.put_DATAFrame(frame)

        async def consume() -> None:
            await recipient()
    else:
        recipient = HTTP2WSReader(
            max_buffer=1,
            credit_callback=actor._make_consume_credit_callback(1),
        )
        assert not recipient.put_DATAFrame(frame)

        async def consume() -> None:
            await recipient.readexactly(2)

    actor._recipients[1] = recipient
    stream_update_started = asyncio.Event()
    release_stream_update = asyncio.Event()
    sent_updates = []

    async def send_control(outbound) -> None:
        if (outbound.FrameType() == FrameTypes.WINDOW_UPDATE
                and outbound.stream_id == 1):
            stream_update_started.set()
            await release_stream_update.wait()
        if outbound.FrameType() == FrameTypes.WINDOW_UPDATE:
            sent_updates.append((outbound.stream_id, outbound.window_size))

    actor._control_sender = send_control
    consumer_task = asyncio.create_task(consume())
    actor._stream_tasks[1] = consumer_task
    actor._active_stream_count = 1
    await asyncio.wait_for(stream_update_started.wait(), timeout=1)
    assert sent_updates == [(0, frame.length)]

    actor._retire_stream(1, via_rst=True)
    release_stream_update.set()
    await asyncio.gather(consumer_task, return_exceptions=True)
    for _ in range(4):
        await asyncio.sleep(0)

    assert consumer_task.cancelled()
    assert sent_updates.count((0, frame.length)) == 1
    assert not [item for item in sent_updates if item[0] == 1]
    assert not actor._credit_flush_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("fatal", [False, True])
async def test_connection_teardown_cancels_pending_credit_before_goaway(fatal: bool):
    actor = _actor()
    credit_started = asyncio.Event()
    release_credit = asyncio.Event()
    sent_types = []

    async def send_control(frame) -> None:
        if frame.FrameType() == FrameTypes.WINDOW_UPDATE:
            credit_started.set()
            await release_credit.wait()
        sent_types.append(frame.FrameType())

    actor._control_sender = send_control
    actor._release_recipient_credit(_Recipient(credit=7))
    await asyncio.wait_for(credit_started.wait(), timeout=1)
    credit_task = next(iter(actor._credit_flush_tasks))

    if fatal:
        await actor._connection_error(ErrorCodes.PROTOCOL_ERROR, "fatal")
    else:
        await actor._close_connection(ErrorCodes.NO_ERROR)
    owned_after_teardown = credit_task in actor._credit_flush_tasks
    release_credit.set()
    await asyncio.gather(credit_task, return_exceptions=True)
    await asyncio.sleep(0)

    assert credit_task.cancelled()
    assert not owned_after_teardown
    assert sent_types == [FrameTypes.GOAWAY]
    assert not actor._credit_flush_tasks


@pytest.mark.asyncio
async def test_push_closed_watermark_does_not_close_lower_peer_stream():
    calls = 0

    async def app(conn, receive, send) -> None:
        nonlocal calls
        calls += 1
        await send(b"ok")

    actor = _actor(app=app)
    actor._mark_closed(4, via_rst=False)
    actor._last_peer_stream_id = 1
    actor._reader = _Reader(_headers(3))
    await actor.run()

    assert calls == 1
    assert not [frame for frame in _frames(actor) if frame.FrameType() == FrameTypes.GOAWAY]


@pytest.mark.asyncio
async def test_priority_update_for_closed_stream_creates_no_owner():
    actor = _actor()
    actor._mark_closed(3, via_rst=False)
    raw = _wire(FrameTypes.PRIORITY_UPDATE, 0, 0, (3).to_bytes(4, "big") + b"u=1")
    frame = actor.factory.load(raw)

    await PriorityUpdateResponder(frame).respond(actor)

    assert actor.find_stream(3) is None
    assert _live_ownership(actor) == (0, 0, 0, 0, 0, 0)


@pytest.mark.asyncio
async def test_idle_priority_reset_does_not_close_lower_legal_peer_stream():
    calls = 0

    async def app(conn, receive, send) -> None:
        nonlocal calls
        calls += 1
        await send(b"ok")

    priority_update = _wire(
        FrameTypes.PRIORITY_UPDATE, 0, 0, (99).to_bytes(4, "big") + b"u=1"
    )
    self_dependent_priority = _wire(
        FrameTypes.PRIORITY, 0, 99, (99).to_bytes(4, "big") + b"\x00"
    )
    actor = _actor(
        reader=_Reader(priority_update + self_dependent_priority + _headers(1)),
        app=app,
    )

    await actor.run()

    assert calls == 1
    assert actor._closed_streams[99] is True
    assert actor._closed_peer_high_water == 1
    assert not [
        frame for frame in _frames(actor) if frame.FrameType() == FrameTypes.GOAWAY
    ]


@pytest.mark.asyncio
async def test_push_stream_joins_and_leaves_common_ownership_registry():
    calls = []

    async def app(conn, receive, send) -> None:
        path = conn["path"] if isinstance(conn, dict) else conn.path
        calls.append(path)
        if path == "/":
            await send({"type": "http.response.push", "path": "/asset", "headers": []})
        await send(b"ok")

    actor = _actor(reader=_Reader(_headers(1)), app=app)
    await actor.run()

    assert sorted(calls) == ["/", "/asset"]
    assert _live_ownership(actor) == (0, 0, 0, 0, 0, 0)
    assert actor._closed_peer_high_water == 1
    assert actor._closed_push_high_water == 2


@pytest.mark.asyncio
async def test_promised_stream_reset_during_write_preserves_other_streams():
    pushed_paths = []

    async def app(conn, receive, send) -> None:
        pushed_paths.append(conn.path)

    writer = _BlockingPushWriter()
    actor = _actor(writer=writer, app=app)
    survivor_release = asyncio.Event()
    completed = []

    async def survivor(stream_id: int) -> None:
        await survivor_release.wait()
        completed.append(stream_id)

    parent = actor.root_stream.add_child(1)
    parent.on_headers_received(end_stream=True)
    parent.conn = Connection(
        method="GET", path="/", raw_path=b"/", headers=Headers([])
    )
    sibling = actor.root_stream.add_child(3)
    sibling.on_headers_received(end_stream=True)

    async with asyncio.TaskGroup() as task_group:
        actor._task_group = task_group
        for stream_id in (1, 3):
            task = task_group.create_task(survivor(stream_id))
            actor._stream_tasks[stream_id] = task
            task.add_done_callback(actor._make_done_cb(stream_id))
        actor._active_stream_count = 2
        push_task = task_group.create_task(
            actor._handle_push(
                {"type": "http.response.push", "path": "/asset", "headers": []},
                1,
            )
        )

        await asyncio.wait_for(writer.promise_written.wait(), timeout=1)
        await RstStreamResponder(
            actor.factory.rst_stream(2, ErrorCodes.CANCEL)
        ).respond(actor)
        writer.release_promise.set()
        survivor_release.set()
        await push_task

    actor._task_group = None
    await asyncio.sleep(0)

    assert completed == [1, 3]
    assert pushed_paths == []
    assert not [
        frame for frame in _frames(actor) if frame.FrameType() == FrameTypes.GOAWAY
    ]
    assert _live_ownership(actor) == (0, 0, 0, 0, 0, 0)


@pytest.mark.asyncio
async def test_parent_reset_during_promise_write_resets_promised_stream():
    pushed_paths = []

    async def app(conn, receive, send) -> None:
        pushed_paths.append(conn.path)

    writer = _BlockingPushWriter()
    actor = _actor(writer=writer, app=app)
    sibling_release = asyncio.Event()
    completed = []

    async def sibling_work() -> None:
        await sibling_release.wait()
        completed.append(3)

    parent = actor.root_stream.add_child(1)
    parent.on_headers_received(end_stream=True)
    parent.conn = Connection(
        method="GET", path="/", raw_path=b"/", headers=Headers([])
    )
    sibling = actor.root_stream.add_child(3)
    sibling.on_headers_received(end_stream=True)

    async with asyncio.TaskGroup() as task_group:
        actor._task_group = task_group
        parent_task = task_group.create_task(
            actor._handle_push(
                {"type": "http.response.push", "path": "/asset", "headers": []},
                1,
            )
        )
        sibling_task = task_group.create_task(sibling_work())
        actor._stream_tasks.update({1: parent_task, 3: sibling_task})
        parent_task.add_done_callback(actor._make_done_cb(1))
        sibling_task.add_done_callback(actor._make_done_cb(3))
        actor._active_stream_count = 2

        await asyncio.wait_for(writer.promise_written.wait(), timeout=1)
        await RstStreamResponder(
            actor.factory.rst_stream(1, ErrorCodes.CANCEL)
        ).respond(actor)
        writer.release_promise.set()
        sibling_release.set()

    actor._task_group = None
    await asyncio.sleep(0)

    assert parent_task.cancelled()
    assert completed == [3]
    assert pushed_paths == []
    assert [
        frame.stream_id
        for frame in _frames(actor)
        if frame.FrameType() == FrameTypes.RST_STREAM
    ] == [2]
    assert not [
        frame for frame in _frames(actor) if frame.FrameType() == FrameTypes.GOAWAY
    ]
    assert _live_ownership(actor) == (0, 0, 0, 0, 0, 0)


@pytest.mark.asyncio
async def test_parent_reset_preserves_cancellation_when_promised_reset_fails():
    actor = _actor()
    control_sender = _FailingResetControlSender()
    actor._control_sender = control_sender
    parent = actor.root_stream.add_child(1)
    parent.on_headers_received(end_stream=True)
    parent.conn = Connection(
        method="GET", path="/", raw_path=b"/", headers=Headers([])
    )

    async with asyncio.TaskGroup() as task_group:
        actor._task_group = task_group
        parent_task = task_group.create_task(
            actor._handle_push(
                {"type": "http.response.push", "path": "/asset", "headers": []},
                1,
            )
        )
        actor._stream_tasks[1] = parent_task
        actor._active_stream_count = 1

        await asyncio.wait_for(control_sender.promise_written.wait(), timeout=1)
        await RstStreamResponder(
            actor.factory.rst_stream(1, ErrorCodes.CANCEL)
        ).respond(actor)
        control_sender.release_promise.set()

    actor._task_group = None

    assert parent_task.cancelled()
    assert control_sender.reset_attempts == [2]
    assert actor.root_stream.find_child(2) is None
    assert 2 not in actor._stream_tasks
    assert 2 not in actor._senders
    assert 2 not in actor._recipients


@pytest.mark.asyncio
@pytest.mark.parametrize("live_parent", [False, True])
async def test_push_without_both_live_parent_and_task_group_creates_nothing(
    live_parent: bool,
):
    actor = _actor()
    if live_parent:
        actor.root_stream.add_child(1)
    else:
        actor._task_group = object()

    await actor._handle_push(
        {"type": "http.response.push", "path": "/asset", "headers": []}, 1
    )

    assert actor._next_push_stream_id == 2
    assert not [
        frame for frame in _frames(actor)
        if frame.FrameType() == FrameTypes.PUSH_PROMISE
    ]
    assert _live_ownership(actor) == (int(live_parent), 0, 0, 0, 0, 0)


@pytest.mark.asyncio
async def test_websocket_spawn_failure_preserves_existing_live_count():
    actor = _actor()
    actor._ws_stream_ids.add(3)
    actor._ws_stream_count = 1
    stream = actor.root_stream.add_child(1)
    stream.on_headers_received(end_stream=False)
    stream.conn = Connection(
        type="websocket",
        http_version="2",
        method="CONNECT",
        path="/ws",
        raw_path=b"/ws",
        headers=Headers([]),
    )
    task_group = asyncio.TaskGroup()
    await task_group.__aenter__()
    await task_group.__aexit__(None, None, None)

    await actor._handle_h2_websocket(
        stream, task_group, SimpleNamespace(status="-")
    )

    assert actor._ws_stream_ids == {3}
    assert actor._ws_stream_count == 1
    assert actor.root_stream.find_child(1) is None
    assert 1 not in actor._stream_tasks
    assert 1 not in actor._senders
    assert 1 not in actor._recipients


def test_websocket_reader_hands_withheld_padded_credit_to_retirement_once():
    reader = HTTP2WSReader(max_buffer=1)
    actor = _actor()
    payload = bytes([4]) + b"abc" + b"xxxx"
    frame = actor.factory.load(
        _wire(FrameTypes.DATA, int(DataFrameFlags.PADDED), 1, payload)
    )

    assert not reader.put_DATAFrame(frame)
    assert reader.take_uncredited() == frame.length
    assert reader.take_uncredited() == 0


def test_closed_history_is_bounded_with_independent_peer_and_push_watermarks():
    actor = _actor()
    for stream_id in range(1, (_CLOSED_STREAMS_CAP + 20) * 2, 2):
        actor._mark_closed(stream_id, via_rst=False)
    actor._mark_closed(2 * (_CLOSED_STREAMS_CAP + 100), via_rst=False)

    assert len(actor._closed_streams) <= _CLOSED_STREAMS_CAP
    assert actor._closed_peer_high_water % 2 == 1
    assert actor._closed_push_high_water % 2 == 0


@pytest.mark.asyncio
async def test_rejected_padded_data_replays_buffered_and_current_connection_credit():
    actor = _actor()
    stream = actor.root_stream.add_child(1)
    stream.on_headers_received(end_stream=False)
    recipient = _Recipient(credit=5)
    recipient.put_DATAFrame = lambda frame: False
    actor._recipients[1] = recipient
    actor.make_sender(1)
    payload = b"abc"
    padding = b"xxxx"
    raw = _wire(
        FrameTypes.DATA,
        int(DataFrameFlags.PADDED),
        1,
        bytes([len(padding)]) + payload + padding,
    )
    frame = actor.factory.load(raw)

    await actor._on_data_frame(frame, stream)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    updates = [
        item
        for item in _frames(actor)
        if item.FrameType() == FrameTypes.WINDOW_UPDATE and item.stream_id == 0
    ]
    assert [item.window_size for item in updates] == [5 + frame.length]
    assert _live_ownership(actor) == (0, 0, 0, 0, 0, 0)


@pytest.mark.asyncio
async def test_undecoded_header_block_over_cap_is_connection_error():
    actor = _actor(reader=_Reader(b""))
    actor._header_max_total = 8
    encoder = Encoder()
    block = encoder.encode(
        [(b":method", b"GET"), (b":path", b"/"), (b":scheme", b"https")]
    )
    opening = _wire(FrameTypes.HEADERS, 0, 1, block[:1])
    continuation = _wire(
        FrameTypes.CONTINUATION, int(HeaderFrameFlags.END_HEADERS), 1, block[1:] + b"x" * 9
    )
    actor._reader = _Reader(opening + continuation)

    await actor.run()

    sent = _frames(actor)
    assert [frame for frame in sent if frame.FrameType() == FrameTypes.GOAWAY]
    assert not [frame for frame in sent if frame.FrameType() == FrameTypes.RST_STREAM]

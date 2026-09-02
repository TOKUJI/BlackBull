"""The HTTP/2 response has the totals and the progress deadline the HTTP/1.1
response has, because the peer chooses which of the two it faces.

``Client.__aenter__`` dispatches on ``selected_alpn_protocol()``.  Every bound
the HTTP/1.1 client carries — ``BB_CLIENT_HEAD_MAX_TOTAL``,
``BB_CLIENT_BODY_MAX_TOTAL``, ``BB_CLIENT_BODY_TIMEOUT`` — was therefore
escapable by a peer that advertises ``h2``: an operator configures the client's
limits and does not choose which path answers.  So the knobs are shared rather
than duplicated under HTTP/2 names; a second name would let a limit be
configured on one path and answered on the other, which is the defect itself.

Three things were unbounded, and they are separate rows of the triad rather
than one:

*The body total.*  ``_on_response_data`` appended every DATA payload to
``body_parts`` and returned flow-control credit immediately, so the window
bounded bytes *in flight* and never the accumulation.

*The header aggregate.*  ``_on_response_headers`` appends for **every** HEADERS
frame on the stream — informational responses, the final headers, trailers — so
a section that is individually legal accumulates without limit.  The bound on
one field section is not this: it belongs to hpack's ``max_header_list_size``,
and ``TestTheFieldSectionBoundIsTheDecoders`` pins that so the dependency
default is on the record as load-bearing.

*The time between frames.*  ``_FRAME_READ_TIMEOUT`` bounds the remainder of a
frame whose header has arrived; nothing bounded the gap between frames.  A
connection-wide clock cannot stand in for it, because another stream's traffic
hides a stalled one — which is what ``test_a_busy_stream_does_not_shelter_a_stalled_one``
is for.

Refusal is a *stream* error here, not a connection one.  The HTTP/1.1 client
abandons the connection because a refusal leaves its position in the byte
stream unknown; HTTP/2 frames are self-delimiting, so ``RST_STREAM`` refuses one
response and the connection stays usable.  Every bound below asserts that.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.client.exceptions import ResponseTooLarge
from blackbull.client.http2 import (HTTP2Client, _PendingResponse,
                                    _WINDOW_UPDATE_THRESHOLD)
from blackbull.protocol.frame_types import (ErrorCodes, FrameTypes,
                                            PseudoHeaders)
from blackbull.server.sender import AbstractWriter

# A bound that does not fire presents as a hang, not as a wrong value, so
# every test here carries a deadline: red must be reported, not waited on.
pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(10)]


async def _resolved(future, seconds: float = 1.0):
    """The response, or a failure — never an unbounded wait."""
    return await asyncio.wait_for(asyncio.shield(future), seconds)


class _RecordingWriter(AbstractWriter):
    async def write(self, data: bytes) -> None:
        pass


class _CapHits:
    """A live view of ``blackbull.caps`` records.

    Snapshotting in the fixture would read the log before the test wrote to
    it, and every assertion here would pass vacuously.
    """

    def __init__(self, caplog) -> None:
        self._caplog = caplog

    def _records(self) -> list:
        return [r for r in self._caplog.records if getattr(r, 'cap', None)]

    def __iter__(self):
        return iter(self._records())

    def __len__(self) -> int:
        # Not ``len(list(self))``: ``list`` asks ``__len__`` for a size hint.
        return len(self._records())

    def __repr__(self) -> str:
        return repr([(r.cap, r.requested) for r in self._records()])


@pytest.fixture
def caps_log(caplog):
    """Cap hits, so a *spurious* refusal is as visible as a missing one."""
    import logging
    caplog.set_level(logging.WARNING, logger='blackbull.caps')
    return _CapHits(caplog)


def _client() -> HTTP2Client:
    c = HTTP2Client('localhost', 1)
    c._writer = _RecordingWriter()
    c.sent: list = []                                    # type: ignore[attr-defined]

    async def _capture(frame):
        c.sent.append(frame)                             # type: ignore[attr-defined]

    c._control_sender = _capture
    return c


def _pending(c: HTTP2Client, stream_id: int = 1) -> asyncio.Future:
    future = asyncio.get_running_loop().create_future()
    c._responses[stream_id] = _PendingResponse(future=future)
    return future


def _frames_of(c: HTTP2Client, kind) -> list:
    return [f for f in c.sent if f.FrameType() == kind]   # type: ignore[attr-defined]


async def _feed_data(c: HTTP2Client, stream_id: int, payload: bytes, *,
                     end_stream: bool = False) -> None:
    frame = c._factory.create(FrameTypes.DATA, 1 if end_stream else 0,
                              stream_id, data=payload)
    await c._on_response_data(frame)


async def _feed_headers(c: HTTP2Client, stream_id: int,
                        headers: list[tuple[str, str]], *,
                        end_stream: bool = False, status: int = 200) -> None:
    """A response head.  *status* below 200 makes it an interim response,
    which must not start the progress clock."""
    frame = c._factory.create(FrameTypes.HEADERS, 5 if end_stream else 4,
                              stream_id)
    frame.pseudo_headers[PseudoHeaders.STATUS] = str(status)
    frame.headers.extend(headers)
    await c._on_response_headers(frame)


# ----------------------------------------------------------------------
# The body total
# ----------------------------------------------------------------------

class TestTheBodyTotal:
    async def test_off_by_default_a_large_body_is_accepted(self):
        c = _client()
        future = _pending(c)
        for _ in range(8):
            await _feed_data(c, 1, b'x' * 4096)
        await _feed_data(c, 1, b'', end_stream=True)
        assert len((await _resolved(future)).body) == 8 * 4096

    async def test_a_body_over_the_cap_is_refused(self, monkeypatch):
        monkeypatch.setenv('BB_CLIENT_BODY_MAX_TOTAL', '10000')
        c = _client()
        future = _pending(c)
        for _ in range(8):
            await _feed_data(c, 1, b'x' * 4096)
        with pytest.raises(ResponseTooLarge):
            await _resolved(future)

    async def test_the_refused_body_is_never_accumulated(self, monkeypatch):
        """The cap bounds memory, so it must be checked before the append."""
        monkeypatch.setenv('BB_CLIENT_BODY_MAX_TOTAL', '10000')
        c = _client()
        future = _pending(c)
        held = c._responses[1]
        for _ in range(8):
            await _feed_data(c, 1, b'x' * 4096)
        with pytest.raises(ResponseTooLarge):
            await _resolved(future)
        assert sum(len(p) for p in held.body_parts) <= 10000

    async def test_the_refusal_resets_the_stream(self, monkeypatch):
        monkeypatch.setenv('BB_CLIENT_BODY_MAX_TOTAL', '10000')
        c = _client()
        future = _pending(c)
        for _ in range(8):
            await _feed_data(c, 1, b'x' * 4096)
        with pytest.raises(ResponseTooLarge):
            await _resolved(future)
        resets = _frames_of(c, FrameTypes.RST_STREAM)
        assert resets and resets[0].stream_id == 1
        assert resets[0].error_code == ErrorCodes.CANCEL

    async def test_the_refusing_frame_is_itself_credited(self, monkeypatch):
        """The frame that breaches the cap is dropped, not un-received.

        Its octets already consumed the shared connection window (RFC 9113
        §6.9), so refusing without crediting leaks the window by one frame per
        refused stream.  Sized past ``_WINDOW_UPDATE_THRESHOLD`` so that one
        frame is enough to require a WINDOW_UPDATE on its own — the follow-on
        frames below take a different code path and would mask this.
        """
        monkeypatch.setenv('BB_CLIENT_BODY_MAX_TOTAL', '1000')
        c = _client()
        future = _pending(c)
        await _feed_data(c, 1, b'x' * (_WINDOW_UPDATE_THRESHOLD + 1))
        with pytest.raises(ResponseTooLarge):
            await _resolved(future)
        updates = _frames_of(c, FrameTypes.WINDOW_UPDATE)
        assert updates, ('the refused frame returned no credit — the shared '
                         'connection window leaks by its size')
        assert all(f.stream_id == 0 for f in updates)

    async def test_frames_after_the_refusal_are_credited(self, monkeypatch):
        """The peer keeps sending until RST_STREAM reaches it."""
        monkeypatch.setenv('BB_CLIENT_BODY_MAX_TOTAL', '1000')
        c = _client()
        future = _pending(c)
        await _feed_data(c, 1, b'x' * 2000)
        with pytest.raises(ResponseTooLarge):
            await _resolved(future)
        c.sent.clear()                                   # type: ignore[attr-defined]
        for _ in range(4):
            await _feed_data(c, 1, b'x' * 16384)
        updates = _frames_of(c, FrameTypes.WINDOW_UPDATE)
        assert updates, ('no credit returned for DATA that arrived after the '
                         'refusal — the shared connection window leaks')
        assert all(f.stream_id == 0 for f in updates)

    async def test_another_stream_is_unaffected(self, monkeypatch):
        """A stream error, not a connection one."""
        monkeypatch.setenv('BB_CLIENT_BODY_MAX_TOTAL', '10000')
        c = _client()
        refused, kept = _pending(c, 1), _pending(c, 3)
        for _ in range(8):
            await _feed_data(c, 1, b'x' * 4096)
        with pytest.raises(ResponseTooLarge):
            await _resolved(refused)
        await _feed_data(c, 3, b'ok', end_stream=True)
        assert (await _resolved(kept)).body == b'ok'


# ----------------------------------------------------------------------
# The header aggregate
# ----------------------------------------------------------------------

class TestTheHeaderAggregate:
    @staticmethod
    def _section(n: int) -> list[tuple[str, str]]:
        return [(f'x-pad-{i}', 'v' * 100) for i in range(n)]

    async def test_off_by_default_repeated_headers_are_accepted(self):
        c = _client()
        future = _pending(c)
        for _ in range(20):
            await _feed_headers(c, 1, self._section(10))
        await _feed_headers(c, 1, [], end_stream=True)
        assert (await _resolved(future)).status

    async def test_headers_accumulating_past_the_cap_are_refused(self, monkeypatch):
        monkeypatch.setenv('BB_CLIENT_HEAD_MAX_TOTAL', '4096')
        c = _client()
        future = _pending(c)
        for _ in range(20):
            await _feed_headers(c, 1, self._section(10))
        with pytest.raises(ResponseTooLarge):
            await _resolved(future)

    async def test_one_legal_section_is_not_refused(self, monkeypatch):
        """The cap is on the aggregate; a single section inside it must pass."""
        monkeypatch.setenv('BB_CLIENT_HEAD_MAX_TOTAL', '4096')
        c = _client()
        future = _pending(c)
        await _feed_headers(c, 1, self._section(10), end_stream=True)
        assert (await _resolved(future)).status

    async def test_the_refusal_resets_the_stream(self, monkeypatch):
        monkeypatch.setenv('BB_CLIENT_HEAD_MAX_TOTAL', '4096')
        c = _client()
        future = _pending(c)
        for _ in range(20):
            await _feed_headers(c, 1, self._section(10))
        with pytest.raises(ResponseTooLarge):
            await _resolved(future)
        assert _frames_of(c, FrameTypes.RST_STREAM)


class TestTheFieldSectionBoundIsTheDecoders:
    """One field section is bounded by hpack, not by us — recorded so the
    dependency default is on the register as load-bearing rather than
    rediscovered as a gap."""

    async def test_hpack_caps_a_single_field_section(self):
        from hpack import Decoder
        assert Decoder().max_header_list_size == 65536


# ----------------------------------------------------------------------
# The time column: a progress deadline that is per stream
# ----------------------------------------------------------------------

class TestThePerStreamProgressDeadline:
    """``_FRAME_READ_TIMEOUT`` bounds the remainder of a frame whose 9-byte
    header has already arrived.  Nothing bounded the gap *between* frames, so a
    peer sending one complete 1-byte DATA frame every 29 s held a response open
    forever.

    ``BB_CLIENT_BODY_TIMEOUT`` is re-armed on each frame for the stream, which
    is what it already means on HTTP/1.1 — progress, not duration — with a
    frame in place of a transport read.

    Most of these assert the timer's *scheduled time* rather than waiting for
    it.  Sleeping through a deadline makes a test that is slow when it passes
    and load-sensitive when it does not, and "the clock was pushed forward" is
    the claim being made anyway.  Two tests do wait, because something has to
    prove the timer is really on the loop.
    """

    @staticmethod
    def _armed_at(c, stream_id: int) -> float | None:
        handle = c._responses[stream_id].deadline
        return None if handle is None else handle.when()

    async def test_the_first_response_frame_arms_it(self, monkeypatch):
        monkeypatch.setenv('BB_CLIENT_BODY_TIMEOUT', '30')
        c = _client()
        _pending(c)
        assert self._armed_at(c, 1) is None, 'armed before the peer answered'
        await _feed_headers(c, 1, [('x-a', 'b')])
        assert self._armed_at(c, 1) is not None

    async def test_each_frame_pushes_the_deadline_forward(self, monkeypatch):
        """Progress, not duration: a response of many frames may outlast the
        deadline many times over so long as no single gap does."""
        monkeypatch.setenv('BB_CLIENT_BODY_TIMEOUT', '30')
        c = _client()
        _pending(c)
        await _feed_headers(c, 1, [('x-a', 'b')])
        armed = [self._armed_at(c, 1)]
        for _ in range(3):
            await asyncio.sleep(0)      # let the loop clock advance
            await _feed_data(c, 1, b'x' * 10)
            armed.append(self._armed_at(c, 1))
        assert armed == sorted(armed) and armed[-1] > armed[0], armed

    async def test_a_busy_stream_does_not_shelter_a_stalled_one(
            self, monkeypatch):
        """The reason the clock cannot be connection-wide.  Frames arrive the
        whole time — on the *other* stream — and must not move this one's."""
        monkeypatch.setenv('BB_CLIENT_BODY_TIMEOUT', '30')
        c = _client()
        _pending(c, 1), _pending(c, 3)
        await _feed_headers(c, 1, [('x-a', 'b')])
        await _feed_headers(c, 3, [('x-a', 'b')])
        stalled_at = self._armed_at(c, 1)
        for _ in range(5):
            await asyncio.sleep(0)
            await _feed_data(c, 3, b'y' * 10)
        assert self._armed_at(c, 1) == stalled_at, (
            "the busy stream's traffic moved the stalled stream's deadline")
        assert self._armed_at(c, 3) > stalled_at

    async def test_it_fires_and_refuses_only_that_stream(self, monkeypatch):
        """The one that waits: proof the timer is really on the loop."""
        monkeypatch.setenv('BB_CLIENT_BODY_TIMEOUT', '0.05')
        c = _client()
        stalled, kept = _pending(c, 1), _pending(c, 3)
        await _feed_headers(c, 1, [('x-a', 'b')])
        with pytest.raises(TimeoutError):
            await _resolved(stalled, 2.0)
        assert _frames_of(c, FrameTypes.RST_STREAM)
        assert 1 not in c._responses
        # A stream error: the connection and every other stream survive.
        assert not kept.done()
        await _feed_data(c, 3, b'ok', end_stream=True)
        assert (await _resolved(kept)).body == b'ok'

    async def test_a_response_that_keeps_arriving_is_not_refused(
            self, monkeypatch):
        """The other one that waits: the control for the test above."""
        monkeypatch.setenv('BB_CLIENT_BODY_TIMEOUT', '0.2')
        c = _client()
        future = _pending(c)
        await _feed_headers(c, 1, [('x-a', 'b')])
        for _ in range(6):
            await asyncio.sleep(0.03)
            await _feed_data(c, 1, b'x' * 10)
        await _feed_data(c, 1, b'', end_stream=True)
        assert len((await _resolved(future)).body) == 60

    async def test_off_when_the_deadline_is_disabled(self, monkeypatch):
        monkeypatch.setenv('BB_CLIENT_BODY_TIMEOUT', '0')
        c = _client()
        _pending(c)
        await _feed_headers(c, 1, [('x-a', 'b')])
        await _feed_data(c, 1, b'x' * 10)
        assert self._armed_at(c, 1) is None

    async def test_no_timer_outlives_its_response(self, monkeypatch):
        """Every path that drops a pending response must stop its timer, or
        the callback holds this client on the loop until the deadline
        elapses.  Completion, peer reset and refusal are all such paths."""
        monkeypatch.setenv('BB_CLIENT_BODY_TIMEOUT', '30')
        c = _client()

        async def _ended_by_completion():
            _pending(c, 1)
            await _feed_headers(c, 1, [('x-a', 'b')])
            await _feed_data(c, 1, b'hi', end_stream=True)

        async def _ended_by_peer_reset():
            _pending(c, 3)
            await _feed_headers(c, 3, [('x-a', 'b')])
            c._on_rst_stream(c._factory.create(
                FrameTypes.RST_STREAM, 0, 3,
                data=int(ErrorCodes.CANCEL).to_bytes(4, 'big')))

        async def _ended_by_refusal():
            _pending(c, 5)
            await _feed_headers(c, 5, [('x-a', 'b')])
            await c._refuse_stream(5, 'client_body_max_total', 1, 0,
                                   ResponseTooLarge('refused'))

        for ending in (_ended_by_completion, _ended_by_peer_reset,
                       _ended_by_refusal):
            await ending()
        assert c._responses == {}
        live = [h for h in asyncio.get_running_loop()._scheduled
                if not h.cancelled()
                and 'stalled' in repr(getattr(h, '_callback', ''))]
        assert not live, f'{len(live)} progress timer(s) outlived their response'


# ----------------------------------------------------------------------
# What two independent reviews of the first cut found
# ----------------------------------------------------------------------

class TestAnInterimResponseIsNotTheResponse:
    """1xx must not start the progress clock.

    ``103 Early Hints`` followed by a second of real work was refused: the
    deadline was armed by *every* HEADERS frame, including one that announces
    the peer is still working.  That is precisely the case
    ``_arm_progress_deadline`` documents itself as exempt from.
    """

    @pytest.mark.parametrize('status', [100, 103, 199])
    async def test_an_interim_head_does_not_arm_the_deadline(
            self, status, monkeypatch):
        monkeypatch.setenv('BB_CLIENT_BODY_TIMEOUT', '30')
        c = _client()
        _pending(c)
        await _feed_headers(c, 1, [('link', '</s.css>; rel=preload')],
                            status=status)
        assert c._responses[1].deadline is None

    async def test_the_final_head_after_an_interim_one_does_arm_it(
            self, monkeypatch):
        monkeypatch.setenv('BB_CLIENT_BODY_TIMEOUT', '30')
        c = _client()
        _pending(c)
        await _feed_headers(c, 1, [], status=103)
        await _feed_headers(c, 1, [], status=200)
        assert c._responses[1].deadline is not None

    async def test_interim_field_lines_still_count_toward_the_total(
            self, monkeypatch):
        """Exempt from the clock is not exempt from the budget — they are
        accumulation whatever they announce."""
        monkeypatch.setenv('BB_CLIENT_HEAD_MAX_TOTAL', '4096')
        c = _client()
        future = _pending(c)
        for _ in range(20):
            await _feed_headers(c, 1, [(f'x-{i}', 'v' * 100)
                                       for i in range(10)], status=103)
        with pytest.raises(ResponseTooLarge):
            await _resolved(future)


class TestAFrameThatDeliversNothingIsNotProgress:
    """Empty and all-padding DATA defeated both body bounds at once.

    ``body_seen`` grew by zero while ``body_parts`` grew by an entry and the
    deadline was re-armed, so neither the total nor the clock could ever fire:
    200,000 such frames held 1.6 MB against a 1 KiB cap.
    """

    async def test_empty_data_does_not_rearm_the_deadline(self, monkeypatch):
        monkeypatch.setenv('BB_CLIENT_BODY_TIMEOUT', '30')
        c = _client()
        _pending(c)
        await _feed_headers(c, 1, [])
        armed = c._responses[1].deadline.when()
        for _ in range(5):
            await asyncio.sleep(0)
            await _feed_data(c, 1, b'')
        assert c._responses[1].deadline.when() == armed

    async def test_empty_data_is_not_accumulated(self, monkeypatch):
        monkeypatch.setenv('BB_CLIENT_BODY_MAX_TOTAL', '1024')
        c = _client()
        _pending(c)
        await _feed_headers(c, 1, [])
        for _ in range(2000):
            await _feed_data(c, 1, b'')
        held = c._responses[1]
        assert held.body_parts == [] and held.body_seen == 0

    async def test_an_all_padding_frame_still_credits_the_window(self):
        """It delivered nothing and still consumed the connection window."""
        c = _client()
        _pending(c)
        await _feed_headers(c, 1, [])
        padded = c._factory.create(
            FrameTypes.DATA, 0x8, 1,
            data=bytes([255]) + b'' + b'\x00' * 255)
        for _ in range(200):
            await c._on_response_data(padded)
        updates = _frames_of(c, FrameTypes.WINDOW_UPDATE)
        assert updates, ('padding consumed the shared window and returned no '
                         'credit — a leak that only ever closes')


class TestARefusalDuringAnUploadReachesTheCaller:
    """RST_STREAM is what makes a peer stop crediting the stream window, so a
    refusal mid-upload parked ``request()`` on a window that would never
    reopen — holding an answer it could not deliver."""

    async def test_the_upload_is_cancelled_by_the_refusal(self):
        c = _client()
        future = _pending(c)
        stalled = asyncio.get_running_loop().create_future()
        c._responses[1].upload = stalled
        await c._refuse_stream(1, 'client_body_max_total', 1, 0,
                               ResponseTooLarge('too big'))
        assert stalled.cancelled()
        with pytest.raises(ResponseTooLarge):
            await _resolved(future)

    async def test_request_returns_the_refusal_instead_of_parking(
            self, monkeypatch):
        """The caller-facing claim: ``request()`` awaits the upload before it
        awaits the response, so without the cancellation it never returns."""
        monkeypatch.setenv('BB_CLIENT_BODY_MAX_TOTAL', '1000')
        c = _client()
        c._on_initial_window_size(10)          # the upload parks after 10 B
        call = asyncio.ensure_future(c.request('POST', '/', body=b'z' * 5000))
        for _ in range(4):
            await asyncio.sleep(0)
        await _feed_headers(c, 1, [])
        await _feed_data(c, 1, b'x' * 2000)    # over the cap, mid-upload
        with pytest.raises(ResponseTooLarge):
            await asyncio.wait_for(call, 2.0)

    async def test_a_completed_response_leaves_its_upload_alone(self):
        """The other half: a server may answer with END_STREAM while the body
        is still going up, and that upload must finish."""
        c = _client()
        _pending(c)
        upload = asyncio.get_running_loop().create_future()
        c._responses[1].upload = upload
        await _feed_data(c, 1, b'ok', end_stream=True)
        assert not upload.cancelled()
        upload.cancel()


class TestTheVerdictIsRetakenWhenTheTimerFires:
    """A timer callback cannot await, so it hands off to a task — and in that
    gap the receive loop can finish the response."""

    async def test_a_response_that_completes_first_is_not_reset(
            self, caps_log, monkeypatch):
        monkeypatch.setenv('BB_CLIENT_BODY_TIMEOUT', '30')
        c = _client()
        future = _pending(c)
        await _feed_headers(c, 1, [])
        c._on_stream_stalled(1, 30.0)          # timer fires…
        await _feed_data(c, 1, b'hello', end_stream=True)   # …response wins
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert (await _resolved(future)).body == b'hello'
        assert not _frames_of(c, FrameTypes.RST_STREAM), (
            'a successful response was reset by a timer that had already lost')
        assert not caps_log, f'a cap hit was logged against it: {caps_log}'

    async def test_one_stream_is_refused_once(self, caps_log, monkeypatch):
        monkeypatch.setenv('BB_CLIENT_BODY_MAX_TOTAL', '1000')
        c = _client()
        future = _pending(c)
        await _feed_headers(c, 1, [])
        await _feed_data(c, 1, b'x' * 2000)
        c._on_stream_stalled(1, 30.0)
        await asyncio.sleep(0)
        with pytest.raises(ResponseTooLarge):
            await _resolved(future)
        assert len(_frames_of(c, FrameTypes.RST_STREAM)) == 1
        assert len(caps_log) == 1, f'refused twice: {caps_log}'


class TestTheRefusalDoesNotOutliveTheClient:
    async def test_a_detached_refusal_is_cancelled_by_aexit(self, monkeypatch):
        monkeypatch.setenv('BB_CLIENT_BODY_TIMEOUT', '30')
        c = _client()
        _pending(c)
        await _feed_headers(c, 1, [])
        c._on_stream_stalled(1, 30.0)
        assert c._detached, 'the refusal task is not tracked'
        await c.__aexit__(None, None, None)
        assert not c._detached


class TestTheHeaderCapUsesTheServersErrorCode:
    async def test_enhance_your_calm_for_the_header_aggregate(self, monkeypatch):
        """The server answers its own header cap with ENHANCE_YOUR_CALM and
        cites nginx and Envoy; one cap should not get two codes."""
        monkeypatch.setenv('BB_CLIENT_HEAD_MAX_TOTAL', '1024')
        c = _client()
        future = _pending(c)
        for _ in range(20):
            await _feed_headers(c, 1, [(f'x-{i}', 'v' * 100)
                                       for i in range(10)])
        with pytest.raises(ResponseTooLarge):
            await _resolved(future)
        resets = _frames_of(c, FrameTypes.RST_STREAM)
        assert resets[0].error_code == ErrorCodes.ENHANCE_YOUR_CALM

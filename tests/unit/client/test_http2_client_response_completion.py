"""A response is complete only when it is a well-formed final response.

`request()` used to resolve on the first ``END_STREAM`` whatever had arrived
before it.  A DATA frame with no head at all, a head with no ``:status``, an
interim ``103`` that ended the stream, a body that contradicted its own
``Content-Length``, a body on ``HEAD`` — each of these resolved to a
``200`` (or to whatever the last ``:status`` said) and reached the
application as a successful response.  HTTP/2 frame syntax was validated;
HTTP response *meaning* was not.

The rule is RFC 9113 §8.1's state machine, and it is the same rule the
HTTP/1.1 reader applies::

    (1xx HEADERS)* -> final HEADERS -> (DATA)* -> [trailer HEADERS]? -> END_STREAM

Everything below is driven through the public ``request()``, because the
contract under test is what a caller is told — a frame handler that behaves
while ``request()`` still lies is not a fix.

The positive controls matter as much as the refusals: a 1xx before a 200, a
legal trailer section, an empty body and a ``HEAD`` are all correct traffic,
and a completeness check that rejects those would trade one bug for another.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.client.exceptions import ProtocolError, ResponseTooLarge
from blackbull.client.http2 import HTTP2Client
from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import (DataFrameFlags, ErrorCodes,
                                            FrameTypes, HeaderFrameFlags,
                                            PseudoHeaders, SettingFrameFlags)
from blackbull.server.sender import AbstractWriter

# A completeness rule that does not fire presents as a wrong status, not as a
# hang; one that fires wrongly presents as a hang.  Both are reported.
pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(10)]


# ----------------------------------------------------------------------
# Harness — the wire is an in-memory StreamReader, the caller is request()
# ----------------------------------------------------------------------

class _MemTransport(asyncio.Transport):
    def __init__(self) -> None:
        super().__init__()
        self.written = bytearray()
        self.closing = False

    def write(self, data: bytes) -> None:
        self.written += data

    def can_write_eof(self) -> bool:
        return True

    def is_closing(self) -> bool:
        return self.closing

    def close(self) -> None:
        self.closing = True

    def abort(self) -> None:
        self.closing = True

    def get_extra_info(self, name, default=None):
        return default


class _MemWriter(asyncio.StreamWriter):
    """A real ``StreamWriter`` (``_adopt`` is typed for one) over no socket."""

    def __init__(self) -> None:
        self.mem = _MemTransport()
        super().__init__(self.mem, asyncio.Protocol(), None,
                         asyncio.get_running_loop())

    async def drain(self) -> None:
        pass


class _Peer:
    """Builds the peer's side of one response and reports what was refused."""

    def __init__(self) -> None:
        self.factory = FrameFactory()
        self.wire = b''

    def settings(self) -> '_Peer':
        self.wire += self.factory.create(
            FrameTypes.SETTINGS, SettingFrameFlags.INIT, 0).save()
        return self

    def headers(self, pseudo: dict, fields=(), *, end_stream: bool = False,
                stream_id: int = 1, status: str | None = ...) -> '_Peer':
        """A HEADERS frame.  ``status=...`` omits ``:status`` entirely."""
        flags = int(HeaderFrameFlags.END_HEADERS)
        if end_stream:
            flags |= int(HeaderFrameFlags.END_STREAM)
        frame = self.factory.create(FrameTypes.HEADERS, flags, stream_id)
        if status is not ...:
            if status is not None:
                frame.pseudo_headers[PseudoHeaders.STATUS] = status
        elif PseudoHeaders.STATUS in pseudo:
            frame.pseudo_headers[PseudoHeaders.STATUS] = pseudo[PseudoHeaders.STATUS]
        frame.pseudo_headers.update(
            {k: v for k, v in pseudo.items() if k is not PseudoHeaders.STATUS})
        frame.headers.extend(fields)
        self.wire += frame.save()
        return self

    def data(self, payload: bytes, *, end_stream: bool = True,
             stream_id: int = 1) -> '_Peer':
        flags = int(DataFrameFlags.END_STREAM) if end_stream else 0
        self.wire += self.factory.create(
            FrameTypes.DATA, flags, stream_id, data=payload).save()
        return self

    def rst(self, stream_id: int = 1,
            code: ErrorCodes = ErrorCodes.PROTOCOL_ERROR) -> '_Peer':
        self.wire += self.factory.rst_stream(stream_id, code).save()
        return self


async def _call(peer: _Peer, method: str = 'GET'):
    """Run the peer's bytes through the public request(); return the outcome."""
    reader = asyncio.StreamReader()
    client = HTTP2Client._adopt('localhost', 80, reader, _MemWriter())
    rst: list = []
    orig = client._factory.rst_stream

    def watch(stream_id, code):
        rst.append(code)
        return orig(stream_id, code)

    client._factory.rst_stream = watch          # type: ignore[assignment]
    try:
        async with client:
            task = asyncio.ensure_future(client.request(method, '/'))
            # The wire must reach the receive loop only once request() has
            # claimed stream 1: bytes buffered before that are consumed first,
            # and every frame lands on a stream nobody has opened yet.
            while not client._responses and not task.done():
                await asyncio.sleep(0)
            reader.feed_data(peer.wire)
            return await asyncio.wait_for(task, 2.0)
    except Exception as exc:
        return exc, rst
    return None, rst


def _refused(outcome) -> Exception:
    assert isinstance(outcome, tuple), f'expected a refusal, got {outcome!r}'
    exc, _ = outcome
    assert isinstance(exc, ProtocolError), f'expected ProtocolError, got {exc!r}'
    return exc


def _ok(outcome):
    assert not isinstance(outcome, tuple), f'expected a response, got {outcome!r}'
    return outcome


# ----------------------------------------------------------------------
# C1 — the required pseudo-header and the status format
# ----------------------------------------------------------------------

class TestTheStatusIsRequired:
    async def test_the_control_response_completes(self):
        res = _ok(await _call(_Peer().settings()
                              .headers({PseudoHeaders.STATUS: '200'},
                                       [(b'content-length', b'1')])
                              .data(b'x')))
        assert res.status == 200 and res.body == b'x'

    async def test_a_response_with_no_status_is_refused(self):
        _refused(await _call(_Peer().settings()
                             .headers({}, [], end_stream=True, status=None)))

    async def test_a_body_that_arrives_before_any_head_is_refused(self):
        _refused(await _call(_Peer().settings().data(b'x')))

    @pytest.mark.parametrize('status', ['', '20', '2000', 'abc', '2x0'])
    async def test_a_status_that_is_not_three_ascii_digits_is_refused(
            self, status):
        _refused(await _call(_Peer().settings()
                             .headers({}, [], end_stream=True, status=status)))

    async def test_a_status_below_100_is_a_final_head(self):
        """RFC 9110 §15 draws no range and the HTTP/1.1 reader accepts one:
        only 1xx is informational, so ``099`` ends the response."""
        res = _ok(await _call(_Peer().settings()
                              .headers({PseudoHeaders.STATUS: '099'}, [],
                                       end_stream=True)))
        assert res.status == 99


# ----------------------------------------------------------------------
# C2 — order
# ----------------------------------------------------------------------

class TestTheOrderIsEnforced:
    async def test_an_interim_response_before_the_final_one_completes(self):
        res = _ok(await _call(_Peer().settings()
                              .headers({PseudoHeaders.STATUS: '103'}, [],
                                       end_stream=False)
                              .headers({PseudoHeaders.STATUS: '200'}, [],
                                       end_stream=True)))
        assert res.status == 200

    async def test_an_interim_response_that_ends_the_stream_is_refused(self):
        _refused(await _call(_Peer().settings()
                             .headers({PseudoHeaders.STATUS: '103'}, [],
                                      end_stream=True)))

    async def test_a_second_final_head_is_refused(self):
        _refused(await _call(_Peer().settings()
                             .headers({PseudoHeaders.STATUS: '200'}, [],
                                      end_stream=False)
                             .headers({PseudoHeaders.STATUS: '200'}, [],
                                      end_stream=True)))

    async def test_a_pseudo_header_in_the_trailers_is_refused(self):
        _refused(await _call(_Peer().settings()
                             .headers({PseudoHeaders.STATUS: '200'}, [],
                                      end_stream=False)
                             .headers({PseudoHeaders.STATUS: '200'},
                                      [(b'x-checksum', b'1')])))

    async def test_a_legal_trailer_section_completes(self):
        res = _ok(await _call(_Peer().settings()
                              .headers({PseudoHeaders.STATUS: '200'}, [],
                                       end_stream=False)
                              .data(b'x', end_stream=False)
                              .headers({}, [(b'x-checksum', b'1')],
                                       end_stream=True)))
        assert res.status == 200 and res.body == b'x'
        assert res.headers.getlist(b'x-checksum') == []
        assert [v for _n, v in res.trailers.getlist(b'x-checksum')] == [b'1']

    async def test_a_second_trailer_section_is_refused(self):
        _refused(await _call(_Peer().settings()
                             .headers({PseudoHeaders.STATUS: '200'}, [],
                                      end_stream=False)
                             .headers({}, [(b'x-a', b'1')], end_stream=False)
                             .headers({}, [(b'x-b', b'2')], end_stream=True)))

    async def test_a_framing_field_in_the_trailers_is_refused(self):
        _refused(await _call(_Peer().settings()
                             .headers({PseudoHeaders.STATUS: '200'}, [],
                                      end_stream=False)
                             .data(b'abc', end_stream=False)
                             .headers({}, [(b'content-length', b'99')],
                                      end_stream=True)))


# ----------------------------------------------------------------------
# C3 — body rules
# ----------------------------------------------------------------------

class TestTheBodyRules:
    async def test_a_body_on_head_is_refused(self):
        _refused(await _call(_Peer().settings()
                             .headers({PseudoHeaders.STATUS: '200'}, [],
                                      end_stream=False)
                             .data(b'x'), method='HEAD'))

    async def test_a_head_response_with_no_body_completes(self):
        res = _ok(await _call(_Peer().settings()
                              .headers({PseudoHeaders.STATUS: '200'}, [],
                                       end_stream=True), method='HEAD'))
        assert res.status == 200 and res.body == b''

    @pytest.mark.parametrize('status', ['204', '304'])
    async def test_a_body_on_a_bodyless_status_is_refused(self, status):
        """RFC 9112 §6.3 rule 1 — these carry none whatever the fields say."""
        _refused(await _call(_Peer().settings()
                             .headers({PseudoHeaders.STATUS: status}, [],
                                      end_stream=False)
                             .data(b'x')))

    @pytest.mark.parametrize('status', ['204', '304'])
    async def test_a_bodyless_status_with_no_body_completes(self, status):
        res = _ok(await _call(_Peer().settings()
                              .headers({PseudoHeaders.STATUS: status}, [],
                                       end_stream=True)))
        assert res.status == int(status) and res.body == b''

    async def test_a_body_on_a_205_is_read_not_refused(self):
        """RFC 9110 §15.3.6 forbids a *server* to generate content in a 205,
        but RFC 9112 §6.3 still frames one. The reader takes it to its
        declared length so it stays in step with a peer that was already
        wrong — dropping the octets is what desynchronises a reader."""
        res = _ok(await _call(_Peer().settings()
                              .headers({PseudoHeaders.STATUS: '205'},
                                       [(b'content-length', b'1')])
                              .data(b'x', end_stream=True)))
        assert res.body == b'x'

    async def test_a_bodyless_response_accepts_content_length_as_metadata(self):
        """RFC 9110 §9.3.2 — on HEAD the length describes the GET body."""
        res = _ok(await _call(_Peer().settings()
                              .headers({PseudoHeaders.STATUS: '200'},
                                       [(b'content-length', b'1000')],
                                       end_stream=True), method='HEAD'))
        assert res.status == 200 and res.body == b''


# ----------------------------------------------------------------------
# C4 — Content-Length against the body
# ----------------------------------------------------------------------

class TestTheDeclaredLengthIsHonoured:
    async def test_a_body_that_matches_completes(self):
        res = _ok(await _call(_Peer().settings()
                              .headers({PseudoHeaders.STATUS: '200'},
                                       [(b'content-length', b'1')])
                              .data(b'x')))
        assert res.body == b'x'

    async def test_a_body_short_of_the_declared_length_is_refused(self):
        _refused(await _call(_Peer().settings()
                             .headers({PseudoHeaders.STATUS: '200'},
                                      [(b'content-length', b'10')])
                             .data(b'x')))

    async def test_a_body_past_the_declared_length_is_refused(self):
        _refused(await _call(_Peer().settings()
                             .headers({PseudoHeaders.STATUS: '200'},
                                      [(b'content-length', b'1')])
                             .data(b'xy')))

    async def test_two_declarations_that_agree_completes(self):
        res = _ok(await _call(_Peer().settings()
                              .headers({PseudoHeaders.STATUS: '200'},
                                       [(b'content-length', b'1'),
                                        (b'content-length', b'1')])
                              .data(b'x')))
        assert res.body == b'x'

    async def test_two_declarations_that_disagree_are_refused(self):
        _refused(await _call(_Peer().settings()
                             .headers({PseudoHeaders.STATUS: '200'},
                                      [(b'content-length', b'1'),
                                       (b'content-length', b'2')])
                             .data(b'x')))

    async def test_a_body_with_no_declaration_completes(self):
        res = _ok(await _call(_Peer().settings()
                              .headers({PseudoHeaders.STATUS: '200'}, [])
                              .data(b'hello')))
        assert res.body == b'hello'


# ----------------------------------------------------------------------
# C6 — what the caller sees
# ----------------------------------------------------------------------

class TestTheCallerSeesTheFinalHead:
    async def test_an_interim_head_does_not_reach_the_response_headers(self):
        res = _ok(await _call(_Peer().settings()
                              .headers({PseudoHeaders.STATUS: '103'},
                                       [(b'link', b'<x>; rel=preload')],
                                       end_stream=False)
                              .headers({PseudoHeaders.STATUS: '200'},
                                       [(b'content-type', b'text/plain')],
                                       end_stream=True)))
        assert [v for _n, v in res.headers.getlist(b'content-type')] == [
            b'text/plain']
        assert res.headers.getlist(b'link') == []

    async def test_a_trailer_does_not_reach_the_response_headers(self):
        res = _ok(await _call(_Peer().settings()
                              .headers({PseudoHeaders.STATUS: '200'}, [],
                                       end_stream=False)
                              .data(b'x', end_stream=False)
                              .headers({}, [(b'x-checksum', b'1')],
                                       end_stream=True)))
        assert res.headers.getlist(b'x-checksum') == []
        assert [v for _n, v in res.trailers.getlist(b'x-checksum')] == [b'1']


# ----------------------------------------------------------------------
# C5 — the refusal is a stream error
# ----------------------------------------------------------------------

class TestTheRefusalIsAStreamError:
    async def test_the_stream_is_reset_with_protocol_error(self):
        outcome = await _call(_Peer().settings().data(b'x'))
        assert isinstance(outcome, tuple), outcome
        assert outcome[1] == [ErrorCodes.PROTOCOL_ERROR]

    async def test_no_cap_record_is_made_for_a_malformed_response(self, caplog):
        import logging
        caplog.set_level(logging.WARNING, logger='blackbull.caps')
        await _call(_Peer().settings().data(b'x'))
        assert not [r for r in caplog.records if getattr(r, 'cap', None)]


# ----------------------------------------------------------------------
# C6 — one rule per concept (BLA-461 / BLA-462 / BLA-463)
# ----------------------------------------------------------------------

class TestTheMethodIsCaseSensitive:
    async def test_a_lowercase_head_is_not_a_head_response(self):
        """RFC 9110 §9.1: `head` is not `HEAD`, so this response may carry
        content."""
        res = _ok(await _call(_Peer().settings()
                              .headers({PseudoHeaders.STATUS: '200'},
                                       [(b'content-length', b'1')])
                              .data(b'x'), 'head'))
        assert res.body == b'x'


class TestTheInterimResponsesAreBounded:
    @staticmethod
    def _interim(n: int) -> _Peer:
        peer = _Peer().settings()
        for _ in range(n):
            peer.headers({PseudoHeaders.STATUS: '103'}, [])
        return peer

    async def test_interim_responses_up_to_the_limit_complete(self, monkeypatch):
        monkeypatch.setenv('BB_CLIENT_MAX_INTERIM_RESPONSES', '3')
        res = _ok(await _call(self._interim(3)
                              .headers({PseudoHeaders.STATUS: '200'}, [],
                                       end_stream=True)))
        assert res.status == 200

    async def test_interim_responses_past_the_limit_are_refused(
            self, monkeypatch, caplog):
        import logging
        monkeypatch.setenv('BB_CLIENT_MAX_INTERIM_RESPONSES', '3')
        caplog.set_level(logging.WARNING, logger='blackbull.caps')
        outcome = await _call(self._interim(4)
                              .headers({PseudoHeaders.STATUS: '200'}, [],
                                       end_stream=True))
        assert isinstance(outcome, tuple), outcome
        assert isinstance(outcome[0], ResponseTooLarge), outcome[0]
        assert [(r.cap, r.protocol, r.requested, r.limit)
                for r in caplog.records if getattr(r, 'cap', None)] == [
            ('client_max_interim_responses', 'http2', 4, 3)]


class TestTheStatusIsUsable:
    async def test_status_101_is_refused(self):
        """RFC 9113 §8.6 — HTTP/2 does not use 101, so it is malformed here
        where HTTP/1.1 makes it a protocol switch."""
        exc = _refused(await _call(_Peer().settings()
                                   .headers({PseudoHeaders.STATUS: '101'}, [],
                                            end_stream=True)))
        assert '101 is not usable over HTTP/2' in str(exc)


class TestTheOrderNamesWhatHappened:
    async def test_a_second_final_head_says_so(self):
        exc = _refused(await _call(_Peer().settings()
                                   .headers({PseudoHeaders.STATUS: '200'}, [],
                                            end_stream=False)
                                   .headers({PseudoHeaders.STATUS: '200'}, [],
                                            end_stream=True)))
        assert 'second final head' in str(exc)

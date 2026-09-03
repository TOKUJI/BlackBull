"""The response's length is decided by the status code and the request method,
not by the header fields alone.

RFC 9112 §6.3 orders the decision, and its first item overrides every header
field present: a response to ``HEAD``, and any ``1xx``, ``204`` or ``304``,
ends at the blank line whatever it declares.  The recipient saw neither the
status nor the method, so it read a body the message could not have — and the
octets it took came from the *next* response on a keep-alive connection.

RFC 9110 §15.2 is the same blindness one layer up: an interim response is not
the answer, and returning it as the answer leaves the real one on the wire.
``103 Early Hints`` is emitted by Cloudflare and Fastly today, so this is
ordinary traffic rather than a corner case.

Both are the desync class this client has closed from three other directions:
the caller receives a plausible answer that is not the answer to its request.
The tests here assert the second read — what the *next* response parses as —
because that is the observable a wrong first read corrupts.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from blackbull.client.exceptions import (ConnectionError, ProtocolError,
                                         ResponseTooLarge)
from blackbull.client.http1 import HTTP1Client, HTTP1ResponseRecipient
from blackbull.server.recipient import AbstractReader, AbstractWriter


class _Canned(AbstractReader):
    """A reader whose bytes end at EOF, like a peer that closes."""

    def __init__(self, payload: bytes) -> None:
        self._buf, self._pos = payload, 0

    async def read(self, n: int = -1) -> bytes:
        out = self._buf[self._pos:] if n < 0 else self._buf[self._pos:self._pos + n]
        self._pos += len(out)
        return out


class _Held(_Canned):
    """Delivers its bytes, then stays connected and says nothing.

    The peer a body-length mistake actually hangs on: a ``HEAD`` response
    declaring ``content-length: 1234`` with no body behind it, on a connection
    the peer intends to keep.  A reader that answers EOF would end the read for
    the wrong reason and prove nothing.
    """

    async def read(self, n: int = -1) -> bytes:
        out = await super().read(n)
        if not out:
            await asyncio.Event().wait()
        return out


class _NullWriter(AbstractWriter):
    async def write(self, data: bytes) -> None:
        pass


#: A second response, complete and distinguishable, pipelined behind the first.
#: If the first read takes octets that are not its own, this is what changes.
_NEXT = b'HTTP/1.1 201 Created\r\ncontent-length: 2\r\n\r\nok'


async def _within(coro, seconds: float = 1.0):
    """Await *coro* under a guard, so a stall fails as a stall."""
    return await asyncio.wait_for(coro, seconds)


# ----------------------------------------------------------------------
# RFC 9110 §15.2 — interim responses (BLA-295)
# ----------------------------------------------------------------------

class TestInterimResponses:
    @pytest.mark.asyncio
    async def test_early_hints_are_skipped_and_the_final_response_returned(self):
        """The MUST is *parse past it*; the MAY is *ignore it*."""
        wire = (b'HTTP/1.1 103 Early Hints\r\n'
                b'link: </s.css>; rel=preload; as=style\r\n\r\n'
                b'HTTP/1.1 200 OK\r\ncontent-length: 5\r\n\r\nHELLO')
        res = await _within(HTTP1ResponseRecipient().receive(_Canned(wire)))
        assert (res.status, res.body) == (200, b'HELLO')

    @pytest.mark.asyncio
    async def test_several_interims_in_a_row_are_all_skipped(self):
        wire = (b'HTTP/1.1 100 Continue\r\n\r\n'
                b'HTTP/1.1 103 Early Hints\r\nlink: </a>\r\n\r\n'
                b'HTTP/1.1 103 Early Hints\r\nlink: </b>\r\n\r\n'
                b'HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nhi')
        res = await _within(HTTP1ResponseRecipient().receive(_Canned(wire)))
        assert (res.status, res.body) == (200, b'hi')

    @pytest.mark.asyncio
    async def test_an_interim_declaring_a_body_consumes_none_of_it(self):
        """§6.3 item 1 covers 1xx too, so a ``content-length`` on an interim
        is a desync offer: five octets of the real response, taken as a body
        nobody asked for."""
        wire = (b'HTTP/1.1 100 Continue\r\ncontent-length: 5\r\n\r\n'
                b'HTTP/1.1 200 OK\r\ncontent-length: 5\r\n\r\nHELLO')
        res = await _within(HTTP1ResponseRecipient().receive(_Canned(wire)))
        assert (res.status, res.body) == (200, b'HELLO')

    @pytest.mark.asyncio
    async def test_a_101_is_the_answer_and_is_not_skipped(self):
        """``101`` is 1xx by number and final by meaning: the protocol
        switches, so there is no later response to skip to.  The WebSocket
        handshake reads its ``101`` through this very method."""
        wire = (b'HTTP/1.1 101 Switching Protocols\r\n'
                b'upgrade: websocket\r\nconnection: upgrade\r\n\r\n')
        res = await _within(HTTP1ResponseRecipient().receive(
            _Canned(wire + b'\x81\x02hi')))
        assert res.status == 101

    @pytest.mark.asyncio
    async def test_a_101_leaves_the_switched_protocol_bytes_unread(self):
        """Reading a body after ``101`` would eat the first WebSocket frame."""
        reader = _Canned(b'HTTP/1.1 101 Switching Protocols\r\n'
                         b'upgrade: websocket\r\n\r\n\x81\x02hi')
        res = await _within(HTTP1ResponseRecipient().receive(reader))
        assert res.status == 101 and res.body == b''
        assert await reader.read(-1) == b'\x81\x02hi'

    @pytest.mark.asyncio
    async def test_an_endless_interim_stream_is_refused(self, monkeypatch):
        """Every per-read deadline is satisfied by a peer that keeps sending
        complete, well-formed interim responses.  Count is the only axis that
        sees it."""
        monkeypatch.setenv('BB_CLIENT_MAX_INTERIM_RESPONSES', '4')
        wire = b'HTTP/1.1 100 Continue\r\n\r\n' * 50
        with pytest.raises(ResponseTooLarge):
            await _within(HTTP1ResponseRecipient().receive(_Canned(wire)))

    @pytest.mark.asyncio
    async def test_the_interim_cap_admits_what_it_allows(self, monkeypatch):
        monkeypatch.setenv('BB_CLIENT_MAX_INTERIM_RESPONSES', '4')
        wire = (b'HTTP/1.1 100 Continue\r\n\r\n' * 4
                + b'HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nhi')
        res = await _within(HTTP1ResponseRecipient().receive(_Canned(wire)))
        assert (res.status, res.body) == (200, b'hi')

    @pytest.mark.asyncio
    async def test_the_interim_refusal_logs_its_cap(self, monkeypatch, caplog):
        monkeypatch.setenv('BB_CLIENT_MAX_INTERIM_RESPONSES', '2')
        caplog.set_level(logging.WARNING, logger='blackbull.caps')
        with pytest.raises(ResponseTooLarge):
            await _within(HTTP1ResponseRecipient().receive(
                _Canned(b'HTTP/1.1 100 Continue\r\n\r\n' * 20)))
        assert [r for r in caplog.records
                if getattr(r, 'cap', None) == 'client_max_interim_responses']

    @pytest.mark.asyncio
    async def test_streaming_also_reaches_the_final_response(self):
        wire = (b'HTTP/1.1 103 Early Hints\r\nlink: </a>\r\n\r\n'
                b'HTTP/1.1 200 OK\r\ncontent-length: 5\r\n\r\nHELLO')
        chunks = [c async for c in
                  HTTP1ResponseRecipient().stream(_Canned(wire))]
        assert b''.join(chunks) == b'HELLO'


# ----------------------------------------------------------------------
# RFC 9112 §6.3 item 1 — a response that cannot have a body (BLA-294)
# ----------------------------------------------------------------------

class TestBodylessResponses:
    @pytest.mark.parametrize('head', [
        b'HTTP/1.1 204 No Content\r\ncontent-length: 5\r\n\r\n',
        b'HTTP/1.1 304 Not Modified\r\ncontent-length: 5\r\n\r\n',
        b'HTTP/1.1 204 No Content\r\ntransfer-encoding: chunked\r\n\r\n',
    ], ids=['204-cl', '304-cl', '204-te'])
    @pytest.mark.asyncio
    async def test_a_bodyless_status_does_not_eat_the_next_response(self, head):
        """The sharpest form: it does not raise, and the second response still
        *looks* right — parsed from whatever survived the theft."""
        reader = _Canned(head + _NEXT)
        recipient = HTTP1ResponseRecipient()

        first = await _within(recipient.receive(reader))
        assert first.body == b''
        second = await _within(recipient.receive(reader))
        assert (second.status, second.body) == (201, b'ok'), \
            'the second response began inside the first'

    @pytest.mark.asyncio
    async def test_a_head_response_is_empty_and_does_not_stall(self):
        """Declared 1234, none sent, peer still connected: bounded only by
        ``BB_CLIENT_BODY_TIMEOUT`` before, which is a 30-second wait for a
        message that was complete at the blank line."""
        reader = _Held(b'HTTP/1.1 200 OK\r\ncontent-length: 1234\r\n\r\n')
        res = await _within(
            HTTP1ResponseRecipient().receive(reader, method='HEAD'))
        assert (res.status, res.body) == (200, b'')

    @pytest.mark.asyncio
    async def test_a_head_response_leaves_the_connection_usable(self):
        reader = _Canned(b'HTTP/1.1 200 OK\r\ncontent-length: 1234\r\n\r\n' + _NEXT)
        recipient = HTTP1ResponseRecipient()
        await _within(recipient.receive(reader, method='HEAD'))
        second = await _within(recipient.receive(reader, method='HEAD'))
        assert second.status == 201

    @pytest.mark.asyncio
    async def test_streaming_a_head_response_yields_nothing(self):
        reader = _Held(b'HTTP/1.1 200 OK\r\ncontent-length: 1234\r\n\r\n')
        chunks = [c async for c in
                  HTTP1ResponseRecipient().stream(reader, method='HEAD')]
        assert chunks == []

    @pytest.mark.asyncio
    async def test_a_successful_connect_has_no_body(self):
        """RFC 9110 §9.3.6 — a 2xx to CONNECT switches to a tunnel, so the
        octets after the blank line are the tunnel's, not a body's."""
        reader = _Canned(b'HTTP/1.1 200 OK\r\ncontent-length: 7\r\n\r\nTUNNEL!')
        res = await _within(
            HTTP1ResponseRecipient().receive(reader, method='CONNECT'))
        assert res.body == b''
        assert await reader.read(-1) == b'TUNNEL!'

    @pytest.mark.asyncio
    async def test_a_failed_connect_still_has_its_body(self):
        """Only 2xx opens the tunnel; an error response is an ordinary one."""
        reader = _Canned(b'HTTP/1.1 403 Forbidden\r\ncontent-length: 2\r\n\r\nno')
        res = await _within(
            HTTP1ResponseRecipient().receive(reader, method='CONNECT'))
        assert res.body == b'no'

    @pytest.mark.asyncio
    async def test_the_client_threads_the_request_method_down(self):
        """The rule is useless if the method never reaches the decision.  A
        recipient-level test cannot see that wiring, so this drives the public
        API instead."""
        client = HTTP1Client('localhost', 1)
        client._reader = _Held(b'HTTP/1.1 200 OK\r\ncontent-length: 1234\r\n\r\n')
        client._writer = _NullWriter()
        res = await _within(client.request('HEAD', '/'))
        assert (res.status, res.body) == (200, b'')

    @pytest.mark.asyncio
    async def test_the_streaming_api_threads_the_method_too(self):
        client = HTTP1Client('localhost', 1)
        client._reader = _Held(b'HTTP/1.1 200 OK\r\ncontent-length: 1234\r\n\r\n')
        client._writer = _NullWriter()
        chunks = [c async for c in client.stream('HEAD', '/')]
        assert chunks == []


# ----------------------------------------------------------------------
# RFC 9112 §6.3 items 3-4 — the transfer-coding list (BLA-294)
# ----------------------------------------------------------------------

class TestTransferCodingList:
    @pytest.mark.asyncio
    async def test_a_coding_list_ending_in_chunked_is_dechunked(self):
        """``te == b'chunked'`` is an exact match, so ``gzip, chunked`` fell
        through to the ``Content-Length`` branch — which found none, returned
        an empty body, and left a whole chunked message on the wire."""
        wire = (b'HTTP/1.1 200 OK\r\ntransfer-encoding: gzip, chunked\r\n\r\n'
                b'4\r\nGZIP\r\n0\r\n\r\n')
        recipient = HTTP1ResponseRecipient()
        reader = _Canned(wire + _NEXT)
        first = await _within(recipient.receive(reader))
        assert first.body == b'GZIP', 'the chunked framing was not read'
        second = await _within(recipient.receive(reader))
        assert second.status == 201, 'the chunked body was left on the wire'

    @pytest.mark.asyncio
    async def test_the_coding_list_may_be_split_across_fields(self):
        wire = (b'HTTP/1.1 200 OK\r\ntransfer-encoding: gzip\r\n'
                b'transfer-encoding: chunked\r\n\r\n4\r\nGZIP\r\n0\r\n\r\n')
        res = await _within(HTTP1ResponseRecipient().receive(_Canned(wire)))
        assert res.body == b'GZIP'

    @pytest.mark.asyncio
    async def test_chunked_not_final_is_read_to_eof(self):
        """§6.3 item 4, response branch: chunked present but not final means
        the length is the connection's, not the framing's."""
        wire = (b'HTTP/1.1 200 OK\r\ntransfer-encoding: chunked, gzip\r\n\r\n'
                b'raw octets to the close')
        res = await _within(HTTP1ResponseRecipient().receive(_Canned(wire)))
        assert res.body == b'raw octets to the close'

    @pytest.mark.asyncio
    async def test_transfer_encoding_with_content_length_is_refused(self):
        """§6.3 item 3 — the two disagree by construction, and the message
        'ought to be handled as an error'.  The server refuses the same shape
        on the request side; this is that rule facing the other way."""
        wire = (b'HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n'
                b'content-length: 5\r\n\r\n2\r\nhi\r\n0\r\n\r\n')
        recipient = HTTP1ResponseRecipient()
        with pytest.raises(ProtocolError):
            await _within(recipient.receive(_Canned(wire)))
        assert recipient.framing_broken


# ----------------------------------------------------------------------
# RFC 9112 §6.3 item 8 — a body delimited by the close (BLA-294)
# ----------------------------------------------------------------------

class TestCloseDelimitedBody:
    @pytest.mark.asyncio
    async def test_a_lengthless_response_body_is_read_not_dropped(self):
        """No ``Content-Length``, no ``Transfer-Encoding``, a body, then EOF.
        The comment at the old ``return b''`` cited item 1's rule and applied
        it to item 8, which says the opposite."""
        wire = b'HTTP/1.1 200 OK\r\ncontent-type: text/plain\r\n\r\nhello world'
        res = await _within(HTTP1ResponseRecipient().receive(_Canned(wire)))
        assert (res.status, res.body) == (200, b'hello world')

    @pytest.mark.asyncio
    async def test_streaming_a_lengthless_body_yields_it(self):
        wire = b'HTTP/1.1 200 OK\r\ncontent-type: text/plain\r\n\r\nhello world'
        chunks = [c async for c in HTTP1ResponseRecipient().stream(_Canned(wire))]
        assert b''.join(chunks) == b'hello world'

    @pytest.mark.asyncio
    async def test_a_close_delimited_body_is_bounded_by_the_total(self, monkeypatch):
        """Read-until-EOF is an accumulation the peer sizes, which is the one
        shape the total column exists for.  The declared path is refused on
        the declaration; this one can only be refused as it arrives."""
        monkeypatch.setenv('BB_CLIENT_BODY_MAX_TOTAL', '16')
        wire = b'HTTP/1.1 200 OK\r\ncontent-type: text/plain\r\n\r\n' + b'x' * 4096
        with pytest.raises(ResponseTooLarge):
            await _within(HTTP1ResponseRecipient().receive(_Canned(wire)))

    @pytest.mark.asyncio
    async def test_the_close_delimited_total_logs_its_cap(self, monkeypatch, caplog):
        monkeypatch.setenv('BB_CLIENT_BODY_MAX_TOTAL', '16')
        caplog.set_level(logging.WARNING, logger='blackbull.caps')
        wire = b'HTTP/1.1 200 OK\r\ncontent-type: text/plain\r\n\r\n' + b'x' * 4096
        with pytest.raises(ResponseTooLarge):
            await _within(HTTP1ResponseRecipient().receive(_Canned(wire)))
        assert [r for r in caplog.records
                if getattr(r, 'cap', None) == 'client_body_max_total']

    @pytest.mark.asyncio
    async def test_a_close_delimited_response_ends_the_connection(self):
        """Its length *is* the close, so there is no second response to read
        and no honest way to send another request."""
        recipient = HTTP1ResponseRecipient()
        await _within(recipient.receive(
            _Canned(b'HTTP/1.1 200 OK\r\ncontent-type: text/plain\r\n\r\nbody')))
        assert recipient.connection_exhausted
        assert not recipient.framing_broken, 'the message ended where it said'

    @pytest.mark.asyncio
    async def test_the_client_refuses_to_reuse_a_closed_connection(self):
        client = HTTP1Client('localhost', 1)
        client._reader = _Canned(
            b'HTTP/1.1 200 OK\r\ncontent-type: text/plain\r\n\r\nbody')
        client._writer = _NullWriter()
        res = await _within(client.request('GET', '/'))
        assert res.body == b'body'
        assert client._connection_exhausted
        # The type alone proves nothing: a second read of a spent reader hits
        # EOF and raises the same ConnectionError, so this passes just as well
        # with the mechanism deleted.  The message is what names the mechanism.
        with pytest.raises(ConnectionError, match='delimited by the connection'):
            await _within(client.request('GET', '/second'))

    @pytest.mark.asyncio
    async def test_the_streaming_api_spends_the_connection_too(self):
        client = HTTP1Client('localhost', 1)
        client._reader = _Canned(
            b'HTTP/1.1 200 OK\r\ncontent-type: text/plain\r\n\r\nbody')
        client._writer = _NullWriter()
        assert b''.join([c async for c in client.stream('GET', '/')]) == b'body'
        assert client._connection_exhausted
        with pytest.raises(ConnectionError, match='delimited by the connection'):
            await _within(client.request('GET', '/second'))

    @pytest.mark.asyncio
    async def test_a_declared_body_still_ends_where_it_declared(self):
        """The control: adding item 8 must not turn every response into one."""
        recipient = HTTP1ResponseRecipient()
        reader = _Canned(b'HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nhi' + _NEXT)
        first = await _within(recipient.receive(reader))
        assert first.body == b'hi'
        assert not recipient.connection_exhausted
        assert (await _within(recipient.receive(reader))).status == 201


# ----------------------------------------------------------------------
# RFC 9110 §5.6.1.2 — a list's empty elements are not elements (BLA-294)
# ----------------------------------------------------------------------

class TestTheCodingListIsAList:
    @pytest.mark.parametrize('te', [
        b'chunked', b'chunked,', b'chunked ,', b', chunked', b',chunked,',
    ], ids=['plain', 'trailing', 'trailing-ws', 'leading', 'both'])
    @pytest.mark.asyncio
    async def test_empty_elements_do_not_change_the_framing(self, te):
        """§5.6.1.2 is a MUST: *"A recipient of such a list that contains an
        empty element MUST treat it as if the empty element were not
        present."*  Judged instead, ``chunked,`` ends in the empty coding —
        not chunked, therefore close-delimited — so the chunked body and every
        response pipelined behind it became this response's body, silently.
        ``chunked,`` is a standard smuggling probe, and the leading spelling
        happening to work made the two disagree."""
        wire = (b'HTTP/1.1 200 OK\r\ntransfer-encoding: ' + te +
                b'\r\n\r\n2\r\nhi\r\n0\r\n\r\n')
        recipient = HTTP1ResponseRecipient()
        reader = _Canned(wire + _NEXT)

        first = await _within(recipient.receive(reader))
        assert first.body == b'hi', 'an empty element changed the framing'
        second = await _within(recipient.receive(reader))
        assert (second.status, second.body) == (201, b'ok'), \
            'the response behind it was swallowed into the first body'

    @pytest.mark.asyncio
    async def test_chunked_applied_twice_is_refused(self):
        """RFC 9112 §7.1 makes applying chunked more than once a sender MUST
        NOT, so no conforming peer sends it — and the server already refuses
        this exact field value on the request side.  Reading it as anything
        would leave the codebase holding two answers to one message."""
        wire = (b'HTTP/1.1 200 OK\r\ntransfer-encoding: chunked, chunked'
                b'\r\n\r\n2\r\nhi\r\n0\r\n\r\n')
        with pytest.raises(ProtocolError):
            await _within(HTTP1ResponseRecipient().receive(_Canned(wire)))

    @pytest.mark.asyncio
    async def test_a_transfer_encoding_naming_no_coding_uses_close(self):
        """Physical TE presence still overrides Content-Length semantics;
        an empty parsed list therefore remains close-delimited."""
        wire = b'HTTP/1.1 200 OK\r\ntransfer-encoding: ,\r\n\r\nbody'
        response = await _within(
            HTTP1ResponseRecipient().receive(_Canned(wire)))
        assert response.body == b'body'


# ----------------------------------------------------------------------
# RFC 9112 §4 — status-code = 3DIGIT (BLA-294)
# ----------------------------------------------------------------------

class TestTheStatusNumeral:
    @pytest.mark.parametrize('numeral', [
        b'2_0_4',   # int() takes underscore separators
        b'+200',
        b'-204',
        b'20',
        b'0204',
    ])
    @pytest.mark.asyncio
    async def test_a_non_3digit_status_is_refused(self, numeral):
        """The status was only reported before; §6.3 items 1 and 2 now let it
        decide whether a body is read at all.  ``2_0_4`` suppressed a body
        that an intermediary enforcing the grammar had framed — a parser
        differential of exactly the shape the chunk-size numeral had."""
        wire = (b'HTTP/1.1 ' + numeral + b' x\r\ncontent-length: 5\r\n\r\nHELLO')
        with pytest.raises(ProtocolError):
            await _within(HTTP1ResponseRecipient().receive(_Canned(wire)))

    @pytest.mark.asyncio
    async def test_three_digits_still_work(self):
        wire = b'HTTP/1.1 599 Odd\r\ncontent-length: 2\r\n\r\nhi'
        res = await _within(HTTP1ResponseRecipient().receive(_Canned(wire)))
        assert (res.status, res.body) == (599, b'hi')


# ----------------------------------------------------------------------
# What the interim loop costs, and who owns it (BLA-295)
# ----------------------------------------------------------------------

class TestTheInterimAggregate:
    @pytest.mark.asyncio
    async def test_the_count_bounds_the_head_deadline_it_multiplies(self,
                                                                    monkeypatch):
        """``BB_CLIENT_HEAD_TIMEOUT`` is per head and must stay per head: a
        ``103 Early Hints`` is a peer saying "still working", and judging that
        wait refuses a slow query for being slow.  So the loop spends one
        deadline per interim, and the count is the only thing bounding the
        aggregate — which makes the worst case ``(limit + 1)`` deadlines, not
        one.  A cap that did not bound it would leave the time column with no
        owner at all."""
        monkeypatch.setenv('BB_CLIENT_HEAD_TIMEOUT', '0.3')
        monkeypatch.setenv('BB_CLIENT_MAX_INTERIM_RESPONSES', '3')

        class _Dripping(AbstractReader):
            """One interim per 0.1 s — inside every head deadline, forever."""

            def __init__(self) -> None:
                self.heads = 0
                self._buf, self._pos = b'', 0

            async def read(self, n: int = -1) -> bytes:
                if self._pos >= len(self._buf):
                    await asyncio.sleep(0.1)
                    self.heads += 1
                    self._buf += b'HTTP/1.1 100 Continue\r\n\r\n'
                out = (self._buf[self._pos:] if n < 0
                       else self._buf[self._pos:self._pos + n])
                self._pos += len(out)
                return out

        reader = _Dripping()
        with pytest.raises(ResponseTooLarge):
            await _within(HTTP1ResponseRecipient().receive(reader), seconds=3.0)
        assert reader.heads <= 4, (
            f'a limit of 3 spent {reader.heads} head deadlines; the aggregate '
            f'is bounded by the count or by nothing')


# ----------------------------------------------------------------------
# The fault-injection primitives observe what the peer sent (BLA-295)
# ----------------------------------------------------------------------

class TestTheRawInstrumentStillSeesInterims:
    @pytest.mark.asyncio
    async def test_read_response_surfaces_a_100_continue(self):
        """``read_response`` exists to drive a peer and report what it sent.
        Skipping interims there would answer a different question — the same
        exemption ``_abandon`` already makes for these primitives."""
        client = HTTP1Client('localhost', 1)
        client._reader = _Canned(b'HTTP/1.1 100 Continue\r\n\r\n'
                                 b'HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nhi')
        res = await _within(client.read_response())
        assert res.status == 100

    @pytest.mark.asyncio
    async def test_the_production_api_still_skips_them(self):
        """The control: the exemption is the instrument's, not the client's."""
        client = HTTP1Client('localhost', 1)
        client._reader = _Canned(b'HTTP/1.1 100 Continue\r\n\r\n'
                                 b'HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nhi')
        client._writer = _NullWriter()
        res = await _within(client.request('GET', '/'))
        assert (res.status, res.body) == (200, b'hi')

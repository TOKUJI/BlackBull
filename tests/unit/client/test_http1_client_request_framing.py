"""RFC 9112 §6 — one unambiguous framing per outbound HTTP/1.1 request.

``prepare`` used to decide the body's framing and validate a caller's framing
fields as two unrelated steps.  The body's Python type picked ``Content-Length``
or ``chunked``, and a caller-supplied field was checked only where a check
happened to exist — so the two could disagree on the wire:

* a fixed body with a caller ``Transfer-Encoding`` emitted **both** fields and
  wrote an unchunked body;
* a stream body with a caller ``Content-Length`` emitted **both** fields and
  wrote chunk syntax;
* only the first ``Content-Length`` was compared, so ``5, 9`` went out as two
  competing message boundaries (the CL.CL shape);
* a value that is not ``1*DIGIT`` (`` 5 ``, ``+5``) was relayed verbatim;
* a coding the client cannot produce (``gzip``) was advertised and never
  applied, so the peer could not decode the body it was told to expect.

The server's response sender already owns framing the other way — it takes one
canonical ``Content-Length`` and runs a declared-length stream raw while
checking its total.  These tests hold the client to that contract.  The one
difference is ``Transfer-Encoding``: a response sender drops an
application-supplied field, but a request sender that dropped one would change
what the body means, so it refuses instead of rewriting.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.client.exceptions import ConnectionError, ProtocolError
from blackbull.client.http1 import HTTP1Client, HTTP1RequestSender
from blackbull.headers import Headers
from blackbull.server.recipient import AbstractReader
from blackbull.server.sender import AbstractWriter


class _Writer(AbstractWriter):
    def __init__(self) -> None:
        self.data = bytearray()

    async def write(self, data: bytes) -> None:
        self.data.extend(data)

    @property
    def head(self) -> bytes:
        return bytes(self.data).split(b'\r\n\r\n', 1)[0]

    @property
    def body(self) -> bytes:
        return bytes(self.data).split(b'\r\n\r\n', 1)[1]


class _Reader(AbstractReader):
    """A canned response, consumed from the top."""

    def __init__(self, data: bytes) -> None:
        self.data, self.pos = data, 0

    async def read(self, n: int = -1) -> bytes:
        if n < 0:
            n = len(self.data) - self.pos
        end = min(self.pos + n, len(self.data))
        out = self.data[self.pos:end]
        self.pos = end
        return out


class _RawWriter:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


async def _send(method='POST', path: str = '/x', headers=(), body=b'') -> _Writer:
    writer = _Writer()
    h = Headers([(b'host', b'example.test'), *headers])
    await HTTP1RequestSender(writer).send(method, path, h, body)
    return writer


def framing(writer: _Writer) -> list[tuple[bytes, bytes]]:
    """The framing fields of a sent request, in order."""
    out: list[tuple[bytes, bytes]] = []
    for line in writer.head.split(b'\r\n')[1:]:
        name, _, value = line.partition(b':')
        if name.lower() in (b'content-length', b'transfer-encoding'):
            out.append((name.lower(), value.strip()))
    return out


async def _chunks(*chunks: bytes):
    for chunk in chunks:
        yield chunk


# ---------------------------------------------------------------------------
# The controls — correct before and after
# ---------------------------------------------------------------------------

class TestControls:
    @pytest.mark.asyncio
    async def test_a_fixed_body_declares_its_length(self):
        assert framing(await _send(body=b'hello')) == [(b'content-length', b'5')]

    @pytest.mark.asyncio
    async def test_a_stream_body_is_chunked(self):
        assert framing(await _send(body=_chunks(b'aa'))) == [
            (b'transfer-encoding', b'chunked')]

    @pytest.mark.asyncio
    async def test_an_empty_body_on_a_body_allowed_method_is_zero(self):
        assert framing(await _send(method='POST')) == [(b'content-length', b'0')]

    @pytest.mark.asyncio
    async def test_an_empty_body_on_a_body_less_method_is_undeclared(self):
        assert framing(await _send(method='GET')) == []

    @pytest.mark.asyncio
    async def test_an_explicit_zero_length_survives_on_a_body_less_method(self):
        assert framing(await _send(
            method='GET', headers=[(b'content-length', b'0')])) == [
            (b'content-length', b'0')]

    @pytest.mark.asyncio
    async def test_a_length_that_disagrees_with_the_body_is_refused(self):
        with pytest.raises(ProtocolError):
            await _send(headers=[(b'content-length', b'3')], body=b'hello')

    @pytest.mark.asyncio
    async def test_an_empty_body_with_a_declared_length_is_refused(self):
        with pytest.raises(ProtocolError):
            await _send(headers=[(b'content-length', b'5')], body=b'')


# ---------------------------------------------------------------------------
# One framing per message
# ---------------------------------------------------------------------------

class TestOneFraming:
    @pytest.mark.asyncio
    async def test_a_fixed_body_advertises_no_transfer_encoding(self):
        """A body written raw must not also claim to be chunk-framed."""
        w = await _send(
            headers=[(b'transfer-encoding', b'chunked')], body=b'hello')
        assert framing(w) == [(b'content-length', b'5')]
        assert w.body == b'hello'

    @pytest.mark.asyncio
    async def test_a_stream_body_with_a_declared_length_is_not_chunk_framed(self):
        w = await _send(
            headers=[(b'content-length', b'3')], body=_chunks(b'ab', b'c'))
        assert framing(w) == [(b'content-length', b'3')]
        assert w.body == b'abc'

    @pytest.mark.asyncio
    async def test_a_coding_the_client_cannot_write_is_refused(self):
        """`gzip, chunked` with a stream the caller compressed itself is a
        message the client *can* carry — but only by keeping the field.  It
        writes plain chunked framing, so dropping the coding would deliver the
        compressed octets as the payload and change what the body means while
        looking identical.  Refusing says so."""
        with pytest.raises(ProtocolError):
            await _send(headers=[(b'transfer-encoding', b'gzip, chunked')],
                        body=_chunks(b'aa'))

    @pytest.mark.parametrize('value', [
        b'gzip, chunked',    # a coding the caller applied
        b'chunked, gzip',    # chunked not last
        b'chunked; ext=1',   # a parameter we would not write
        b'chunked, chunked', # chunked applied twice
        b'gzip',             # no chunked at all
    ])
    @pytest.mark.asyncio
    async def test_only_the_lone_chunked_coding_is_accepted(self, value):
        with pytest.raises(ProtocolError):
            await _send(headers=[(b'transfer-encoding', value)],
                        body=_chunks(b'aa'))

    @pytest.mark.asyncio
    async def test_a_transfer_encoding_beside_a_content_length_is_refused(self):
        """RFC 9112 §6.2 forbids one message from carrying both; which of the
        two describes the body would be the recipient's guess."""
        with pytest.raises(ProtocolError):
            await _send(headers=[(b'transfer-encoding', b'chunked'),
                                 (b'content-length', b'2')],
                        body=_chunks(b'aa'))

    @pytest.mark.asyncio
    async def test_a_refused_transfer_encoding_writes_nothing(self):
        writer = _Writer()
        with pytest.raises(ProtocolError):
            await HTTP1RequestSender(writer).send(
                'POST', '/x',
                Headers([(b'host', b'h'),
                         (b'transfer-encoding', b'gzip, chunked')]),
                _chunks(b'aa'))
        assert bytes(writer.data) == b''

    @pytest.mark.asyncio
    async def test_the_lone_chunked_coding_is_the_one_we_write(self):
        w = await _send(headers=[(b'transfer-encoding', b'chunked')],
                        body=_chunks(b'aa'))
        assert framing(w) == [(b'transfer-encoding', b'chunked')]
        assert w.body == b'2\r\naa\r\n0\r\n\r\n'

    @pytest.mark.asyncio
    async def test_a_send_leaves_the_callers_header_set_alone(self):
        """Reusing one `Headers` for two requests used to append a second
        `content-length` to it — the CL.CL shape reached through an ordinary
        pattern rather than through a deliberate duplicate."""
        headers = Headers([(b'host', b'example.test')])
        before = list(headers)
        for _ in range(2):
            w = _Writer()
            await HTTP1RequestSender(w).send('POST', '/x', headers, b'hello')
            assert framing(w) == [(b'content-length', b'5')]
        assert list(headers) == before

    @pytest.mark.parametrize('name', [b'content-length', b'Content-Length',
                                      b'CONTENT-LENGTH'])
    @pytest.mark.asyncio
    async def test_a_framing_field_is_found_whatever_its_casing(self, name):
        """RFC 9110 §5.1: the name is case-insensitive, so a caller's casing
        neither hides the field from the check nor survives into the output."""
        w = await _send(headers=[(name, b'5')], body=b'hello')
        assert framing(w) == [(b'content-length', b'5')]

    @pytest.mark.parametrize('name', [b'transfer-encoding',
                                      b'Transfer-Encoding'])
    @pytest.mark.asyncio
    async def test_the_transfer_encoding_name_is_case_insensitive(self, name):
        w = await _send(headers=[(name, b'chunked')], body=b'hello')
        assert framing(w) == [(b'content-length', b'5')]


# ---------------------------------------------------------------------------
# Content-Length is one value, written once, in 1*DIGIT
# ---------------------------------------------------------------------------

class TestContentLengthValue:
    @pytest.mark.asyncio
    async def test_identical_duplicates_are_emitted_once(self):
        w = await _send(headers=[(b'content-length', b'5'),
                                (b'content-length', b'5')], body=b'hello')
        assert framing(w) == [(b'content-length', b'5')]

    @pytest.mark.asyncio
    async def test_conflicting_duplicates_are_refused(self):
        with pytest.raises(ProtocolError):
            await _send(headers=[(b'content-length', b'5'),
                                 (b'content-length', b'9')], body=b'hello')

    @pytest.mark.asyncio
    async def test_comma_joined_members_are_emitted_as_one_field(self):
        w = await _send(headers=[(b'content-length', b'5, 5')], body=b'hello')
        assert framing(w) == [(b'content-length', b'5')]

    @pytest.mark.asyncio
    async def test_comma_joined_conflicting_members_are_refused(self):
        with pytest.raises(ProtocolError):
            await _send(headers=[(b'content-length', b'5, 9')], body=b'hello')

    @pytest.mark.parametrize('value', [
        b'+5',       # sign
        b'-5',       # sign
        b'0x5',      # not decimal
        b'5.0',      # not a digit run
        b'',         # empty
        b'5 5',      # two numerals
    ])
    @pytest.mark.asyncio
    async def test_a_value_that_is_not_one_digit_run_is_refused(self, value):
        with pytest.raises(ProtocolError):
            await _send(headers=[(b'content-length', value)], body=b'hello')

    @pytest.mark.parametrize('value', [b'5,', b',5', b'5,,5', b','])
    @pytest.mark.asyncio
    async def test_an_empty_comma_member_is_refused(self, value):
        """A list member is ``1*DIGIT`` after its OWS; an empty one leaves
        the number of lengths undeclared, which is the CL.CL shape."""
        with pytest.raises(ProtocolError):
            await _send(headers=[(b'content-length', value)], body=b'hello')

    @pytest.mark.parametrize('value', [b' 5 ', b'5\t', b'\t5'])
    @pytest.mark.asyncio
    async def test_ows_around_a_value_is_not_part_of_it(self, value):
        """``field-line = field-name ":" OWS field-value OWS`` — the OWS is
        the line's, so the numeral is ``5`` and that is what goes out."""
        w = await _send(headers=[(b'content-length', value)], body=b'hello')
        assert framing(w) == [(b'content-length', b'5')]

    @pytest.mark.asyncio
    async def test_leading_zeros_are_the_same_number(self):
        w = await _send(headers=[(b'content-length', b'005')], body=b'hello')
        assert framing(w) == [(b'content-length', b'5')]


# ---------------------------------------------------------------------------
# Method-dependent body policy
# ---------------------------------------------------------------------------

class TestMethodBodyPolicy:
    @pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE'])
    @pytest.mark.asyncio
    async def test_a_body_allowed_method_declares_zero(self, method):
        assert framing(await _send(method=method)) == [
            (b'content-length', b'0')]

    @pytest.mark.parametrize('method', ['GET', 'HEAD', 'OPTIONS', 'TRACE',
                                        'CONNECT'])
    @pytest.mark.asyncio
    async def test_a_body_less_method_declares_nothing(self, method):
        assert framing(await _send(method=method)) == []

    @pytest.mark.asyncio
    async def test_an_unknown_method_with_an_empty_body_declares_nothing(self):
        assert framing(await _send(method='BREW')) == []

    @pytest.mark.asyncio
    async def test_an_upgrade_request_declares_nothing(self):
        w = await _send(method='GET', headers=[
            (b'connection', b'upgrade'), (b'upgrade', b'websocket')])
        assert framing(w) == []

    @pytest.mark.parametrize('method,expected', [
        ('POST', [(b'content-length', b'0')]),
        ('GET', []),
    ])
    @pytest.mark.asyncio
    async def test_a_transfer_encoding_alone_declares_no_body(self, method,
                                                              expected):
        """``Transfer-Encoding`` describes bytes on the transport, and with no
        body there are none — so it goes and the method's own empty-body
        policy decides what is left."""
        assert framing(await _send(method=method, headers=[
            (b'transfer-encoding', b'chunked')])) == expected

    @pytest.mark.asyncio
    async def test_an_explicit_zero_length_on_a_body_allowed_method_is_kept(self):
        w = await _send(method='POST', headers=[(b'content-length', b'0')])
        assert framing(w) == [(b'content-length', b'0')]


# ---------------------------------------------------------------------------
# A declared-length stream is checked against its declared total
# ---------------------------------------------------------------------------

class TestDeclaredLengthStream:
    @pytest.mark.asyncio
    async def test_a_stream_that_ends_short_is_refused(self):
        writer = _Writer()
        with pytest.raises(ProtocolError):
            await HTTP1RequestSender(writer).send(
                'POST', '/x',
                Headers([(b'host', b'h'), (b'content-length', b'3')]),
                _chunks(b'ab'))
        assert writer.body == b'ab'

    @pytest.mark.asyncio
    async def test_a_stream_that_crosses_the_boundary_is_refused(self):
        writer = _Writer()
        with pytest.raises(ProtocolError):
            await HTTP1RequestSender(writer).send(
                'POST', '/x',
                Headers([(b'host', b'h'), (b'content-length', b'3')]),
                _chunks(b'ab', b'cd'))
        assert writer.body == b'ab'

    @pytest.mark.asyncio
    async def test_a_stream_that_hits_the_boundary_exactly_is_accepted(self):
        w = await _send(headers=[(b'content-length', b'3')],
                        body=_chunks(b'ab', b'c'))
        assert w.body == b'abc'

    @pytest.mark.asyncio
    async def test_an_empty_declared_stream_sends_no_octets(self):
        w = await _send(headers=[(b'content-length', b'0')], body=_chunks())
        assert framing(w) == [(b'content-length', b'0')]
        assert w.body == b''

    @pytest.mark.asyncio
    async def test_empty_chunks_do_not_contribute_to_the_total(self):
        w = await _send(headers=[(b'content-length', b'3')],
                        body=_chunks(b'', b'ab', b'', b'c', b''))
        assert w.body == b'abc'


# ---------------------------------------------------------------------------
# A refusal must cost the connection only what it actually damaged
# ---------------------------------------------------------------------------

def _client(reader: _Reader) -> HTTP1Client:
    client = HTTP1Client('example.test', 80)
    client._reader = reader
    client._writer = _Writer()
    return client


_OK = b'HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok'


async def _call(client: HTTP1Client, api: str, **kwargs) -> None:
    """Drive one request through either of the two normal entry points."""
    if api == 'request':
        await client.request('POST', '/', **kwargs)
    else:
        async for _ in client.stream('POST', '/', **kwargs):
            pass


class TestConnectionFate:
    @pytest.mark.parametrize('api', ['request', 'stream'])
    @pytest.mark.asyncio
    async def test_a_refused_prepare_leaves_the_connection_reusable(self, api):
        client = _client(_Reader(_OK + _OK))
        client._raw_writer = _RawWriter()  # type: ignore[assignment]

        with pytest.raises(ProtocolError):
            await _call(client, api, body=b'hello', headers=[
                (b'content-length', b'5'), (b'content-length', b'9')])

        assert bytes(client._writer.data) == b''  # type: ignore[union-attr]
        assert client._active_response is None
        assert client._reusable is True
        assert (await client.request('GET', '/next')).body == b'ok'

    @pytest.mark.parametrize('api', ['request', 'stream'])
    @pytest.mark.asyncio
    async def test_a_declared_stream_that_runs_over_retires_the_connection(
            self, api):
        client = _client(_Reader(_OK))
        raw = _RawWriter()
        client._raw_writer = raw  # type: ignore[assignment]

        with pytest.raises(ProtocolError):
            await _call(client, api, headers=[(b'content-length', b'3')],
                        body=_chunks(b'ab', b'cd'))

        assert bytes(client._writer.data).endswith(b'\r\n\r\nab')  # type: ignore[union-attr]
        assert client._reusable is False
        assert client._framing_broken is True
        assert raw.close_calls == 1
        with pytest.raises(ConnectionError):
            await client.request('GET', '/next')

    @pytest.mark.parametrize('api', ['request', 'stream'])
    @pytest.mark.asyncio
    async def test_a_declared_stream_that_ends_short_retires_the_connection(
            self, api):
        client = _client(_Reader(_OK))
        raw = _RawWriter()
        client._raw_writer = raw  # type: ignore[assignment]

        with pytest.raises(ProtocolError):
            await _call(client, api, headers=[(b'content-length', b'3')],
                        body=_chunks(b'ab'))

        assert bytes(client._writer.data).endswith(b'\r\n\r\nab')  # type: ignore[union-attr]
        assert client._reusable is False
        assert raw.close_calls == 1
        with pytest.raises(ConnectionError):
            await client.request('GET', '/next')

    @pytest.mark.asyncio
    async def test_cancelling_mid_stream_retires_the_connection(self):
        """Cancellation is nobody's framing error, but two octets of a
        three-octet body are already out."""
        client = _client(_Reader(_OK))
        raw = _RawWriter()
        client._raw_writer = raw  # type: ignore[assignment]
        started = asyncio.Event()

        async def slow():
            yield b'ab'
            started.set()
            await asyncio.sleep(3600)
            yield b'c'

        task = asyncio.ensure_future(client.request(
            'POST', '/', headers=[(b'content-length', b'3')], body=slow()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert client._reusable is False
        assert raw.close_calls == 1
        with pytest.raises(ConnectionError):
            await client.request('GET', '/next')

    @pytest.mark.asyncio
    async def test_a_body_source_that_fails_midway_retires_the_connection(self):
        """Not our refusal but the caller's iterator: the head and part of the
        body are already out, so the next response read would begin inside
        this request's body."""
        client = _client(_Reader(_OK))
        raw = _RawWriter()
        client._raw_writer = raw  # type: ignore[assignment]

        async def boom():
            yield b'ab'
            raise RuntimeError('body source failed')

        with pytest.raises(RuntimeError):
            await client.request('POST', '/', headers=[
                (b'content-length', b'4')], body=boom())

        assert bytes(client._writer.data).endswith(b'\r\n\r\nab')  # type: ignore[union-attr]
        assert client._reusable is False
        assert raw.close_calls == 1

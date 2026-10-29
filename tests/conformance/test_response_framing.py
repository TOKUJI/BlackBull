"""One response rule on both protocols: what a generated response may carry.

Every row is sent as HTTP/1.1 and as HTTP/2, through the native and the
compatibility lane, and is judged by what reaches the peer: the status, the
length field, the content, the trailers, and whether the next request or
stream on the same connection is still served.

A length the application declares must hold.  Broken before anything was
sent, the send that broke it raises and nothing reaches the wire, so the
application can still answer 500.  Broken after the head left, the response
cannot be repaired: HTTP/1.1 closes the connection, HTTP/2 resets the stream.
"""
from __future__ import annotations

import asyncio
import itertools

import pytest
from hpack import Decoder, Encoder

from blackbull.native import NativeResponse
from tests.conformance.http2._harness import h2_connect, h2_section
from tests.harness import read_h2_frame, served

pytestmark = pytest.mark.asyncio

TRAILERS = [(b'x-checksum', b'abc')]
LENGTHS = {'none': None, 'zero': (b'0',), 'five': (b'5',), 'duplicate': (b'5', b'5'),
           'non-canonical': (b'05',), 'malformed': (b'abc',), 'conflicting': (b'5', b'6')}
BODIES = {'none': None, 'empty': (b'',), 'exact': (b'hello',), 'short': (b'hel',),
          'long': (b'hello!',), 'exact-split': (b'he', b'llo'),
          'short-split': (b'he', b'l'), 'long-split': (b'hel', b'lo!')}
EXCHANGES = {'GET 200': ('GET', 200), 'HEAD 200': ('HEAD', 200), 'GET 204': ('GET', 204),
             'GET 205': ('GET', 205), 'GET 304': ('GET', 304)}


def _expected(method, status, lengths, body, trailers, protocol):
    """What the peer must see for one row: ``(outcome, length, content, trailers)``.

    *outcome* is ``'ok'``, ``'refused'`` (the send raised before anything was
    sent) or ``'broken'`` (the head left, then the connection or stream ended).
    *length* is the content-length the response must state, ``None`` for none,
    and ``...`` where the transport is free to say what it likes.
    """
    sent = b''.join(body or ())
    contentless = status in (204, 205)
    metadata = method == 'HEAD' or status == 304
    if protocol == 'http1' and trailers and not (contentless or metadata):
        # Chunked carries the trailers and no length (RFC 9112 §6.1): the
        # field is discarded, so there is nothing to hold the content to.
        return 'ok', None, sent, TRAILERS
    declared = None
    if lengths is not None and not contentless:
        values = set(lengths)
        if len(values) != 1 or not lengths[0].isdigit():
            return 'refused', None, b'', []
        declared = int(lengths[0])
    if contentless:
        return 'ok', None, b'', []
    if metadata:
        return 'ok', declared if declared is not None else ..., b'', []
    if declared is not None and declared != len(sent):
        first = body[0] if body else b''
        if body is not None and len(body) > 1 and len(first) <= declared:
            return 'broken', None, b'', []
        return 'refused', None, b'', []
    return 'ok', declared if declared is not None else ..., sent, TRAILERS if trailers else []


#: How the response is handed to the sender: one event per part, one
#: combined native object, or with its trailer section split in two.
FORMS = ('events', 'native', 'split-trailers')


def _rows():
    for (name, (method, status)), (lname, lengths), (bname, body), trailers, form in (
            itertools.product(EXCHANGES.items(), LENGTHS.items(), BODIES.items(),
                              (False, True), FORMS)):
        if body is None and not trailers:
            continue    # nothing would end the response
        if form == 'native' and body is not None and len(body) > 1:
            continue    # one object carries one body part
        if form == 'split-trailers' and not trailers:
            continue
        yield pytest.param(method, status, lengths, body, trailers, form,
                           id=f'{name}-{lname}-{bname}{"-trailers" if trailers else ""}-{form}')


def _app(status, lengths, body, trailers, form, refusals):
    headers = [(b'x-head', b'present')]
    headers += [(b'content-length', v) for v in lengths or ()]
    calls = []

    async def app(conn, receive, send):
        calls.append(1)
        if len(calls) > 1:
            await send(NativeResponse(header=[], body=b'next'))
            return
        try:
            if form == 'native':
                await send(NativeResponse(status=status, header=headers,
                                          body=body[0] if body else None,
                                          trailers=TRAILERS if trailers else None,
                                          expects_trailers=trailers))
                return
            await send({'type': 'http.response.start', 'status': status,
                        'headers': headers, 'trailers': trailers})
            for i, part in enumerate(body or ()):
                await send({'type': 'http.response.body', 'body': part,
                            'more_body': i < len(body) - 1})
            if form == 'split-trailers':
                await send({'type': 'http.response.trailers', 'headers': TRAILERS[:0],
                            'more_trailers': True})
            if trailers:
                await send({'type': 'http.response.trailers', 'headers': TRAILERS})
        except ValueError:
            refusals.append(1)
            await send({'type': 'http.response.start', 'status': 500, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})
    return app


async def _http1(server, method):
    wire = await server.wire()
    await wire.send(method.encode() + b' / HTTP/1.1\r\nHost: x\r\n\r\n')
    try:
        first = await wire.response(head=method == 'HEAD')
    except (asyncio.IncompleteReadError, ConnectionError, ValueError):
        return None, None
    await wire.send(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n')
    try:
        second = await wire.response()
    except (asyncio.IncompleteReadError, ConnectionError):
        second = None
    return ((first.status, first.headers, first.body or b'', first.trailers or []),
            second and second.body)


async def _http2(server, method):
    wire = await h2_connect(server)
    encoder, decoder = Encoder(), Decoder()
    for sid, m in ((1, method), (3, 'GET')):
        block = encoder.encode([(b':method', m.encode()), (b':path', b'/'),
                                (b':scheme', b'http'), (b':authority', b'x')])
        await wire.send(h2_section(block, sid=sid))
    streams = {1: [None, [], bytearray(), [], False], 3: [None, [], bytearray(), [], False]}
    while not all(s[4] for s in streams.values()):
        kind, flags, sid, payload = await asyncio.wait_for(read_h2_frame(wire.reader), 5)
        if kind == 7:
            break
        state = streams.get(sid)
        if kind == 1:
            fields = decoder.decode(payload, raw=True)
            if state is None:
                continue
            if state[0] is None:
                state[0] = int(dict(fields)[b':status'])
                state[1] = [f for f in fields if not f[0].startswith(b':')]
            else:
                state[3] = fields
        elif kind == 0 and state is not None:
            state[2] += payload
        elif kind == 3 and state is not None:
            state[0] = 'reset'
            state[4] = True
        if state is not None and kind in (0, 1) and flags & 1:
            state[4] = True
    first, second = streams[1], streams[3]
    if first[0] in (None, 'reset'):
        return None, bytes(second[2]) if second[4] else None
    return (first[0], first[1], bytes(first[2]), first[3]), bytes(second[2])


@pytest.mark.parametrize('protocol', ['http1', 'http2'])
@pytest.mark.parametrize('lane', ['0', '1'], ids=['native', 'compat'])
@pytest.mark.parametrize('method,status,lengths,body,trailers,form', _rows())
async def test_a_generated_response_keeps_its_length_rule(
        monkeypatch, protocol, lane, method, status, lengths, body, trailers, form):
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', lane)
    outcome, length, content, tail = _expected(method, status, lengths, body, trailers,
                                               protocol)
    refusals = []
    async with served(_app(status, lengths, body, trailers, form, refusals)) as server:
        first, following = await (_http1 if protocol == 'http1' else _http2)(server, method)

    if outcome == 'broken':
        assert refusals == [1]
        if first is not None and first[0] == 500:
            # HTTP/2 holds a first chunk for coalescing when trailers follow,
            # so the break may land before HEADERS: refused, not broken.
            assert protocol == 'http2' and trailers and following == b'next'
        elif protocol == 'http1':
            assert following is None, 'a broken response left the connection reusable'
        else:
            assert first is None, f'stream 1 was not reset: {first}'
            assert following == b'next', 'the reset stream took its siblings with it'
        return
    if outcome == 'refused':
        assert refusals == [1]
        assert first is not None and first[0] == 500, first
        assert following == b'next'
        return
    assert not refusals
    got_status, fields, got_content, got_trailers = first
    assert got_status == status
    assert (b'x-head', b'present') in fields
    stated = [v for k, v in fields if k == b'content-length']
    if length is None:
        assert stated in ([], [b'0']) if protocol == 'http1' and status == 205 else stated == []
    elif length is not ...:
        assert stated == [str(length).encode()]
    assert not any(k == b'transfer-encoding' for k, _ in fields) or protocol == 'http1'
    assert got_content == content
    assert got_trailers == tail
    assert following == b'next'


@pytest.mark.parametrize('protocol', ['http1', 'http2'])
async def test_an_interim_response_states_no_length(protocol):
    async def app(conn, receive, send):
        await send({'type': 'http.response.start', 'status': 103,
                    'headers': [(b'link', b'</a>'), (b'content-length', b'5')]})
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'final'})

    async with served(app) as server:
        if protocol == 'http1':
            wire = await server.wire()
            await wire.send(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n')
            interim = await wire.recv_until()
            assert interim.startswith(b'HTTP/1.1 103')
            assert b'content-length' not in interim.lower()
            assert (await wire.response()).body == b'final'
        else:
            wire = await h2_connect(server)
            encoder, decoder = Encoder(), Decoder()
            await wire.send(h2_section(encoder.encode(
                [(b':method', b'GET'), (b':path', b'/'), (b':scheme', b'http'),
                 (b':authority', b'x')])))
            while True:
                kind, _, sid, payload = await asyncio.wait_for(read_h2_frame(wire.reader), 5)
                if kind == 1 and sid == 1:
                    fields = dict(decoder.decode(payload, raw=True))
                    if fields[b':status'] == b'103':
                        assert b'content-length' not in fields
                        break

"""RFC 9112 §6-§7 request bodies, as the application receives them."""
import asyncio
import logging

import pytest

from blackbull import BlackBull
from blackbull.env import reset_settings_cache

from tests.harness import served

pytestmark = pytest.mark.asyncio


def _post(*fields, body=b'', path=b'/echo'):
    return (b'\r\n'.join([b'POST ' + path + b' HTTP/1.1', b'Host: x', *fields])
            + b'\r\n\r\n' + body)


def _chunked(wire: bytes) -> bytes:
    return _post(b'Transfer-Encoding: chunked', body=wire)


def _echo_app(events: list):
    """``/echo`` answers with the body it received; *events* keeps every receive."""
    app = BlackBull()

    @app.route(path='/echo', methods=['POST'])
    async def echo(conn, receive, send):
        body = b''
        while True:
            event = await receive()
            events.append(event)
            body += event.get('body', b'')
            if event['type'] != 'http.request' or not event.get('more_body'):
                break
        await send(body, 200)

    return app


async def _send(*raws: bytes, dribble=False):
    events: list = []
    async with served(_echo_app(events)) as s:
        w = await s.wire()
        out = []
        for raw in raws:
            if dribble:
                for i in range(len(raw)):
                    await w.send(raw[i:i + 1])
            else:
                await w.send(raw)
            out.append(await w.response())
        out.append(await w.closed() if out[-1].status >= 400 else None)
    return out, events


VALID = {
    'two-chunks': (b'5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n', b'hello world'),
    'upper-and-lower-hex': (b'A\r\n0123456789\r\na\r\nabcdefghij\r\n0\r\n\r\n',
                            b'0123456789abcdefghij'),
    'size-16': (b'10\r\n' + b'x' * 16 + b'\r\n0\r\n\r\n', b'x' * 16),
    'ext-ignored': (b'5;foo=bar\r\nhello\r\n0\r\n\r\n', b'hello'),
    'bws-before-ext': (b'5 ;foo=bar\r\nhello\r\n0\r\n\r\n', b'hello'),
    'ext-without-value': (b'5;foo\r\nhello\r\n0\r\n\r\n', b'hello'),
    'quoted-ext': (b'5;a="quoted val"\r\nhello\r\n0\r\n\r\n', b'hello'),
    'quoted-ext-comma': (b'5;a="x,y"\r\nhello\r\n0\r\n\r\n', b'hello'),
    'benign-trailer': (b'5\r\nhello\r\n0\r\nx-checksum: abc123\r\n\r\n', b'hello'),
    'longest-chunk-line': (b'5;e=' + b'a' * (8192 - 6) + b'\r\nhello\r\n0\r\n\r\n', b'hello'),
}


@pytest.mark.parametrize('wire,body', VALID.values(), ids=VALID.keys())
async def test_a_valid_chunked_body_is_delivered_whole(wire, body):
    (resp, _), _ = await _send(_chunked(wire))
    assert (resp.status, resp.body) == (200, body)


MALFORMED = {
    **{name: line + b'hello\r\n0\r\n\r\n' for name, line in {
        'negative': b'-1\r\n', 'plus': b'+0\r\n', 'hex-prefix': b'0x5\r\n',
        'underscore': b'1_0\r\n', 'leading-space': b' 5\r\n',
        'trailing-space': b'5 \r\n', 'bare-semicolon': b'5;\r\n',
        'ext-name-not-token': b'5;a@b=c\r\n', 'ext-control': b'5;a=\x01\r\n',
        'ext-unterminated-quote': b'5;a="unterminated\r\n',
        'quoted-ext-control': b'5;a="x\x01"\r\n', 'quoted-ext-del': b'5;a="x\x7f"\r\n',
        'empty-size': b'\r\n', 'not-hex': b'XYZ\r\n',
    }.items()},
    'spill': b'5\r\nhelloEXTRA\r\n0\r\n\r\n',
    'bare-cr-after-data': b'5\r\nhello\r0\r\n\r\n',
    'bare-lf-after-data': b'5\r\nhello\n0\r\n\r\n',
    'bare-lf-size-line': b'5\nhello\r\n0\r\n\r\n',
    'missing-crlf-between-chunks': b'5\r\nhello0\r\n\r\n',
    'bare-lf-trailer-terminator': b'5\r\nhello\r\n0\r\n\n',
    'bare-lf-trailer-line': b'5\r\nhello\r\n0\r\nfoo: bar\n\r\n',
    'chunk-line-over-bound': b'5;e=' + b'a' * (8192 - 5) + b'\r\nhello\r\n0\r\n\r\n',
    'trailer-line-over-bound': b'5\r\nhello\r\n0\r\nx-pad: ' + b'a' * 65536 + b'\r\n\r\n',
    **{f'prohibited-trailer-{f.split(b":")[0].decode()}': b'5\r\nhello\r\n0\r\n' + f + b'\r\n\r\n'
       for f in (b'Transfer-Encoding: chunked', b'Content-Length: 999', b'Host: evil.example',
                 b'Authorization: Bearer evil', b'Content-Type: text/evil')},
}


@pytest.mark.parametrize('wire', MALFORMED.values(), ids=MALFORMED.keys())
async def test_malformed_chunked_framing_is_refused_and_closes(wire):
    (resp, closed), _ = await _send(_chunked(wire))
    assert (resp.status, closed) == (400, True)


async def test_an_over_long_chunk_line_is_a_counted_cap(caplog):
    caplog.set_level(logging.WARNING, logger='blackbull.caps')
    await _send(_chunked(MALFORMED['chunk-line-over-bound']))
    assert [r.cap for r in caplog.records if r.name == 'blackbull.caps'] == \
        ['h1_chunk_line_length']


async def test_bodies_arriving_one_octet_at_a_time_are_reassembled():
    a, b = b'a' * 50, b'b' * 70
    (resp, _), _ = await _send(
        _chunked(b'32\r\n' + a + b'\r\n46\r\n' + b + b'\r\n0\r\n\r\n'), dribble=True)
    assert resp.body == a + b


async def test_each_chunk_is_its_own_event():
    _, events = await _send(_chunked(b'5\r\nhello\r\n5\r\nworld\r\n0\r\n\r\n'))
    assert events[0] == {'type': 'http.request', 'body': b'hello', 'more_body': True}
    assert [e['body'] for e in events if e['body']] == [b'hello', b'world']
    assert events[-1]['more_body'] is False


@pytest.mark.parametrize('fields,body', [
    ((), b''),
    ((b'Content-Length: 0',), b''),
    ((b'Content-Length: 5',), b'hello'),
])
async def test_a_small_body_is_one_event(fields, body):
    _, events = await _send(_post(*fields, body=body))
    assert events == [{'type': 'http.request', 'body': body, 'more_body': False}]


async def test_a_content_length_body_is_sliced_at_the_chunk_cap(monkeypatch):
    monkeypatch.setenv('BB_BODY_CHUNK_MAX', '3')
    reset_settings_cache()
    try:
        (resp, _), events = await _send(_post(b'Content-Length: 7', body=b'abcdefg'))
    finally:
        reset_settings_cache()
    assert resp.body == b'abcdefg'
    assert all(0 < len(e['body']) <= 3 for e in events)
    assert [e['more_body'] for e in events][-1] is False


async def test_each_keep_alive_request_reads_its_own_framing():
    (first, second, third, _), _ = await _send(
        _post(b'Content-Length: 5', body=b'first'),
        _chunked(b'4\r\nwxyz\r\n0\r\n\r\n'),
        _post(b'Content-Length: 7', body=b'second!'))
    assert (first.body, second.body, third.body) == (b'first', b'wxyz', b'second!')


@pytest.mark.parametrize('unread,reused', [(300, True), (70_000, False)])
async def test_an_unread_body_is_drained_within_a_bound(unread, reused):
    app = BlackBull()

    @app.route(path='/ignore', methods=['POST'])
    async def ignore():
        return b'ignored'

    async with served(app) as s:
        w = await s.wire()
        await w.send(_post(b'Content-Length: %d' % unread, body=b'z' * unread, path=b'/ignore'))
        assert (await w.response()).status == 200
        if reused:
            await w.send(_post(path=b'/ignore'))
            assert (await w.response()).status == 200
        else:
            assert await w.closed()


@pytest.mark.parametrize('expect', [b'100-continue', b'100-Continue'])
async def test_expect_continue_is_answered_before_the_body_is_read(expect):
    events: list = []
    async with served(_echo_app(events)) as s:
        w = await s.wire()
        await w.send(_post(b'Content-Length: 5', b'Expect: ' + expect))
        assert (await w.recv_until()).startswith(b'HTTP/1.1 100 ')
        await w.send(b'hello')
        resp = await w.response()
    assert (resp.status, resp.body) == (200, b'hello')


async def test_no_interim_without_expect():
    async with served(_echo_app([])) as s:
        w = await s.wire()
        await w.send(_post(b'Content-Length: 4', body=b'data'))
        assert (await w.recv_until()).startswith(b'HTTP/1.1 200 ')


@pytest.mark.parametrize('sent', [b'', b'hel'])
async def test_a_peer_leaving_mid_body_is_a_disconnect(sent):
    done = asyncio.Event()
    events: list = []

    async def app(conn, receive, send):
        try:
            while True:
                event = await receive()
                events.append(event)
                if not event.get('more_body'):
                    return
        finally:
            done.set()

    async with served(app) as s:
        w = await s.wire()
        await w.send(_post(b'Content-Length: 10', body=sent))
        w.half_close()
        await asyncio.wait_for(done.wait(), 5)
    assert b''.join(e.get('body', b'') for e in events) == sent
    assert events[-1] == {'type': 'http.disconnect'}

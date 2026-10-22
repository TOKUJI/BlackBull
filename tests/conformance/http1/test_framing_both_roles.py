"""Tables of HTTP/1.1 message rules, each row run as a request at the server
and as a response at the client.

Each row is a field section and the octets after it.  The two columns are the
two roles' answers: the server's status (or the echoed body), and the body the
client read (or the exception it raised).  Rows that differ by role differ
because RFC 9112 makes requests and responses differ; the role policy in
``blackbull.protocol.http1`` names the section.
"""
import asyncio
from typing import NamedTuple

import pytest

from blackbull import BlackBull
from blackbull.client.http1 import Fate, HTTP1ResponseRecipient
from tests.harness import fed, served

pytestmark = pytest.mark.asyncio


class Case(NamedTuple):
    fields: tuple[bytes, ...]
    body: bytes
    #: Echoed body, or the status the server answers with.
    request: bytes | int
    #: The body the client read, or the name of the exception it raised.
    response: bytes | str


CHUNKED = b'Transfer-Encoding: chunked'
LAST = b'0\r\n\r\n'

CASES: dict[str, Case] = {
    # --- Content-Length (RFC 9110 §8.6, RFC 9112 §6.2) ---------------------
    'cl': Case((b'Content-Length: 5',), b'hello', b'hello', b'hello'),
    'cl-repeated-agree': Case((b'Content-Length: 5', b'Content-Length: 5'),
                              b'hello', b'hello', b'hello'),
    'cl-conflict': Case((b'Content-Length: 5', b'Content-Length: 6'),
                        b'hello!', 400, 'ProtocolError'),
    'cl-list': Case((b'Content-Length: 5, 5',), b'hello', 400, 'ProtocolError'),
    'cl-leading-zero': Case((b'Content-Length: 05',), b'hello', 400, 'ProtocolError'),
    'cl-sign': Case((b'Content-Length: +5',), b'hello', 400, 'ProtocolError'),
    'cl-trailing-space': Case((b'Content-Length: 5 ',), b'hello', 400, 'ProtocolError'),
    'cl-and-te': Case((b'Content-Length: 5', CHUNKED), b'5\r\nhello\r\n' + LAST,
                      400, 'ProtocolError'),
    'te-and-cl': Case((CHUNKED, b'Content-Length: 5'), b'5\r\nhello\r\n' + LAST,
                      400, 'ProtocolError'),
    # --- Transfer-Encoding (RFC 9112 §6.1, §6.3) --------------------------
    'te-chunked': Case((CHUNKED,), b'5\r\nhello\r\n' + LAST, b'hello', b'hello'),
    'te-chunked-empty-body': Case((CHUNKED,), LAST, b'', b''),
    'te-chunked-uppercase': Case((b'Transfer-Encoding: Chunked',),
                                 b'5\r\nhello\r\n' + LAST, b'hello', b'hello'),
    'te-split-fields': Case((b'Transfer-Encoding: gzip', CHUNKED),
                            b'5\r\nhello\r\n' + LAST, 501, b'hello'),
    'te-gzip-chunked': Case((b'Transfer-Encoding: gzip, chunked',),
                            b'5\r\nhello\r\n' + LAST, 501, b'hello'),
    'te-gzip': Case((b'Transfer-Encoding: gzip',), b'hello', 501, b'hello'),
    'te-unknown-coding': Case((b'Transfer-Encoding: xchunked',), b'hello', 501, b'hello'),
    'te-chunked-gzip': Case((b'Transfer-Encoding: chunked, gzip',), b'hello',
                            400, b'hello'),
    'te-chunked-twice': Case((b'Transfer-Encoding: chunked, chunked',),
                             b'5\r\nhello\r\n' + LAST, 400, 'ProtocolError'),
    'te-chunked-parameter': Case((b'Transfer-Encoding: chunked;q=1',),
                                 b'5\r\nhello\r\n' + LAST, 501, b'hello'),
    'te-unparsable': Case((b'Transfer-Encoding: chunked;',),
                          b'5\r\nhello\r\n' + LAST, 400, 'ProtocolError'),
    'te-trailing-empty-member': Case((b'Transfer-Encoding: chunked, ',),
                                     b'5\r\nhello\r\n' + LAST, 400, b'hello'),
    'te-empty-members-across-fields': Case(
        (b'Transfer-Encoding: gzip,', b'Transfer-Encoding: ,chunked'),
        b'5\r\nhello\r\n' + LAST, 501, b'hello'),
    'te-quoted-parameter': Case((b'Transfer-Encoding: gzip;p="a,b", chunked',),
                                b'5\r\nhello\r\n' + LAST, 501, b'hello'),
    'te-bad-parameter': Case((b'Transfer-Encoding: gzip;=value',), b'hello',
                             400, 'ProtocolError'),
    'te-too-many-empty-members': Case((b'Transfer-Encoding: ' + b',' * 17,), b'',
                                      400, 'ProtocolError'),
    'te-empty-and-cl': Case((b'Transfer-Encoding:', b'Content-Length: 5'), b'hello',
                            400, 'ProtocolError'),
    'te-gzip-and-cl': Case((b'Transfer-Encoding: gzip', b'Content-Length: 5'), b'hello',
                           400, 'ProtocolError'),
    'no-framing': Case((), b'hello', b'', b'hello'),
    # --- chunk-size line (RFC 9112 §7.1) ----------------------------------
    'size-uppercase-hex': Case((CHUNKED,), b'A\r\n0123456789\r\n' + LAST,
                               b'0123456789', b'0123456789'),
    'size-0x': Case((CHUNKED,), b'0x5\r\nhello\r\n' + LAST, 400, 'ProtocolError'),
    'size-non-hex': Case((CHUNKED,), b'XYZ\r\nhello\r\n' + LAST, 400, 'ProtocolError'),
    'size-leading-zeros': Case((CHUNKED,), b'0000000000000005\r\nhello\r\n' + LAST,
                               b'hello', b'hello'),
    'size-empty': Case((CHUNKED,), b'\r\nhello\r\n' + LAST, 400, 'ProtocolError'),
    'size-negative': Case((CHUNKED,), b'-5\r\nhello\r\n' + LAST, 400, 'ProtocolError'),
    'size-underscore': Case((CHUNKED,), b'1_0\r\n' + b'x' * 16 + b'\r\n' + LAST,
                            400, 'ProtocolError'),
    'size-truncated': Case((CHUNKED,), b'5', 'close', 'ConnectionError'),
    'size-sign': Case((CHUNKED,), b'+5\r\nhello\r\n' + LAST, 400, 'ProtocolError'),
    'size-trailing-space': Case((CHUNKED,), b'5 \r\nhello\r\n' + LAST,
                                400, 'ProtocolError'),
    'size-bare-lf': Case((CHUNKED,), b'5\nhello\r\n' + LAST, 400, 'ProtocolError'),
    'size-bare-cr': Case((CHUNKED,), b'5\r\rhello\r\n' + LAST, 400, 'ProtocolError'),
    # --- chunk-ext (RFC 9112 §7.1.1) --------------------------------------
    'ext': Case((CHUNKED,), b'5;name=value\r\nhello\r\n' + LAST, b'hello', b'hello'),
    'ext-bws': Case((CHUNKED,), b'5 ; name = "v"\r\nhello\r\n' + LAST,
                    b'hello', b'hello'),
    'ext-tab-bws': Case((CHUNKED,), b'5\t;name=value\r\nhello\r\n' + LAST,
                        b'hello', b'hello'),
    'ext-on-last-chunk': Case((CHUNKED,), b'5\r\nhello\r\n0;name\r\n\r\n',
                              b'hello', b'hello'),
    'ext-empty-name': Case((CHUNKED,), b'5;\r\nhello\r\n' + LAST, 400, 'ProtocolError'),
    'ext-bad-name': Case((CHUNKED,), b'5;a b\r\nhello\r\n' + LAST, 400, 'ProtocolError'),
    'ext-unterminated-quote': Case((CHUNKED,), b'5;n="v\r\nhello\r\n' + LAST,
                                   400, 'ProtocolError'),
    'ext-over-line-limit': Case((CHUNKED,), b'5;n=' + b'v' * 9000 + b'\r\nhello\r\n' + LAST,
                                400, 'ResponseTooLarge'),
    # --- chunk-data (RFC 9112 §7.1) ---------------------------------------
    'data-spill': Case((CHUNKED,), b'5\r\nhelloXX\r\n' + LAST, 400, 'ProtocolError'),
    'data-truncated': Case((CHUNKED,), b'5\r\nhel', 'close', 'ConnectionError'),
    # --- trailer section (RFC 9112 §7.1.2, RFC 9110 §6.5) -----------------
    'trailer': Case((CHUNKED,), b'5\r\nhello\r\n0\r\nX-Sum: 1\r\n\r\n', b'hello', b'hello'),
    'trailer-prohibited-field': Case(
        (CHUNKED,), b'5\r\nhello\r\n0\r\nContent-Length: 5\r\n\r\n', 400, 'ProtocolError'),
    'trailer-bad-line': Case((CHUNKED,), b'5\r\nhello\r\n0\r\nno colon\r\n\r\n',
                             400, 'ProtocolError'),
    'trailer-bare-lf': Case((CHUNKED,), b'5\r\nhello\r\n0\r\nX: 1\n\r\n',
                            400, 'ProtocolError'),
    'trailer-space-before-colon': Case((CHUNKED,), b'5\r\nhello\r\n0\r\nX : 1\r\n\r\n',
                                       400, 'ProtocolError'),
    'trailer-obs-fold': Case((CHUNKED,), b'5\r\nhello\r\n0\r\nX: 1\r\n folded\r\n\r\n',
                             400, 'ProtocolError'),
    'trailer-control-octet': Case((CHUNKED,), b'5\r\nhello\r\n0\r\nX: a\x00b\r\n\r\n',
                                  400, 'ProtocolError'),
    # --- field lines (RFC 9112 §5) ----------------------------------------
    'field-space-before-colon': Case((b'Content-Length : 5',), b'hello',
                                     400, 'ProtocolError'),
    'field-te-space-before-colon': Case((b'Transfer-Encoding : chunked',), LAST,
                                        400, 'ProtocolError'),
    'field-bare-lf': Case((b'X-A: 1\nX-B: 2',), b'', 400, 'ProtocolError'),
    'field-obs-fold': Case((b'X-A: 1', b' folded'), b'', 400, 'ProtocolError'),
    'field-bad-name': Case((b'Bad Name: v',), b'', 400, 'ProtocolError'),
    'field-control-octet': Case((b'X-A: a\x01b',), b'', 400, 'ProtocolError'),
    'field-underscore-framing-name': Case((b'Content_Length: 5',), b'hello',
                                          400, 'ProtocolError'),
    'field-two-content-types': Case((b'Content-Type: a/b', b'Content-Type: c/d'), b'',
                                    400, 'ProtocolError'),
}


def _echo_app() -> BlackBull:
    app = BlackBull()

    @app.route(path='/', methods=['POST'])
    async def echo(conn, receive, send):
        body = b''
        while True:
            event = await receive()
            if event['type'] != 'http.request':
                return
            body += event.get('body', b'')
            if not event.get('more_body'):
                break
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-length', b'%d' % len(body))]})
        await send({'type': 'http.response.body', 'body': body})

    return app


async def as_request(case: Case) -> bytes | int | str:
    """The server's answer: the echoed body on 200, else the status, or
    'close' when it hangs up without one."""
    head = b'\r\n'.join([b'POST / HTTP/1.1', b'Host: x', *case.fields, b'', b''])
    async with served(_echo_app()) as server:
        wire = await server.wire()
        await wire.send(head + case.body)
        wire.half_close()
        try:
            response = await asyncio.wait_for(wire.response(), 5)
        except (asyncio.IncompleteReadError, ConnectionError):
            return 'close'
        return response.body if response.status == 200 else response.status


async def as_response(case: Case) -> bytes | str:
    """The client's answer: the body it read, or the exception it raised."""
    head = b'\r\n'.join([b'HTTP/1.1 200 OK', *case.fields, b'', b''])
    try:
        response = await HTTP1ResponseRecipient('GET').receive(
            fed(head + case.body).reader)
    except Exception as exc:
        return type(exc).__name__
    return response.body


@pytest.mark.parametrize('case', CASES.values(), ids=CASES.keys())
async def test_the_server_answers_the_request(case):
    assert await as_request(case) == case.request


@pytest.mark.parametrize('case', CASES.values(), ids=CASES.keys())
async def test_the_client_answers_the_response(case):
    assert await as_response(case) == case.response


#: RFC 9112 §9.3: ``(version, Connection value or None)`` -> the connection
#: persists ('open'), closes ('closed'), or the message is refused.
PERSISTENCE: dict[str, tuple[bytes, bytes | None, str]] = {
    '1.1-default': (b'HTTP/1.1', None, 'open'),
    '1.1-close': (b'HTTP/1.1', b'close', 'closed'),
    '1.1-close-any-case': (b'HTTP/1.1', b'Close', 'closed'),
    '1.1-close-in-a-list': (b'HTTP/1.1', b'keep-alive, close', 'closed'),
    '1.0-default': (b'HTTP/1.0', None, 'closed'),
    '1.0-keep-alive': (b'HTTP/1.0', b'keep-alive', 'open'),
    '1.0-close-beats-keep-alive': (b'HTTP/1.0', b'keep-alive, close', 'closed'),
    'option-not-a-token': (b'HTTP/1.1', b'close x', 'refused'),
}


async def persists_as_request(version: bytes, connection: bytes | None) -> str:
    fields = [b'Connection: ' + connection] if connection is not None else []
    head = b'\r\n'.join([b'POST / ' + version, b'Host: x', b'Content-Length: 0',
                          *fields, b'', b''])
    async with served(_echo_app()) as server:
        wire = await server.wire()
        await wire.send(head)
        if (await wire.response()).status != 200:
            return 'refused'
        await wire.send(b'POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n')
        try:
            await asyncio.wait_for(wire.response(), 2)
        except (asyncio.IncompleteReadError, ConnectionError):
            return 'closed'
        return 'open'


async def persists_as_response(version: bytes, connection: bytes | None) -> str:
    fields = [b'Connection: ' + connection] if connection is not None else []
    head = b'\r\n'.join([version + b' 200 OK', b'Content-Length: 0', *fields, b'', b''])
    recipient = HTTP1ResponseRecipient('GET')
    try:
        await recipient.receive(fed(head).reader)
    except Exception:
        return 'refused'
    return 'open' if recipient.fate is Fate.REUSE else 'closed'


@pytest.mark.parametrize('version,connection,expected', PERSISTENCE.values(),
                         ids=PERSISTENCE.keys())
async def test_the_server_keeps_the_connection_by_the_rule(version, connection, expected):
    assert await persists_as_request(version, connection) == expected


@pytest.mark.parametrize('version,connection,expected', PERSISTENCE.values(),
                         ids=PERSISTENCE.keys())
async def test_the_client_keeps_the_connection_by_the_rule(version, connection, expected):
    assert await persists_as_response(version, connection) == expected

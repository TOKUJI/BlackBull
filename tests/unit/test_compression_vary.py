"""Unit tests for the Compression middleware's ``Vary: Accept-Encoding``
emission (bug 1.21a).

A compressed response's body depends on the request ``Accept-Encoding``;
without ``Vary: Accept-Encoding`` a shared cache may replay the encoded
body to a client that sent ``identity`` / no ``Accept-Encoding``
(RFC 9110 §12.5.5).
"""
from __future__ import annotations

import pytest

from blackbull.middleware.compression import Compression, _merge_vary


async def _noop_receive():
    return {'type': 'http.disconnect'}


def _scope(accept: bytes = b'gzip') -> dict:
    return {
        'type': 'http',
        'method': 'GET',
        'path': '/',
        'headers': [(b'accept-encoding', accept)],
    }


async def _run(mw, body: bytes, resp_headers, accept: bytes = b'gzip') -> dict:
    events: list[dict] = []

    async def send(event: dict) -> None:
        events.append(event)

    async def call_next(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': list(resp_headers)})
        await send({'type': 'http.response.body', 'body': body, 'more_body': False})

    await mw(_scope(accept), _noop_receive, send, call_next)
    start = next(e for e in events if e['type'] == 'http.response.start')
    return {'status': start['status'], 'headers': start['headers']}


# A body over the default _MIN_SIZE (100) so compression actually runs.
_BODY = b'the quick brown fox jumps over the lazy dog ' * 20


def _vary_values(headers) -> list[bytes]:
    return [v for k, v in headers if k.lower() == b'vary']


@pytest.mark.asyncio
async def test_compressed_response_emits_vary():
    mw = Compression()
    res = await _run(mw, _BODY, [(b'content-type', b'text/plain')])
    assert res['headers']  # sanity
    assert dict(res['headers']).get(b'content-encoding') == b'gzip'
    varies = _vary_values(res['headers'])
    assert len(varies) == 1
    assert b'accept-encoding' in varies[0].lower()


@pytest.mark.asyncio
async def test_vary_folds_into_existing():
    """An upstream Vary must be extended, not duplicated with a 2nd header."""
    mw = Compression()
    res = await _run(mw, _BODY, [
        (b'content-type', b'text/plain'),
        (b'vary', b'Accept-Language'),
    ])
    varies = _vary_values(res['headers'])
    assert len(varies) == 1, f'expected one folded Vary; got {varies}'
    tokens = {t.strip().lower() for t in varies[0].split(b',')}
    assert tokens == {b'accept-language', b'accept-encoding'}


@pytest.mark.asyncio
async def test_vary_not_duplicated_when_already_present():
    mw = Compression()
    res = await _run(mw, _BODY, [
        (b'content-type', b'text/plain'),
        (b'vary', b'accept-encoding'),
    ])
    varies = _vary_values(res['headers'])
    assert len(varies) == 1
    tokens = [t.strip().lower() for t in varies[0].split(b',')]
    assert tokens.count(b'accept-encoding') == 1


class TestMergeVaryHelper:
    def test_appends_when_absent(self):
        hdrs = [(b'content-type', b'text/plain')]
        _merge_vary(hdrs)
        assert (b'vary', b'Accept-Encoding') in hdrs

    def test_folds_into_existing(self):
        hdrs = [(b'vary', b'Accept-Language')]
        _merge_vary(hdrs)
        assert hdrs == [(b'vary', b'Accept-Language, Accept-Encoding')]

    def test_no_dup(self):
        hdrs = [(b'vary', b'Accept-Encoding')]
        _merge_vary(hdrs)
        assert hdrs == [(b'vary', b'Accept-Encoding')]

    def test_star_untouched(self):
        hdrs = [(b'vary', b'*')]
        _merge_vary(hdrs)
        assert hdrs == [(b'vary', b'*')]

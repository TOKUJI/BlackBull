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


def _has_vary_accept_encoding(headers) -> bool:
    return any(k.lower() == b'vary' and b'accept-encoding' in v.lower()
               for k, v in headers)


class TestVaryOnCompressibleButUncompressedPaths:
    """1.21f — a *compressible* response must carry ``Vary: Accept-Encoding``
    even on the exit paths that do not actually compress, so a downstream
    shared cache never stores an un-varied entry it then replays to a client
    that does accept an encoding."""

    @pytest.mark.asyncio
    async def test_no_matching_codec_still_varies(self):
        # Client accepts nothing we offer → we forward verbatim (Branch 1).
        mw = Compression()
        res = await _run(mw, _BODY, [(b'content-type', b'text/plain')], accept=b'')
        assert dict(res['headers']).get(b'content-encoding') is None
        assert _has_vary_accept_encoding(res['headers'])

    @pytest.mark.asyncio
    async def test_small_body_still_varies(self):
        # Body under _MIN_SIZE → not compressed, but still compressible (2c).
        mw = Compression()
        res = await _run(mw, b'tiny', [(b'content-type', b'text/plain')], accept=b'gzip')
        assert dict(res['headers']).get(b'content-encoding') is None
        assert _has_vary_accept_encoding(res['headers'])

    @pytest.mark.asyncio
    async def test_executor_at_cap_still_varies(self):
        # Executor-offload cap hit → served uncompressed but must vary (2d).
        mw = Compression(executor_max_inflight=1, executor_threshold=1)
        mw._executor_inflight = 1
        res = await _run(mw, _BODY, [(b'content-type', b'text/plain')], accept=b'gzip')
        assert dict(res['headers']).get(b'content-encoding') is None
        assert _has_vary_accept_encoding(res['headers'])

    @pytest.mark.asyncio
    async def test_uncompressible_content_type_does_not_vary(self):
        # An already-compressed Content-Type must NOT gain a spurious Vary (2a).
        mw = Compression()
        res = await _run(mw, _BODY, [(b'content-type', b'image/png')], accept=b'gzip')
        assert not _has_vary_accept_encoding(res['headers'])

    @pytest.mark.asyncio
    async def test_precompressed_response_does_not_vary(self):
        # A pre-existing Content-Encoding means we don't touch the body — and
        # must not add our own Vary (2b).
        mw = Compression()
        res = await _run(mw, _BODY, [
            (b'content-type', b'text/plain'),
            (b'content-encoding', b'br'),
        ], accept=b'gzip')
        assert not _has_vary_accept_encoding(res['headers'])

    @pytest.mark.asyncio
    async def test_no_codec_path_leaves_uncompressible_alone(self):
        # Branch 1 + uncompressible type → no compression, no Vary.
        mw = Compression()
        res = await _run(mw, _BODY, [(b'content-type', b'video/mp4')], accept=b'')
        assert not _has_vary_accept_encoding(res['headers'])


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

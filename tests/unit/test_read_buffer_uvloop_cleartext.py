"""Regression: the uvloop cleartext read path must not crash on the read-buffer
window release.

uvloop 0.22.1's buffered read path (`__uv_stream_buffered_on_read` in
handles/stream.pyx) calls ``buffer_updated()`` while its Py_buffer export on
the ``get_buffer`` window is still acquired — the export is released in
uvloop's own ``finally``, immediately after our callback returns.
``ReadBuffer._drop_view`` used to call ``view.release()`` unconditionally,
which raised ``BufferError: memoryview has 1 exported buffer`` on every
cleartext connection.  TLS goes through SSLProtocol and never hit this — so
`blackbull-cleartext` (plain HTTP under uvloop) was broken at HEAD c618255
while TLS worked.

These run the production server over loopback under uvloop — the exact path
that crashed; the first round trip failed on the pre-fix code.
"""
import pytest

from blackbull.server.read_buffer import ReadBuffer

from tests.harness import served

uvloop = pytest.importorskip("uvloop")


async def _ok(conn, receive, send):
    while (await receive()).get('more_body'):
        pass
    await send({'type': 'http.response.start', 'status': 200, 'headers': []})
    await send({'type': 'http.response.body', 'body': b'ok'})


def _statuses(requests: list[bytes], *, connections: int = 1) -> list[int]:
    async def run():
        out = []
        async with served(_ok) as s:
            for i in range(connections):
                w = await s.wire()
                for raw in requests:
                    await w.send(raw)
                    out.append((await w.response()).status)
        return out
    return uvloop.run(run())


GET = b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n'


def test_cleartext_connections_under_uvloop_round_trip():
    assert _statuses([GET], connections=64) == [200] * 64


def test_keep_alive_past_the_compact_threshold_under_uvloop():
    """~1.2 KiB heads: the consumed prefix crosses 4 KiB and ``compact()`` runs live."""
    padded = GET[:-2] + b'X-Pad: ' + b'a' * 1200 + b'\r\n\r\n'
    assert _statuses([padded] * 8) == [200] * 8


def test_a_body_past_the_initial_buffer_under_uvloop():
    """32 KiB forces ``_make_room()`` past the 8 KiB starting allocation."""
    post = b'POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: 32768\r\n\r\n' + b'x' * 32768
    assert _statuses([post]) == [200]


def test_small_keep_alive_requests_do_not_grow_the_buffer(monkeypatch):
    """uvloop's fixed 64 KiB sizehint must not drive growth: arrivals do."""
    calls: list[int] = []
    grow = ReadBuffer._make_room
    monkeypatch.setattr(ReadBuffer, '_make_room', lambda self, want: calls.append(want) or grow(self, want))
    assert _statuses([GET] * 200) == [200] * 200
    assert calls == []

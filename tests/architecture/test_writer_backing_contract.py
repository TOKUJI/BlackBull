"""Whatever `AsyncioWriter` calls on its backing object, the server must supply.

`AsyncioWriter` was written against `asyncio.StreamWriter` and validates only
`write` + `drain` at construction.  The server now hands it a
`ConnectionProtocol` instead — an object that happens to implement most of the
same surface.  "Most" is the problem: `writelines` was missing, so the send
path's size gate served every response under 32 KiB and answered 500 for every
response over it, and the entire suite stayed green because the gate's own
tests use a double that has the method.

A duck-typed seam needs its duck checked somewhere.  Rather than list the
methods by hand — a list that goes stale exactly when it matters — this reads
`AsyncioWriter`'s source and asks what it actually touches.
"""
import ast
import inspect

import pytest

from blackbull.server.connection_protocol import ConnectionProtocol
from blackbull.server.sender import AsyncioWriter


def _attributes_used_on(cls, backing_attr: str) -> set[str]:
    """Every ``self.<backing_attr>.<name>`` read anywhere in *cls*."""
    tree = ast.parse(inspect.getsource(cls))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        inner = node.value
        # Match ``self._sw.<name>`` — an Attribute whose value is itself the
        # Attribute ``self._sw``.
        if (isinstance(inner, ast.Attribute)
                and inner.attr == backing_attr
                and isinstance(inner.value, ast.Name)
                and inner.value.id == 'self'):
            found.add(node.attr)
    return found


def test_asyncio_writer_touches_something():
    """Guard the guard: a parse that silently finds nothing would make the
    real assertion below vacuously true."""
    used = _attributes_used_on(AsyncioWriter, '_sw')
    assert 'write' in used and 'drain' in used, (
        f'AST scan found {used!r} — the extraction is broken, so the '
        f'contract test below is not testing anything')


def test_connection_protocol_supplies_the_whole_backing_surface():
    # Checked against an *instance*: ``transport`` is set in ``__init__``, and
    # a class-level ``hasattr`` would report it missing and send the reader
    # chasing a method that does not need to exist.
    proto = ConnectionProtocol()
    used = _attributes_used_on(AsyncioWriter, '_sw')
    missing = sorted(n for n in used if not hasattr(proto, n))
    assert not missing, (
        f'AsyncioWriter calls {missing} on its backing object, and '
        f'ConnectionProtocol — what the server actually hands it — does not '
        f'provide them. Each one is a response shape that raises '
        f'AttributeError at runtime while every unit test passes, because '
        f'the doubles have the method and the real object does not.')


@pytest.mark.parametrize('name', ['write', 'writelines', 'drain', 'close'])
def test_named_essentials_are_present(name):
    """The AST scan above is the general net; these are the four the send path
    cannot work without, spelled out so a failure names the casualty."""
    assert hasattr(ConnectionProtocol, name)


# ---------------------------------------------------------------------------
# The static half above proves the names exist.  This proves they work: an
# attribute can be present and still be the wrong thing (a coroutine where a
# plain call is expected, a transport that never got assigned).
#
# ``write`` and ``writelines`` are covered end-to-end by the size-gate tests in
# tests/unit/test_write_many_gate.py, which straddle the 32 KiB threshold that
# decides between them.  ``sendfile`` has no gate — it is reached only by a
# static file above the cache threshold — so it is exercised here.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sendfile_path_works_against_the_real_transport(tmp_path):
    """`AsyncioWriter.sendfile` reaches for `self._sw.transport` and hands it
    to `loop.sendfile`.  That worked when the backing object was a
    `StreamWriter`; it has to keep working now that it is the protocol."""
    import httpx  # noqa: PLC0415

    from blackbull import BlackBull  # noqa: PLC0415
    from blackbull.testing.native import NativeTestServer  # noqa: PLC0415

    # Above the 4 MiB StaticFiles cache threshold, so the response is streamed
    # from the file rather than served out of the in-memory cache.
    size = 8 * 1024 * 1024
    (tmp_path / 'big.bin').write_bytes(b'q' * size)

    app = BlackBull()
    app.static('/static', tmp_path)

    async with NativeTestServer(app) as srv:
        async with httpx.AsyncClient(base_url=srv.url, timeout=30) as c:
            r = await c.get('/static/big.bin')

    assert r.status_code == 200
    assert len(r.content) == size

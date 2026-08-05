"""`as_middleware` honours a `scope` parameter by handing over a real scope.

The word `scope` means a genuine ASGI scope dict everywhere in BlackBull —
never a `Connection`.  `_adapt_handler` *rejects* the name for simplified
handlers on that ground.  `as_middleware` takes the other branch of the same
rule: a middleware that asks for `scope` is written against ASGI, so it is
given a real scope dict and adapted at both of its own edges.

What must hold:

  * a scope-declaring middleware sees a `dict`, and `scope['headers']` works;
  * everything *below* it still sees the native `Connection` — the dict does
    not outlive the frame that asked for it;
  * that middleware's own `send` wrapper sees ASGI event dicts, because it
    will inspect `event['type']`;
  * a native middleware is not adapted at all, and pays nothing.
"""
import pytest

from blackbull.connection import Connection
from blackbull.headers import Headers
from blackbull.middleware.utils import as_middleware
from blackbull.native import NativeResponse


def _conn():
    return Connection(method='GET', path='/', raw_path=b'/',
                      headers=Headers([(b'host', b'x')]), type='http')


async def _noop_receive():
    return {'type': 'http.disconnect'}


async def _run(mw):
    """Drive *mw* over a native inner handler; return what reached the wire."""
    out: list = []
    seen_below: list = []

    async def send(event):
        out.append(event)

    async def call_next(conn, receive, inner_send):
        seen_below.append(conn)
        await inner_send(NativeResponse(
            status=200, header=[(b'content-type', b'text/plain')], body=b'ok'))

    await mw(_conn(), _noop_receive, send, call_next)
    return out, seen_below


@pytest.mark.asyncio
async def test_scope_declaring_function_gets_a_real_scope_dict():
    seen: dict = {}

    @as_middleware
    async def asgi_mw(scope, receive, send, call_next):
        seen['type'] = type(scope)
        seen['headers'] = scope['headers']
        seen['path'] = scope['path']
        await call_next(scope, receive, send)

    out, below = await _run(asgi_mw)

    assert seen['type'] is dict, (
        f'a middleware declaring `scope` got {seen["type"].__name__}')
    assert seen['path'] == '/'
    assert seen['headers'] is not None
    # The dict must not travel further than the frame that asked for it.
    assert all(isinstance(c, Connection) for c in below), (
        f'scope leaked below the middleware: '
        f'{[type(c).__name__ for c in below]}')
    assert out and isinstance(out[0], NativeResponse), (
        f'the seam above did not stay native: {type(out[0]).__name__}')


@pytest.mark.asyncio
async def test_the_documented_logging_middleware_runs():
    """`docs/guide/middleware.md`'s first example, verbatim.

    It reads `scope['method']` and `scope['path']`.  A `Connection` answers
    neither — it is not subscriptable — so this example only works because the
    `scope` name is honoured.
    """
    printed: list = []

    @as_middleware
    async def logging_mw(scope, receive, send, call_next):
        await call_next(scope, receive, send)
        printed.append(f"{scope['method']} {scope['path']}")

    await _run(logging_mw)

    assert printed == ['GET /']
    with pytest.raises(TypeError):
        _conn()['method']       # the shape the example would otherwise get


@pytest.mark.asyncio
async def test_scope_declaring_middleware_sees_asgi_events_on_send():
    """It will inspect `event['type']`, so give it dicts — and only it."""
    captured: list = []

    @as_middleware
    async def asgi_mw(scope, receive, send, call_next):
        async def tap(event):
            captured.append(event)
            await send(event)
        await call_next(scope, receive, tap)

    out, _ = await _run(asgi_mw)

    assert captured, 'the middleware send wrapper never fired'
    assert all(isinstance(e, dict) for e in captured), (
        f'ASGI middleware got non-dict events: '
        f'{[type(e).__name__ for e in captured]}')
    assert captured[0]['type'] == 'http.response.start'
    assert captured[0]['status'] == 200
    # …and its emissions are converted back before leaving.
    assert all(isinstance(e, NativeResponse) for e in out), (
        f'dicts escaped the adapter: {[type(e).__name__ for e in out]}')


@pytest.mark.asyncio
async def test_scope_declaring_class_form():
    seen: dict = {}

    @as_middleware
    class AsgiCls:
        async def __call__(self, scope, receive, send, call_next):
            seen['type'] = type(scope)
            await call_next(scope, receive, send)

    out, below = await _run(AsgiCls())

    assert seen['type'] is dict
    assert all(isinstance(c, Connection) for c in below)
    assert all(isinstance(e, NativeResponse) for e in out)


@pytest.mark.asyncio
async def test_native_middleware_is_not_adapted():
    """The default path must be untouched — no scope built, no dict events."""
    seen: dict = {}
    captured: list = []

    @as_middleware
    async def native_mw(conn, receive, send, call_next):
        seen['type'] = type(conn)

        async def tap(event):
            captured.append(event)
            await send(event)
        await call_next(conn, receive, tap)

    out, below = await _run(native_mw)

    assert seen['type'] is Connection
    assert all(isinstance(e, NativeResponse) for e in captured), (
        f'a native middleware was handed dicts: '
        f'{[type(e).__name__ for e in captured]}')
    assert all(isinstance(c, Connection) for c in below)
    assert all(isinstance(e, NativeResponse) for e in out)


def test_declaration_is_recorded_at_decoration_time():
    """Signature inspection happens once, not per request."""
    @as_middleware
    async def asgi_mw(scope, receive, send, call_next):
        ...

    @as_middleware
    async def native_mw(conn, receive, send, call_next):
        ...

    assert getattr(asgi_mw, '__blackbull_asgi_scope__') is True
    assert getattr(native_mw, '__blackbull_asgi_scope__') is False

"""A native WebSocket message survives every middleware on the chain.

The §6 native-ization gave the WS send channel `NativeWSMessage`, and two
middleware send wrappers broke on it: they assumed everything that is not a
`NativeResponse` is a dict, and called `.get('type')` on it.  `NativeWSMessage`
is a `__slots__` class with no `.get`, so an object-form WebSocket handler
raised `AttributeError` the moment it sent through `CORS` or `Compression`.

The architecture guard could not see this: it enumerates dict *producers* —
literals and `to_asgi()` calls — and this is a *consumer* assuming dictness.
So the enforcement here is behavioural rather than structural: run an
object-form handler through each middleware and require the message to arrive.

Both handler forms are exercised.  The raw `(conn, receive, send)` form kept
working throughout (its events really are dicts), which is exactly why the
break was invisible until the object form was tried.
"""
import pytest

from blackbull import BlackBull
from blackbull.middleware.cache import Cache
from blackbull.middleware.compression import Compression
from blackbull.middleware.cors import CORS
from blackbull.testing import TestClient
from blackbull.utils import Scheme
from blackbull.websocket import WebSocket


# Every middleware a user may install with ``app.use``, with the arguments
# each needs.  A hand-written list would go stale the moment one is added, so
# ``test_every_public_middleware_is_covered`` checks this against the package's
# own exports and fails if the two drift.
_FACTORIES = {
    'CORS': lambda: CORS(allow_origins=['*']),
    'Compression': lambda: Compression(),
    'Cache': lambda: Cache(),
    # Not a send-wrapper: StaticFiles is a producer and TrustedProxy only
    # rewrites the request, so neither can eat an outbound WS message.  Listed
    # so the coverage check below is a real check rather than a tautology.
    'StaticFiles': None,
    'TrustedProxy': None,
}

MIDDLEWARE = [(name, f) for name, f in _FACTORIES.items() if f is not None]


def _app(*middleware):
    app = BlackBull()
    for mw in middleware:
        app.use(mw)

    @app.route(path='/object', scheme=Scheme.websocket)
    async def object_form(ws: WebSocket):
        await ws.accept()
        async for message in ws:
            await ws.send_text(f'echo:{message}')

    @app.route(path='/raw', scheme=Scheme.websocket)
    async def raw_form(conn, receive, send):
        await receive()                       # websocket.connect
        await send({'type': 'websocket.accept'})
        while True:
            event = await receive()
            if event['type'] == 'websocket.disconnect':
                return
            await send({'type': 'websocket.send',
                        'text': f"echo:{event.get('text')}"})

    return app


@pytest.mark.parametrize('name,factory', MIDDLEWARE, ids=[m[0] for m in MIDDLEWARE])
@pytest.mark.parametrize('path', ['/object', '/raw'])
def test_ws_message_survives_each_middleware(name, factory, path):
    """A message sent by the handler must reach the client through *name*."""
    app = _app(factory())
    with TestClient(app) as client:
        with client.websocket_connect(path) as ws:
            ws.send_text('hi')
            assert ws.receive_text() == 'echo:hi', (
                f'{name} did not pass the message through on {path}')


@pytest.mark.parametrize('path', ['/object', '/raw'])
def test_ws_message_survives_the_whole_chain(path):
    """All of them stacked — the shape a real app installs."""
    app = _app(*(factory() for _, factory in MIDDLEWARE))
    with TestClient(app) as client:
        with client.websocket_connect(path) as ws:
            ws.send_text('hi')
            assert ws.receive_text() == 'echo:hi'


def test_cors_sees_an_allowed_origin_and_still_passes_the_message():
    """CORS's injecting wrapper only wraps `send` when an Origin is allowed.

    Without the header the middleware returns early and never installs the
    wrapper, so the crash path would not be reached — this is the case that
    actually exercised it.
    """
    app = _app(CORS(allow_origins=['https://app.example.com']))
    with TestClient(app) as client:
        with client.websocket_connect(
                '/object',
                headers=[('origin', 'https://app.example.com')]) as ws:
            ws.send_text('hi')
            assert ws.receive_text() == 'echo:hi'


def test_every_public_middleware_is_covered():
    """The parametrisation must track `blackbull.middleware`'s exports.

    A middleware added after this test was written would otherwise never be
    run against a native WS message — which is exactly how the two broken
    wrappers shipped.
    """
    import blackbull.middleware as mw_pkg

    public = {name for name in dir(mw_pkg)
              if not name.startswith('_') and name[0].isupper()}
    missing = public - set(_FACTORIES)
    assert not missing, (
        f'middleware with no entry in _FACTORIES: {sorted(missing)} — add a '
        f'factory to exercise it, or None with a reason if it wraps no send')

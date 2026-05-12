"""Utilities for middleware authors.

Public API:
- ``middleware``: decorator that normalises the ``send`` callable so inner send
  wrappers defined by the middleware always receive plain ASGI event dicts,
  never ``Response`` objects.
- ``_normalize_send``: the underlying normaliser; available as an escape hatch
  for class-based middleware that cannot use the decorator directly.
"""
from functools import wraps

from ..response import Response


def _normalize_send(inner_send):
    """Return a wrapper around *inner_send* that expands Response objects.

    Handlers that use the simplified return-value form call ``send`` with a
    ``Response`` (or ``JSONResponse``) object.  Middleware that wraps ``send``
    would otherwise need an ``isinstance`` guard for every response type.
    This wrapper intercepts ``Response`` objects and emits the two ASGI events
    (``http.response.start`` + ``http.response.body``) that ``inner_send``
    expects, forwarding all other arguments unchanged.
    """
    async def normalized(event, *args, **kwargs):
        if isinstance(event, Response):
            await inner_send({
                'type': 'http.response.start',
                'status': event.status,
                'headers': list(event.headers),
            })
            await inner_send({
                'type': 'http.response.body',
                'body': event.body,
                'more_body': False,
            })
        else:
            await inner_send(event, *args, **kwargs)

    return normalized


def middleware(fn):
    """Decorator for middleware functions.

    Wraps ``call_next`` so any ``send`` callable the middleware passes to it is
    automatically normalised via ``_normalize_send``.  The middleware's own
    inner ``send`` wrapper therefore only receives plain ASGI event dicts and
    does not need ``isinstance`` guards for ``Response`` objects.

    Power users who need to handle raw ``send`` arguments (e.g. because their
    middleware is used in a context where no simplified handlers are registered)
    should omit this decorator — their ``call_next`` is then wired directly to
    the next handler with no extra wrapping.

    Usage::

        from blackbull import middleware

        @middleware
        async def timing_mw(scope, receive, send, call_next):
            async def timed_send(event):
                # event is always a dict here
                await send(event)
            await call_next(scope, receive, timed_send)
    """
    @wraps(fn)
    async def wrapper(scope, receive, send, call_next):
        async def normalizing_call_next(scope, receive, inner_send):
            return await call_next(scope, receive, _normalize_send(inner_send))
        return await fn(scope, receive, send, normalizing_call_next)

    wrapper.__blackbull_middleware__ = True
    return wrapper

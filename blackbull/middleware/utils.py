"""Utilities for middleware authors.

Public API:
- ``as_middleware``: decorator that normalises the ``send`` callable so inner
  send wrappers defined by the middleware always receive a single native
  representation — ``NativeResponse`` on the H1 native path, plain ASGI event
  dicts elsewhere — never raw ``Response`` objects.  Works on both async
  middleware functions and middleware classes (decorates ``__call__``).
"""
from functools import wraps

from ..asgi import ASGISendCallable
from ..response import wrap_native_send


def _normalize_send(inner_send: ASGISendCallable | None):
    """Return a wrapper around *inner_send* that normalises to native.

    On the H1 native path the handler boundary already converts every shape
    to :class:`~blackbull.native.NativeResponse`, so the middleware's inner
    send wrapper observes native objects.  This wrapper guarantees the same
    contract regardless of the seam: ``Response`` / ``StreamingResponse`` /
    3-arg / ASGI dict shapes from the handler are all converted to
    ``NativeResponse`` before reaching ``inner_send`` — the exact conversion
    the app applies at its handler boundary (shared via
    :func:`blackbull.response.wrap_native_send`), so global and per-route
    middleware see one representation.

    ASGI ``send`` is always called with a single positional event — no
    ``*args/**kwargs`` form needs to be preserved here, and dropping it
    shaves a per-event call-frame setup that shows in py-spy profiles of
    the static path.
    """
    # ``inner_send`` is Optional because a middleware may be driven with no
    # send channel at all on pass-through paths (a websocket or lifespan
    # scope a middleware declines to touch).  The wrapper is built either
    # way; it is simply never invoked in that case.
    return wrap_native_send(inner_send)


def as_middleware(target):
    """Decorator that marks an async function **or** class as BlackBull middleware.

    Wraps ``call_next`` so any ``send`` callable the middleware passes to it is
    automatically normalised — Response/JSONResponse objects are expanded into
    ASGI event dicts before reaching the middleware's inner ``send`` wrapper.
    The wrapper therefore only ever sees plain dict events and does not need
    ``isinstance`` guards.

    Applied to an async function (signature ``(conn, receive, send, call_next)``)::

        @as_middleware
        async def timing_mw(conn, receive, send, call_next):
            async def timed_send(event):
                # event is always a dict here
                await send(event)
            await call_next(conn, receive, timed_send)

    Applied to a class whose ``__call__`` is the middleware coroutine::

        @as_middleware
        class Cache:
            async def __call__(self, conn, receive, send, call_next):
                async def cap_send(event):
                    # event is always a dict here
                    ...
                await call_next(conn, receive, cap_send)

    Power users who need to handle raw ``send`` arguments (e.g. because their
    middleware is used in a context where no simplified handlers are registered)
    should omit this decorator — their ``call_next`` is then wired directly to
    the next handler with no extra wrapping.
    """
    if isinstance(target, type):
        original_call = target.__call__

        @wraps(original_call)
        async def wrapped_call(self, conn, receive, send, call_next):
            async def normalizing_call_next(conn, receive, inner_send):
                return await call_next(conn, receive, _normalize_send(inner_send))
            return await original_call(self, conn, receive, send, normalizing_call_next)

        target.__call__ = wrapped_call
        target.__blackbull_middleware__ = True
        return target

    @wraps(target)
    async def wrapper(conn, receive, send, call_next):
        async def normalizing_call_next(conn, receive, inner_send):
            return await call_next(conn, receive, _normalize_send(inner_send))
        return await target(conn, receive, send, normalizing_call_next)

    wrapper.__blackbull_middleware__ = True
    return wrapper

"""Utilities for middleware authors.

Public API:
- ``as_middleware``: decorator that normalises the ``send`` callable so inner
  send wrappers defined by the middleware always receive a single native
  representation — ``NativeResponse`` on the HTTP path (H1 + H2 since Sprint
  93), plain ASGI event dicts only at the external-host edge — never raw
  ``Response`` objects.  Works on both async middleware functions and
  middleware classes (decorates ``__call__``).
"""
from functools import wraps

from ..asgi import ASGISendCallable
from ..response import wrap_native_send


def _normalize_send(inner_send: ASGISendCallable | None):
    """Return a wrapper around *inner_send* converting every shape to native.

    The native-path normalisation (shared with the app's handler-boundary
    adapter via :func:`blackbull.response.wrap_native_send`): ``Response`` /
    ``StreamingResponse`` / 3-arg / ASGI dict / NativeResponse all become a
    single native representation before reaching ``inner_send``, so middleware
    observes one contract on the HTTP path.  The H2 sender has a native arm
    since Sprint 93, so no dict fallback is needed (the Sprint 92 H2 gate
    dropped with it).
    """
    # ``inner_send`` is Optional because a middleware may be driven with no
    # send channel at all on pass-through paths (a websocket or lifespan
    # scope a middleware declines to touch).  The wrapper is built either
    # way; it is simply never invoked in that case.
    return wrap_native_send(inner_send)


def as_middleware(target):
    """Decorator that marks an async function **or** class as BlackBull middleware.

    Wraps ``call_next`` so any ``send`` callable the middleware passes to it is
    automatically normalised — Response/JSONResponse objects are converted to
    NativeResponse before reaching the middleware's inner ``send`` wrapper.
    The wrapper therefore only ever sees the native representation
    (``NativeResponse`` on the HTTP path — H1 and H2).

    Applied to an async function (signature ``(conn, receive, send, call_next)``)::

        @as_middleware
        async def timing_mw(conn, receive, send, call_next):
            async def timed_send(event):
                # event is a NativeResponse on the HTTP path
                await send(event)
            await call_next(conn, receive, timed_send)

    Applied to a class whose ``__call__`` is the middleware coroutine::

        @as_middleware
        class Cache:
            async def __call__(self, conn, receive, send, call_next):
                async def cap_send(event):
                    # event is a NativeResponse on the HTTP path
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
                return await call_next(
                    conn, receive, _normalize_send(inner_send))
            return await original_call(self, conn, receive, send, normalizing_call_next)

        target.__call__ = wrapped_call
        target.__blackbull_middleware__ = True
        return target

    @wraps(target)
    async def wrapper(conn, receive, send, call_next):
        async def normalizing_call_next(conn, receive, inner_send):
            return await call_next(
                conn, receive, _normalize_send(inner_send))
        return await target(conn, receive, send, normalizing_call_next)

    wrapper.__blackbull_middleware__ = True
    return wrapper

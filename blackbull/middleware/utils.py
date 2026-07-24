"""Utilities for middleware authors.

Public API:
- ``as_middleware``: decorator that normalises the ``send`` callable so inner
  send wrappers defined by the middleware always receive plain ASGI event
  dicts, never ``Response`` objects.  Works on both async middleware functions
  and middleware classes (decorates ``__call__``).
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
    expects, forwarding all other event dicts unchanged.

    ASGI ``send`` is always called with a single positional event — no
    ``*args/**kwargs`` form needs to be preserved here, and dropping it
    shaves a per-event call-frame setup that showed in the Sprint 33
    py-spy profile on the static path.

    The Response→ASGI expansion is delegated to :meth:`Response.__call__` so
    there is a single source of truth for the start/body event shape (shared
    with ``app._wrap_send`` and the simplified-handler dispatch).
    """
    async def normalized(event):
        if isinstance(event, Response):
            # Response is ASGI-callable and ignores conn/receive (it is a pure
            # serialiser wearing the ASGI-app signature), so drive it with the
            # inner send to reuse the one Response→ASGI path.
            await event(None, None, inner_send)
        else:
            await inner_send(event)

    return normalized


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

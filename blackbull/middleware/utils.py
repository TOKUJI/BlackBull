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
from ..response import Response, wrap_native_send


def _normalize_dict_send(inner_send: ASGISendCallable | None):
    """Return a wrapper around *inner_send* that expands Response objects.

    The v0.69 ASGI-lane normalisation: handlers that use the simplified
    return-value form call ``send`` with a ``Response`` object; this wrapper
    expands it to the two ASGI events (``http.response.start`` +
    ``http.response.body``) that ``inner_send`` expects, and forwards every
    other event dict unchanged.  Used on the H2 / external ASGI lanes where
    the wire contract stays dict — never converts to ``NativeResponse``.
    """
    # ``inner_send`` is Optional because a middleware may be driven with no
    # send channel at all on pass-through paths (a websocket or lifespan
    # scope a middleware declines to touch).  The wrapper is built either
    # way; it is simply never invoked in that case.
    # Unannotated on purpose: rebuilt per request (see _wrap_send in app.py).
    # ``event`` is an ASGISendEvent or a Response.
    async def normalized(event):
        if isinstance(event, Response):
            # Response is ASGI-callable and ignores conn/receive (it is a pure
            # serialiser wearing the ASGI-app signature), so drive it with the
            # inner send to reuse the one Response→ASGI path.
            await event(None, None, inner_send)
        else:
            await inner_send(event)

    return normalized


def _normalize_send(inner_send: ASGISendCallable | None, *, native: bool = True):
    """Return a wrapper around *inner_send* normalising to the lane's contract.

    * ``native=True`` (the H1 native path) — every shape (``Response`` /
      ``StreamingResponse`` / 3-arg / ASGI dict) is converted to
      :class:`~blackbull.native.NativeResponse` before reaching
      ``inner_send`` (shared with the app's handler-boundary adapter via
      :func:`blackbull.response.wrap_native_send`), so middleware sees one
      native representation.
    * ``native=False`` (the H2 / external ASGI lanes) — the v0.69 contract:
      ``Response`` objects are expanded to ASGI events, everything else
      passes through as dicts.  **Never** converts to ``NativeResponse`` on
      these lanes — the H2 sender has no native arm yet (the H2 gate), so a
      leaked ``NativeResponse`` would ``TypeError`` it.

    ``as_middleware`` picks the flag from ``conn.http_version``.
    """
    if native:
        return wrap_native_send(inner_send)
    return _normalize_dict_send(inner_send)


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
                # Protocol-aware: native by default (the H1 / Sprint 92
                # contract), v0.69 dict normalisation on the H2 lane (the
                # H2 sender has no native arm yet — a leaked NativeResponse
                # would TypeError it; the gate drops with the H2 sprint).
                native = getattr(conn, 'http_version', '1.1') == '1.1'
                return await call_next(
                    conn, receive, _normalize_send(inner_send, native=native))
            return await original_call(self, conn, receive, send, normalizing_call_next)

        target.__call__ = wrapped_call
        target.__blackbull_middleware__ = True
        return target

    @wraps(target)
    async def wrapper(conn, receive, send, call_next):
        async def normalizing_call_next(conn, receive, inner_send):
            native = getattr(conn, 'http_version', '1.1') == '1.1'
            return await call_next(
                conn, receive, _normalize_send(inner_send, native=native))
        return await target(conn, receive, send, normalizing_call_next)

    wrapper.__blackbull_middleware__ = True
    return wrapper

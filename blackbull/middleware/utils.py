"""Utilities for middleware authors.

Public API:
- ``as_middleware``: decorator that normalises the ``send`` callable so inner
  send wrappers defined by the middleware always receive a single native
  representation — ``NativeResponse`` on the HTTP path (H1 + H2 since Sprint
  93), plain ASGI event dicts only at the external-host edge — never raw
  ``Response`` objects.  Works on both async middleware functions and
  middleware classes (decorates ``__call__``).
"""
import inspect
from functools import wraps

from ..asgi import ASGISendCallable
from ..response import wrap_native_send


def _declares_asgi_scope(fn, *, is_method: bool) -> bool:
    """True when *fn*'s first request parameter is literally named ``scope``.

    The name **is** the declaration.  Everywhere else in BlackBull ``scope``
    means a genuine ASGI scope dict and never a :class:`Connection` — the
    router *rejects* the name for simplified handlers on exactly that ground.
    Here it is honoured rather than rejected: a middleware that asks for
    ``scope`` was written against ASGI, so it is handed a real scope dict and
    adapted at its own two edges.  A middleware that asks for anything else
    (``conn``, ``connection``, …) is native and is not adapted at all.

    Signature inspection happens once, at decoration time — never per request.
    """
    try:
        params = list(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        # Builtins / C callables have no introspectable signature; treat them
        # as native rather than guessing.
        return False
    if is_method:
        params = params[1:]           # drop ``self``
    return bool(params) and params[0] == 'scope'


def _to_asgi_send(inner_send):
    """Expand native emissions to ASGI event dicts for a scope-declared
    middleware's own ``send`` wrapper.

    The inverse of :func:`_normalize_send`, and the reason the pair is safe:
    an ASGI-written middleware inspects ``event['type']``, so what reaches it
    from below must be dicts — on the WebSocket path as much as the HTTP one.
    The dict form is created here and consumed again at the same middleware's
    exit — it never travels further in either direction.
    """
    from ..native import asgi_send_boundary  # noqa: PLC0415

    return asgi_send_boundary(inner_send)


def _adapt(conn, send, wants_scope: bool):
    """Resolve the three per-request pieces for one middleware invocation.

    Returns ``(request_arg, outward_send, inner_normaliser)``.

    Native middleware (the default) get the :class:`Connection` untouched, an
    unwrapped ``send``, and a native inner normaliser — no adaptation at all,
    so the single-world path pays nothing for this feature.

    A scope-declaring middleware is adapted at both of its edges, and only
    there: the scope dict is built on the way in, its own emissions are
    converted back to native on the way out, and what reaches its ``send``
    wrapper from below is expanded to dicts.  The dict form therefore exists
    across exactly one frame and is gone again on both sides of it.
    """
    if not wants_scope:
        return conn, send, _normalize_send
    from ..connection import Connection  # noqa: PLC0415
    arg = conn.to_asgi_scope() if isinstance(conn, Connection) else conn
    return arg, wrap_native_send(send), _to_asgi_send


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
        wants_scope = _declares_asgi_scope(original_call, is_method=True)

        @wraps(original_call)
        async def wrapped_call(self, conn, receive, send, call_next):
            arg, send, inner = _adapt(conn, send, wants_scope)

            async def normalizing_call_next(_arg, receive, inner_send):
                # The native Connection always goes down, whatever the
                # middleware handed back — a scope dict must not outlive the
                # middleware that asked for it.
                return await call_next(conn, receive, inner(inner_send))

            return await original_call(self, arg, receive, send,
                                       normalizing_call_next)

        target.__call__ = wrapped_call
        target.__blackbull_middleware__ = True
        target.__blackbull_asgi_scope__ = wants_scope
        return target

    wants_scope = _declares_asgi_scope(target, is_method=False)

    @wraps(target)
    async def wrapper(conn, receive, send, call_next):
        arg, send, inner = _adapt(conn, send, wants_scope)

        async def normalizing_call_next(_arg, receive, inner_send):
            return await call_next(conn, receive, inner(inner_send))

        return await target(arg, receive, send, normalizing_call_next)

    wrapper.__blackbull_middleware__ = True
    setattr(wrapper, '__blackbull_asgi_scope__', wants_scope)
    return wrapper

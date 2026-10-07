"""Middleware send adapters.

Native wrappers receive NativeResponse on HTTP paths; ASGI dictionaries
are introduced only at compatibility boundaries, never raw Response objects.
"""
import inspect
from functools import wraps

from ..asgi import ASGISendCallable
from ..response import wrap_native_send


def _declares_asgi_scope(fn, *, is_method: bool) -> bool:
    """Detect a first request parameter named scope once, at decoration.
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

    The inverse of [`_normalize_send`][], and the reason the pair is safe:
    an ASGI-written middleware inspects ``event['type']``, so what reaches it
    from below must be dicts — on the WebSocket path as much as the HTTP one.
    The dict form is created here and consumed again at the same middleware's
    exit — it never travels further in either direction.
    """
    from ..native import asgi_send_boundary  # noqa: PLC0415

    return asgi_send_boundary(inner_send)


def _adapt(conn, send, wants_scope: bool):
    """Return (request_arg, outward_send, inner_normalizer) for native or scope-declared middleware.
    """
    if not wants_scope:
        return conn, send, _normalize_send
    from ..connection import Connection  # noqa: PLC0415
    arg = conn.to_asgi_scope() if isinstance(conn, Connection) else conn
    return arg, wrap_native_send(send), _to_asgi_send


def _normalize_send(inner_send: ASGISendCallable | None):
    """Normalize HTTP send shapes to NativeResponse using wrap_native_send.
    """
    # ``inner_send`` is Optional because a middleware may be driven with no
    # send channel at all on pass-through paths (a websocket or lifespan
    # scope a middleware declines to touch).  The wrapper is built either
    # way; it is simply never invoked in that case.
    return wrap_native_send(inner_send)


def as_middleware(target):
    """Decorate a function or class with (conn, receive, send, call_next) middleware.

    Normalize downstream HTTP sends to NativeResponse before the middleware's
    send wrapper. A first request parameter named scope opts into ASGI scope
    and event dictionaries at both edges, including WebSocket events.
    Omit the decorator to handle raw downstream send arguments.
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

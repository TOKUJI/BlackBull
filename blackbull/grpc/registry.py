"""gRPC service registry — maps ``/package.Service/Method`` to a handler.

This is the gRPC analogue of :class:`blackbull.router.Router`: it holds the
table of method paths and the coroutine that serves each one.

A **unary** handler is::

    async def handler(request: bytes, context: GrpcContext) -> bytes

A **server-streaming** handler is an async generator that yields zero or more
response messages::

    async def handler(request: bytes, context: GrpcContext):
        yield b'...'
        yield b'...'

In both cases ``request`` is the (already de-framed) request message body and
the yielded / returned value is a response message body.  Protobuf
(de)serialisation is the handler's responsibility — the framework stays
dependency-free.  Handlers signal a non-OK result by raising
:class:`GrpcError` or calling ``context.abort(...)``.

A **client-streaming** or **bidirectional** handler takes an async iterator of
request messages instead of a single ``request``::

    async def handler(request_iter, context) -> bytes:   # client-streaming
        async for message in request_iter:
            ...

    async def handler(request_iter, context):            # bidirectional
        async for message in request_iter:
            yield ...

Response-streaming is detected automatically at registration via
:func:`inspect.isasyncgenfunction`; request-streaming is detected from the first
parameter name (``request_iter`` / ``requests`` / ``request_iterator`` /
``request_stream``).  Pass ``streaming=`` / ``client_streaming=`` explicitly to
override when a decorator hides the handler's nature.
"""
from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import NamedTuple

GrpcHandler = Callable[..., Awaitable[bytes]]

# First-parameter names that mark a handler as client-streaming (the request is
# an async iterator of messages).  Detection mirrors the framework's simplified-
# handler convention of inspecting parameter names; an explicit
# ``client_streaming=`` override always wins.
_REQUEST_STREAM_PARAMS = frozenset(
    {'request_iter', 'request_iterator', 'requests', 'request_stream'})


def _first_param_name(handler: Callable[..., object]) -> str | None:
    """Return the handler's first request parameter name (skipping
    ``self``/``cls``), or ``None`` if it takes no positional parameter."""
    try:
        params = inspect.signature(handler).parameters
    except (ValueError, TypeError):
        return None
    for name in params:
        if name in ('self', 'cls'):
            continue
        return name
    return None


class GrpcMethod(NamedTuple):
    """A registered method: its *handler* and the two streaming axes.

    *streaming* is the **response** axis (the handler is an async generator that
    yields response messages); *client_streaming* is the **request** axis (the
    handler takes an async iterator of request messages instead of a single
    ``request: bytes``).  The four combinations are unary, server-streaming,
    client-streaming, and bidirectional.

    *handler* is annotated as a bare :class:`~collections.abc.Callable` rather
    than :data:`GrpcHandler`: a response-streaming handler is an async generator
    function (its call returns an async iterator, not an ``Awaitable``), and a
    plain ``Callable`` is the one form that stays runtime-isinstanceable for the
    NamedTuple field check (see memory ``beartype-namedtuple-callable-alias``).
    """
    handler: Callable[..., object]
    streaming: bool
    client_streaming: bool = False


def _normalise(path: str) -> str:
    """Return *path* as a leading-slash ``/Service/Method`` key."""
    return path if path.startswith('/') else '/' + path


class GrpcServiceRegistry:
    """Holds the ``path -> GrpcMethod`` table for gRPC methods."""

    def __init__(self) -> None:
        self._methods: dict[str, GrpcMethod] = {}

    def add_method(self, path: str, handler: GrpcHandler, *,
                   streaming: bool | None = None,
                   client_streaming: bool | None = None) -> None:
        """Register *handler* for the fully-qualified method *path*
        (``/package.Service/Method`` or ``package.Service/Method``).

        *streaming* is the **response** axis — server-streaming (the handler is
        an async generator that yields response messages).  When ``None`` (the
        default) it is auto-detected with :func:`inspect.isasyncgenfunction`;
        pass ``True`` to force it for a handler whose async-generator nature is
        hidden behind a wrapper, or ``False`` to force a single response.
        Forcing ``False`` on an async-generator function is a contradiction and
        raises ``ValueError``.

        *client_streaming* is the **request** axis — the handler takes an async
        iterator of request messages (first parameter ``request_iter`` /
        ``requests`` / ``request_iterator`` / ``request_stream``) instead of a
        single ``request: bytes``.  When ``None`` it is auto-detected from that
        first parameter name; pass an explicit bool to override.
        """
        key = _normalise(path)
        if key in self._methods:
            raise ValueError(f'Duplicate gRPC method {key!r}')
        is_asyncgen = inspect.isasyncgenfunction(handler)
        if streaming is None:
            streaming = is_asyncgen
        elif streaming is False and is_asyncgen:
            raise ValueError(
                f'{key!r}: handler is an async generator (server-streaming) but '
                f'streaming=False was requested')
        if client_streaming is None:
            client_streaming = _first_param_name(handler) in _REQUEST_STREAM_PARAMS
        self._methods[key] = GrpcMethod(handler, streaming, client_streaming)

    def method(self, path: str, *, streaming: bool | None = None,
               client_streaming: bool | None = None
               ) -> Callable[[GrpcHandler], GrpcHandler]:
        """Decorator form of :meth:`add_method`."""
        def decorator(handler: GrpcHandler) -> GrpcHandler:
            self.add_method(path, handler, streaming=streaming,
                            client_streaming=client_streaming)
            return handler
        return decorator

    def add_service(self, service: str, methods: dict[str, GrpcHandler]) -> None:
        """Register every ``method_name -> handler`` in *methods* under the
        fully-qualified *service* name (e.g. ``"helloworld.Greeter"``).

        Each handler's streaming-ness is auto-detected individually."""
        for name, handler in methods.items():
            self.add_method(f'/{service}/{name}', handler)

    def lookup(self, path: str) -> GrpcHandler | None:
        """Return the handler for *path*, or ``None`` if unregistered.

        Backwards-compatible accessor (returns just the callable); use
        :meth:`lookup_method` when the streaming flag is needed."""
        method = self._methods.get(_normalise(path))
        return method.handler if method is not None else None

    def lookup_method(self, path: str) -> GrpcMethod | None:
        """Return the :class:`GrpcMethod` for *path*, or ``None`` if
        unregistered."""
        return self._methods.get(_normalise(path))

    def methods(self) -> list[str]:
        """Return all registered method paths, in registration order."""
        return list(self._methods)

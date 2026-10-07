"""gRPC method registration.

Response streaming is inferred from async-generator functions; request
streaming from request_iter, request_iterator, requests or request_stream.
Use explicit flags when a decorator hides these forms. Handler messages are
bytes; raise GrpcError or call context.abort for non-OK status.
"""
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
    """Registered handler with request and response streaming flags.

    client_streaming receives an async message iterator; streaming returns one.
    The four flag combinations select unary, server, client or bidirectional calls.
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

        Both streaming axes default to ``None``, meaning auto-detection —
        see [`GrpcMethod`][] for what each axis is.  Pass a bool to
        override it for a handler whose nature a wrapper hides.  Forcing
        *streaming* ``False`` on an async-generator function is a
        contradiction and raises ``ValueError``.
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
        """Decorator form of [`add_method`][]."""
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
        [`lookup_method`][] when the streaming flag is needed."""
        method = self._methods.get(_normalise(path))
        return method.handler if method is not None else None

    def lookup_method(self, path: str) -> GrpcMethod | None:
        """Return the [`GrpcMethod`][] for *path*, or ``None`` if
        unregistered."""
        return self._methods.get(_normalise(path))

    def methods(self) -> list[str]:
        """Return all registered method paths, in registration order."""
        return list(self._methods)

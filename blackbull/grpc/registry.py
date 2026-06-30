"""gRPC service registry — maps ``/package.Service/Method`` to a handler.

This is the gRPC analogue of :class:`blackbull.router.Router`: it holds the
table of method paths and the coroutine that serves each one.  A gRPC handler
is::

    async def handler(request: bytes, context: GrpcContext) -> bytes

where ``request`` is the (already de-framed) request message body and the
return value is the response message body.  Protobuf (de)serialisation is the
handler's responsibility — the framework stays dependency-free.  Handlers
signal a non-OK result by raising :class:`GrpcError` or calling
``context.abort(...)``.

Only unary RPCs are served in this first cut; streaming method types are a
follow-up (the wire path — DATA frames + trailers — is already in place).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

GrpcHandler = Callable[..., Awaitable[bytes]]


def _normalise(path: str) -> str:
    """Return *path* as a leading-slash ``/Service/Method`` key."""
    return path if path.startswith('/') else '/' + path


class GrpcServiceRegistry:
    """Holds the ``path -> handler`` table for unary gRPC methods."""

    def __init__(self) -> None:
        self._methods: dict[str, GrpcHandler] = {}

    def add_method(self, path: str, handler: GrpcHandler) -> None:
        """Register *handler* for the fully-qualified method *path*
        (``/package.Service/Method`` or ``package.Service/Method``)."""
        key = _normalise(path)
        if key in self._methods:
            raise ValueError(f'Duplicate gRPC method {key!r}')
        self._methods[key] = handler

    def method(self, path: str) -> Callable[[GrpcHandler], GrpcHandler]:
        """Decorator form of :meth:`add_method`."""
        def decorator(handler: GrpcHandler) -> GrpcHandler:
            self.add_method(path, handler)
            return handler
        return decorator

    def add_service(self, service: str, methods: dict[str, GrpcHandler]) -> None:
        """Register every ``method_name -> handler`` in *methods* under the
        fully-qualified *service* name (e.g. ``"helloworld.Greeter"``)."""
        for name, handler in methods.items():
            self.add_method(f'/{service}/{name}', handler)

    def lookup(self, path: str) -> GrpcHandler | None:
        """Return the handler for *path*, or ``None`` if unregistered."""
        return self._methods.get(_normalise(path))

    def methods(self) -> list[str]:
        """Return all registered method paths, in registration order."""
        return list(self._methods)

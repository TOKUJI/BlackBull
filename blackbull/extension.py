"""Extension mechanism — the single, generic plugin contract (Sprint 53).

An :class:`Extension` wires itself into a :class:`~blackbull.app.BlackBull`
application through the public ``app.*`` API: routes, middleware, event
listeners, and — for non-HTTP protocols — ``app.register_protocol_handler``.
Registration goes through one core method, :meth:`BlackBull.add_extension`, so
the core class carries **no** protocol-specific surface.

This generalises the convention that ``OpenAPIExtension`` already followed
(``extension_key`` + ``init_app(app)`` + self-storage in ``app.extensions``).
Protocol extensions (the MQTT broker is the reference) are *just* extensions
that call ``register_protocol_handler`` in :meth:`init_app` — there is
deliberately no separate "protocol extension" base class until a second
protocol justifies one.

See ``docs/guide/extensions.md``.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar


class Extension(ABC):
    """Base class for BlackBull extensions.

    Subclasses set :attr:`extension_key` and implement :meth:`init_app`.  They
    may optionally override :meth:`startup` / :meth:`shutdown` for async
    resource lifecycle; :meth:`BlackBull.add_extension` wires those into the
    application's ``app_startup`` / ``app_shutdown`` lifespan events.

    ``add_extension`` accepts any object exposing ``init_app(app)``, so legacy
    duck-typed extensions keep working without adopting this base class.
    """

    #: Key under which the extension stores itself in ``app.extensions``.
    extension_key: ClassVar[str]

    @abstractmethod
    def init_app(self, app: Any) -> None:
        """Wire this extension into *app* (synchronous).

        Called by :meth:`BlackBull.add_extension`.  Register routes,
        middleware, protocol handlers, and event listeners through the public
        ``app.*`` API, then call :meth:`_register` to store ``self`` at
        ``app.extensions[extension_key]``.
        """

    async def startup(self, app: Any) -> None:
        """Async startup hook, run at lifespan ``app_startup``.  Default no-op."""

    async def shutdown(self, app: Any) -> None:
        """Async shutdown hook, run at lifespan ``app_shutdown``.  Default no-op."""

    def _register(self, app: Any) -> None:
        """Store ``self`` at ``app.extensions[extension_key]``, guarding against
        a different extension already holding the key.  Idempotent for *self*."""
        key = self.extension_key
        existing = app.extensions.get(key)
        if existing is not None and existing is not self:
            raise RuntimeError(
                f"app.extensions[{key!r}] is already registered by "
                f"{type(existing).__module__}.{type(existing).__name__}; cannot "
                f"register {type(self).__module__}.{type(self).__name__}.")
        app.extensions[key] = self

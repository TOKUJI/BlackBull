"""Extensions register through app.add_extension and public app APIs.

Protocol extensions use the same lifecycle and registration mechanism;
do not add a separate protocol-extension base without a shared requirement.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar


class Extension(ABC):
    """Base class for BlackBull extensions.

    Subclasses set ``extension_key`` and implement ``init_app``.  They
    may optionally override [`startup`][] / [`shutdown`][] for async
    resource lifecycle; [`BlackBull.add_extension`][BlackBull.add_extension] wires those into the
    application's ``app_startup`` / ``app_shutdown`` lifespan events.

    ``add_extension`` accepts any object exposing ``init_app(app)``, so a
    duck-typed extension works without adopting this base class.
    """

    #: Key under which the extension stores itself in ``app.extensions``.
    extension_key: ClassVar[str]

    @abstractmethod
    def init_app(self, app: Any) -> None:
        """Wire this extension into *app* (synchronous).

        Called by [`BlackBull.add_extension`][BlackBull.add_extension].  Register routes,
        middleware, protocol handlers, and event listeners through the public
        ``app.*`` API, then call ``_register`` to store ``self`` at
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

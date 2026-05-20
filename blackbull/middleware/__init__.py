"""Public middleware exports.

Names are short nouns by convention — the module path (``blackbull.middleware``)
supplies the "this is middleware" context, so the type names don't need
a redundant suffix.  This matches the project's earliest middlewares
(``CORS``, ``StaticFiles``).

Deprecated aliases for the previous ``*Middleware``-suffixed names and the
``compress`` pre-built instance are kept available through PEP 562
``__getattr__`` so existing user code keeps working with a one-time
``DeprecationWarning``.  They will be removed in a future release.
"""
from .cache import Cache
from .compression import Compression, _make_default_compress
from .cors import CORS
from .proxy import TrustedProxy
from .session import Session
from .static import StaticFiles
from .utils import as_middleware
from .websocket import websocket

__all__ = [
    'Cache',
    'CORS',
    'Compression',
    'Session',
    'StaticFiles',
    'TrustedProxy',
    'as_middleware',
    'websocket',
]


# ---------------------------------------------------------------------------
# Deprecated aliases (PEP 562 — emit on access, not on package import)
# ---------------------------------------------------------------------------

import warnings as _warnings  # noqa: E402

_DEPRECATED: dict[str, tuple[str, object]] = {
    'CacheMiddleware':         ('Cache',         Cache),
    'SessionMiddleware':       ('Session',       Session),
    'CompressionMiddleware':   ('Compression',   Compression),
    'TrustedProxyMiddleware':  ('TrustedProxy',  TrustedProxy),
}


def __getattr__(name):
    if name in _DEPRECATED:
        new_name, target = _DEPRECATED[name]
        _warnings.warn(
            f'{name} is deprecated; use {new_name} instead '
            f'(``from blackbull.middleware import {new_name}``).',
            DeprecationWarning,
            stacklevel=2,
        )
        return target
    if name == 'compress':
        _warnings.warn(
            'The pre-built ``compress`` instance is deprecated; instantiate '
            'Compression explicitly (``from blackbull.middleware import '
            'Compression; app.use(Compression())``).',
            DeprecationWarning,
            stacklevel=2,
        )
        return _make_default_compress()
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')

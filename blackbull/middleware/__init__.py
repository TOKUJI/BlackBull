"""Public middleware exports.

Legacy Middleware-suffixed aliases and compress emit DeprecationWarning;
use the unsuffixed classes for new code.
"""
from .cache import Cache
from .compression import Compression, _make_default_compress
from .cors import CORS
from .proxy import TrustedProxy
from .static import StaticFiles
from .utils import as_middleware
from .websocket import websocket

__all__ = [
    'Cache',
    'CORS',
    'Compression',
    'StaticFiles',
    'TrustedProxy',
    'as_middleware',
    'websocket',
]


# ---------------------------------------------------------------------------
# Deprecated aliases (PEP 562 — emit on access, not on package import)
# ---------------------------------------------------------------------------

from ..deprecations import _warn_deprecated  # noqa: E402

_DEPRECATED: dict[str, tuple[str, object]] = {
    'CacheMiddleware':         ('Cache',         Cache),
    'CompressionMiddleware':   ('Compression',   Compression),
    'TrustedProxyMiddleware':  ('TrustedProxy',  TrustedProxy),
}


def __getattr__(name):
    if name in _DEPRECATED:
        new_name, target = _DEPRECATED[name]
        _warn_deprecated('BBD001', f'blackbull.middleware.{name}', new_name)
        return target
    if name == 'compress':
        _warn_deprecated('BBD002')
        return _make_default_compress()
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')

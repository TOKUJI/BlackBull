"""URL routing for BlackBull.

``Router`` maps ``(path, method, scheme)`` triples to handler chains.  Paths
support exact strings, regex patterns, and ``{name}`` parameter syntax;
``ErrorRouter`` does the same for HTTPStatus codes and exception classes.
``_register_chain`` composes per-route middlewares with ``functools.partial``
so each middleware receives ``call_next`` bound to the next link.

``RouteGroup`` is defined in :mod:`blackbull.app` to avoid a circular
import; this module re-exports it lazily through ``__getattr__``.
"""
from collections import UserDict
from collections.abc import Iterable
from typing import Any, Callable, List, Tuple, Type, Optional
from functools import wraps, partial
from http import HTTPStatus, HTTPMethod
import re
import inspect
from .utils import Scheme, do_nothing
from .logger import get_logger_set

# RouteGroup is defined in app.py to avoid a circular import;
# re-export here so tests can import it from either location.
def __getattr__(name):
    if name == 'RouteGroup':
        from .app import RouteGroup
        return RouteGroup
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")



logger, _ = get_logger_set(__name__)

# Sentinel used when scheme is omitted, matching any scheme at lookup time
class _AnyScheme:
    """Typed sentinel — enables isinstance() narrowing in pyright."""

_ANY_SCHEME = _AnyScheme()


class PathNotRegistered(KeyError):
    """Raised when no registered path matches the requested path."""


class MethodNotApplicable(Exception):
    """Raised when the path exists but the HTTP method is not allowed."""
    def __init__(self, allowed_methods):
        self.allowed_methods = tuple(allowed_methods)
        super().__init__(f"Method not allowed. Allowed: {self.allowed_methods}")

def _middleware_param(fn) -> str | None:
    """Return the middleware-chaining parameter name for *fn*, or None.

    Checks for 'call_next' first (Starlette/FastAPI convention), then falls
    back to 'inner' (legacy BlackBull convention) for backwards compatibility.
    """
    params = inspect.signature(fn).parameters
    if 'call_next' in params:
        return 'call_next'
    if 'inner' in params:
        return 'inner'
    return None


def has_middleware_param(fn) -> bool:
    return _middleware_param(fn) is not None


has_inner = has_middleware_param  # backward-compat alias


def _to_tuple(value: Any) -> tuple:
    """
    Normalise a value into a tuple.
    Strings and other non-iterables are wrapped in a single-element tuple.
    Any other iterable is converted with tuple().
    """
    if isinstance(value, Iterable) and not isinstance(value, str):
        return tuple(value)
    return (value,)


class BaseRouter:
    def __setitem__(self, key, value):
        raise NotImplementedError()

    def __getitem__(self, key):
        raise NotImplementedError()

    def __contains__(self, item):
        raise NotImplementedError()

    def route(self, methods: HTTPMethod | Iterable[HTTPMethod] = [HTTPMethod.GET],
              path: str = '/', scheme: Scheme | Iterable[Scheme] = Scheme.http,
              functions: list = []):
        raise NotImplementedError()



# http://taichino.com/programming/1538
class Router(UserDict, BaseRouter):
    """
    This class has 2 dictionaries: self.data and self.regex_.
    key: str or re.Pattern
    value: (function, methods, scheme)
    """
    f_string = re.compile(r'\{([a-zA-Z_]\w*?)\}', flags=re.ASCII)

    def __init__(self, *args, **kwds):
        super(Router, self).__init__(*args, **kwds)
        self.regex_ = {}

    def __setitem__(
        self,
        key: Tuple[str | re.Pattern,
                   HTTPMethod | Iterable[HTTPMethod],
                   Optional[Scheme | Iterable[Scheme]]],
        value: Any,
    ):
        """
        If key[0] is a str:
            - Store it in self.data under the normalised (path, methods, scheme) key.
            - Also compile a regex from {param} placeholders and store it in self.regex_.
 
        If key[0] is a re.Pattern:
            - Store it in self.regex_ only.
 
        When scheme is omitted it is stored as _ANY_SCHEME,
        which matches any scheme at lookup time.
        """
        # Unpack key
        if len(key) == 3:
            path, methods, scheme = key
        elif len(key) == 2:
            path, methods = key
            scheme = _ANY_SCHEME          # omitted -> match any scheme
        else:
            raise ValueError(
                f"key must be a 2- or 3-element tuple (path, methods) or "
                f"(path, methods, scheme), got: {key!r}"
            )
 
        # Normalise methods / scheme to tuples
        methods = _to_tuple(methods)
        scheme  = _ANY_SCHEME if scheme is None or isinstance(scheme, _AnyScheme) \
                  else _to_tuple(scheme)

        logger.debug("setitem key=%r", key)

        # Dispatch on path type
        if isinstance(path, str):
            # Store under the normalised 3-element key in self.data
            normalized_key = (path, tuple(methods), _ANY_SCHEME if isinstance(scheme, _AnyScheme) else tuple(scheme))
            self.data[normalized_key] = value

            # Expand {param} placeholders into named capture groups
            pattern_str = self.f_string.sub(
                r'(?P<\1>[a-zA-Z0-9_\-\.\~]+)', path
            )
            compiled = re.compile(f'^{pattern_str}$')
            self.regex_[(compiled, tuple(methods), _ANY_SCHEME if isinstance(scheme, _AnyScheme) else tuple(scheme))] = value

        elif isinstance(path, re.Pattern):
            self.regex_[(path, tuple(methods), _ANY_SCHEME if isinstance(scheme, _AnyScheme) else tuple(scheme))] = value
 
        else:
            logger.error(f"Unexpected type for path: {key!r}")
            raise TypeError(f"path must be str or re.Pattern, got: {path!r}")


    def __getitem__(
        self,
        key: Tuple[str, HTTPMethod, Scheme],
    ):
        """
        key: (path: str, method: HTTPMethod, scheme: Scheme)

        1. Exact match against self.data, then pattern match against self.regex_.
           Both passes collect allowed methods for any path+scheme hit regardless
           of method, so we can distinguish:
             - path+scheme found, method allowed  → return the handler
             - path+scheme found, method not in registered set → MethodNotApplicable
             - no path+scheme match at all → PathNotRegistered
        """
        key_path, key_method, key_scheme = key
        logger.debug("getitem key=%r", key)

        allowed_methods: set = set()

        # --- 1. Exact match in self.data ---------------------------------
        for (p, ms, ss), value in self.data.items():
            if p != key_path:
                continue
            if not self._scheme_matches(key_scheme, ss):
                continue
            # Path + scheme match: record allowed methods
            allowed_methods.update(ms)
            if self._method_matches(key_method, ms):
                logger.debug("data hit: value=%r", value)
                return value

        # --- 2. Pattern match in self.regex_ ------------------------------
        for (pattern, ms, ss), fn in self.regex_.items():
            m = pattern.match(key_path)
            if not m:
                continue
            if not self._scheme_matches(key_scheme, ss):
                continue
            # Path + scheme match: record allowed methods
            allowed_methods.update(ms)
            if self._method_matches(key_method, ms):
                logger.debug("regex_ hit: pattern=%r fn=%r", pattern, fn)
                if gdict := m.groupdict():
                    _fn, _params = fn, gdict
                    async def _inject(scope, receive, send,
                                      _fn=_fn, _params=_params):
                        scope.setdefault('path_params', {}).update(_params)
                        return await _fn(scope, receive, send)
                    return _inject
                return fn

        # --- 3. Raise appropriate exception -------------------------------
        logger.debug("No match: key=%r allowed=%r", key, allowed_methods)
        if allowed_methods:
            raise MethodNotApplicable(allowed_methods)
        raise PathNotRegistered(key_path)

    def __contains__(self, item) -> bool:
        """
        Accept either a plain str (path only) or a (path, method, scheme) tuple.
        Search both self.data and self.regex_.
        """
        # Extract only the path when a tuple is given
        if isinstance(item, tuple):
            path = item[0]
        else:
            path = item
 
        # Check for an exact path match in self.data
        for (p, *_) in self.data:
            if p == path:
                return True
 
        # Check whether any pattern in self.regex_ matches
        for (pattern, *_) in self.regex_:
            m = pattern.match(path)
            if m:
                logger.debug("%r matches %r? %r", pattern, path, m)
                return True
 
        return False

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"data={self.data!r}, "
            f"regex_={self.regex_!r})"
        )

    @staticmethod
    def _method_matches(key_method, registered_methods: tuple) -> bool:
        """Return True if key_method matches any entry in registered_methods.

        Accepts both HTTPMethod enum values and plain strings (e.g. 'GET'),
        comparing by normalised uppercase name so the two forms are interchangeable.
        """
        return key_method in registered_methods

    @staticmethod
    def _scheme_matches(key_scheme: Scheme, registered_scheme) -> bool:
        """
        Return True if key_scheme is found in the registered scheme tuple,
        or if registered_scheme is _ANY_SCHEME (omitted at registration time).
        """
        if isinstance(registered_scheme, _AnyScheme):
            return True
        return key_scheme in registered_scheme

    def route_fn(self,
                 methods: HTTPMethod | Iterable[HTTPMethod] = [HTTPMethod.GET],
                 path: str = '/',
                 scheme: Scheme | Iterable[Scheme] = Scheme.http):

        logger.debug('Router.route_fn() is called.')
        methods = _to_tuple(methods)

        if [x for x in methods if not isinstance(x, HTTPMethod)]:
            raise ValueError('methods must be HTTPMethod.')

        def register(fn):
            logger.debug(f'Router.route_fn.register() is called. {fn}')

            @wraps(fn)
            def wrapper(*args, **kwds):
                logger.debug('Router.route_fn.register.wrapper() is called.')
                return fn(*args, **kwds)

            logger.debug((path, methods, scheme))
            self[(path, methods, scheme)] = wrapper

            return wrapper

        return register

    def _register_chain(self, functions, path, methods, scheme):
        """Build a middleware chain from *functions* and register it."""
        if not isinstance(functions, Iterable):
            raise TypeError(f'{functions} is not iterable.')

        fns = list(functions)
        param = _middleware_param(fns[-1])
        if param is not None:
            inner_chain = partial(fns[-1], **{param: do_nothing})
        else:
            inner_chain = fns[-1]

        for fn in fns[-2::-1]:
            param = _middleware_param(fn)
            if param is None:
                raise ValueError(f'{fn} does not have "inner" or "call_next" in its parameters.')
            inner_chain = partial(fn, **{param: inner_chain})

        # Wrap in a named coroutine so it is recognisable at lookup time.
        # Path parameters will be injected into scope['path_params'] by
        # __getitem__ rather than forwarded as kwargs to the outermost
        # middleware (which would raise TypeError for unknown keyword args).
        _ic = inner_chain
        async def _chain_wrapper(scope, receive, send):
            return await _ic(scope, receive, send)

        self[(path, methods, scheme)] = _chain_wrapper

    def route(self, methods: HTTPMethod | Iterable[HTTPMethod] = [HTTPMethod.GET],
              path: str = '/', scheme: Scheme | Iterable[Scheme] = Scheme.http,
              functions: list = [], middlewares: list = []):
        """Register a function or middleware chain in the routing table.

        Three calling conventions:
        1. Decorator with no extra middlewares (``functions`` and ``middlewares``
           both empty) — returns a decorator via ``route_fn``.
        2. ``functions=[...]`` — registers a pre-built chain immediately;
           returns None (same as before).
        3. ``middlewares=[...]`` — returns a decorator; the decorated handler is
           appended to the middleware list before the chain is registered.
        """
        logger.debug('Router.route() is called. functions=%r middlewares=%r', functions, middlewares)

        if functions:
            self._register_chain(list(functions), path, methods, scheme)
            return lambda fn: fn

        if middlewares:
            def decorator(fn):
                self._register_chain(list(middlewares) + [fn], path, methods, scheme)
                return fn
            return decorator

        return self.route_fn(methods, path, scheme)


class ErrorRouter:
    """Maps HTTP error statuses and exception classes to ASGI error-handler functions.

    Keys accepted by __setitem__ / __getitem__:
      - HTTPStatus value  (e.g. HTTPStatus.NOT_FOUND)
      - Exception class   (e.g. ValueError)

    Lookup rules:
      - HTTPStatus key: exact match only.
      - Exception class: walks the MRO so a handler registered for a base class
        (e.g. Exception) catches all unhandled subclasses.
      - Returns None when no handler is found (caller decides the fallback).

    Usage::

        errors = ErrorRouter()

        @errors[HTTPStatus.NOT_FOUND]
        async def handle_404(scope, receive, send):
            ...

        @errors[ValueError]
        async def handle_value_error(scope, receive, send):
            ...

        handler = errors[HTTPStatus.NOT_FOUND]   # → handle_404
        handler = errors[KeyError()]              # → handle_value_error via MRO (if registered)
        handler = errors[KeyError]               # same, accepting the class directly
    """

    def __init__(self):
        self._status_handlers: dict[HTTPStatus, Callable] = {}
        self._exc_handlers: dict[Type[BaseException], Callable] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def __setitem__(self, key: HTTPStatus | Type[BaseException], fn: Callable):
        if isinstance(key, HTTPStatus):
            if not key.is_client_error and not key.is_server_error:
                raise ValueError(f"{key} is not an error status (4xx/5xx).")
            self._status_handlers[key] = fn
        elif isinstance(key, type) and issubclass(key, BaseException):
            self._exc_handlers[key] = fn
        else:
            raise TypeError(
                f"Key must be an HTTPStatus or an exception class, got {key!r}"
            )

    def __call__(self, key: HTTPStatus | Type[BaseException]) -> Callable:
        """Decorator form: @errors[HTTPStatus.NOT_FOUND]"""
        def decorator(fn: Callable) -> Callable:
            self[key] = fn
            return fn
        return decorator

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def __getitem__(
        self, key: HTTPStatus | Type[BaseException] | BaseException
    ) -> Callable | None:
        """Return the registered handler for *key*, or None if not found.

        Accepts:
          - HTTPStatus           → exact match
          - exception class      → MRO walk
          - exception instance   → MRO walk on type(key)
        """
        if isinstance(key, HTTPStatus):
            return self._status_handlers.get(key)

        # Normalise instance → class
        exc_class = key if isinstance(key, type) else type(key)
        if not issubclass(exc_class, BaseException):
            raise TypeError(f"Key must be HTTPStatus or exception class/instance, got {key!r}")

        for cls in exc_class.__mro__:
            if cls in self._exc_handlers:
                return self._exc_handlers[cls]
        return None

    def __contains__(self, key: HTTPStatus | Type[BaseException] | BaseException) -> bool:
        return self[key] is not None

"""URL routing for BlackBull.

``Router`` maps ``(path, method, scheme)`` triples to handler chains.  Paths
support exact strings, regex patterns, and ``{name}`` / ``{name:converter}``
parameter syntax; ``ErrorRouter`` does the same for HTTPStatus codes and
exception classes.  ``_register_chain`` composes per-route middlewares with
``functools.partial`` so each middleware receives ``call_next`` bound to the
next link.

``RouteGroup`` is defined in :mod:`blackbull.app` to avoid a circular
import; this module re-exports it lazily through ``__getattr__``.
"""
from collections import OrderedDict
from collections.abc import Iterable
import dataclasses
from dataclasses import dataclass, field
import typing
from typing import Any, Callable, NamedTuple, Tuple, Type, Optional, Union, get_args, get_origin
from functools import wraps, partial
from http import HTTPStatus, HTTPMethod
import json
import re
import inspect
import types
import uuid as _uuid
from .utils import Scheme, do_nothing

# RouteGroup is defined in app.py to avoid a circular import;
# re-export here so tests can import it from either location.
def __getattr__(name):
    if name == 'RouteGroup':
        from .app import RouteGroup
        return RouteGroup
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")



import logging
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Converter table
# ---------------------------------------------------------------------------

_CONVERTERS: dict[str, tuple[str, type]] = {
    'str':  (r'[^/]+',                                                                 str),
    'int':  (r'-?[0-9]+',                                                              int),
    'uuid': (r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',       _uuid.UUID),
    'path': (r'.+',                                                                    str),
}

_SAMPLE_INPUTS: dict[str, str] = {
    'str':  'test',
    'int':  '42',
    'uuid': '00000000-0000-0000-0000-000000000000',
    'path': 'a/b/c',
}

# Characters that signal regex intent in a string path (after removing
# {param} placeholders).  '.' is deliberately absent — it is common in
# literal paths ('/openapi.json') and harmless.
_REGEX_METACHARS = frozenset('^$*+?()[]|\\')


# ---------------------------------------------------------------------------
# Module-level route-matching helpers (reused by Router and _RouteTrie)
# ---------------------------------------------------------------------------

def _route_scheme_matches(key_scheme: 'Scheme', registered_scheme) -> bool:
    if isinstance(registered_scheme, _AnyScheme):
        return True
    return key_scheme in registered_scheme


def _route_method_matches(key_method, registered_methods: tuple) -> bool:
    return key_method in registered_methods


# ---------------------------------------------------------------------------
# Routing trie — O(path-depth) lookup for string-path routes
# ---------------------------------------------------------------------------

@dataclass
class _TrieNode:
    children: dict[str, '_TrieNode'] = field(default_factory=dict)
    param_child: Optional['_TrieNode'] = None   # matches any single segment
    wildcard_child: Optional['_TrieNode'] = None  # matches rest of path ({name:path})
    # list of (methods_tuple, scheme_key, handler, param_specs)
    # param_specs: tuple of (param_name: str, converter: Callable, is_wildcard: bool)
    entries: list = field(default_factory=list)


class _RouteTrie:
    """Radix trie for O(path-depth) string-path route lookup."""

    def __init__(self) -> None:
        self.root = _TrieNode()

    def insert(self, path: str, methods: tuple, scheme_key: Any, handler: Any) -> None:
        segments = [s for s in path.split('/') if s]
        node = self.root
        param_specs: list[tuple] = []

        for i, seg in enumerate(segments):
            if seg.startswith('{') and seg.endswith('}'):
                inner = seg[1:-1]
                name, _, spec = inner.partition(':')
                spec = spec or 'str'
                _, converter = _CONVERTERS.get(spec, (None, str))
                if spec == 'path':
                    # {name:path} consumes all remaining segments, so it must be
                    # the final segment.  A route like ``/a/{p:path}/b`` used to
                    # silently register as ``/a/{p:path}`` — dropping ``/b`` — so
                    # reject it at registration (bug 1.13).
                    if i != len(segments) - 1:
                        raise ConfigurationError(
                            f"path converter {seg!r} must be the last segment of "
                            f"route {path!r}: a '{{name:path}}' wildcard consumes "
                            f"all remaining segments, so nothing may follow it."
                        )
                    # {name:path} consumes all remaining segments
                    param_specs.append((name, converter, True))
                    if node.wildcard_child is None:
                        node.wildcard_child = _TrieNode()
                    node.wildcard_child.entries.append(
                        (methods, scheme_key, handler, tuple(param_specs))
                    )
                    return
                param_specs.append((name, converter, False))
                if node.param_child is None:
                    node.param_child = _TrieNode()
                node = node.param_child
            else:
                if seg not in node.children:
                    node.children[seg] = _TrieNode()
                node = node.children[seg]

        node.entries.append((methods, scheme_key, handler, tuple(param_specs)))

    def lookup(self, path: str, method: Any, scheme: Any) -> tuple:
        """Return (handler, params, allowed_methods).

        handler is None on miss; allowed_methods is non-empty when the path
        matched but the method was not registered (enables MethodNotApplicable).
        """
        segments = [s for s in path.split('/') if s]
        return self._lookup(self.root, segments, 0, [], method, scheme)

    def _lookup(self, node: _TrieNode, segments: list, idx: int,
                raw_caps: list, method: Any, scheme: Any) -> tuple:
        if idx == len(segments):
            allowed: set = set()
            for (ms, ss, handler, param_specs) in node.entries:
                if not _route_scheme_matches(scheme, ss):
                    continue
                try:
                    params = {name: conv(raw_caps[j])
                              for j, (name, conv, _) in enumerate(param_specs)}
                except (ValueError, TypeError):
                    continue  # converter rejected this segment value
                allowed.update(ms)
                if _route_method_matches(method, ms):
                    return (handler, params, set())
            return (None, {}, allowed)

        seg = segments[idx]
        allowed_all: set = set()

        # 1. Static child (most specific — checked first)
        if seg in node.children:
            h, p, a = self._lookup(
                node.children[seg], segments, idx + 1, raw_caps, method, scheme
            )
            if h is not None:
                return (h, p, set())
            allowed_all.update(a)

        # 2. Param child (single-segment wildcard)
        if node.param_child is not None:
            h, p, a = self._lookup(
                node.param_child, segments, idx + 1, raw_caps + [seg], method, scheme
            )
            if h is not None:
                return (h, p, set())
            allowed_all.update(a)

        # 3. Wildcard child ({name:path} — matches rest of path)
        if node.wildcard_child is not None:
            rest = '/'.join(segments[idx:])
            h, p, a = self._lookup(
                node.wildcard_child, segments, len(segments), raw_caps + [rest], method, scheme
            )
            if h is not None:
                return (h, p, set())
            allowed_all.update(a)

        return (None, {}, allowed_all)


class ConfigurationError(Exception):
    """Raised by Router.validate() when route definitions are inconsistent."""


class HTTPException(Exception):
    """An exception carrying the HTTP status the dispatcher should report.

    Raising this from a handler — or from the framework's own request-body
    adapter — makes ``BlackBull._dispatch`` answer with ``status`` instead of
    the generic 500, and (for 4xx) log it quietly as a client error rather
    than dumping a server traceback.  ``detail`` is an optional
    human-readable message surfaced in development-mode error pages.
    """

    def __init__(self, status: HTTPStatus, detail: str = ''):
        super().__init__(detail or f'{int(status)} {status.phrase}')
        self.status = status
        self.detail = detail


@dataclass
class _RouteInfo:
    template: str
    handler: Callable
    param_specs: dict[str, str]    # {param_name: converter_spec}
    methods: tuple = ()            # tuple of HTTPMethod values
    scheme: Any = None             # Scheme | tuple[Scheme, ...] | _AnyScheme | None
    name: str | None = None
    # {param_name} written with an explicit ``:converter`` in the template
    # (e.g. {task_id:int}), as opposed to defaulted to 'str'. validate()'s
    # converter/annotation type-match check only applies to these: a bare
    # {task_id} is matched as 'str' by the router but re-coerced to the
    # handler's own annotation at call time by _adapt_handler, so the
    # router-level 'str' spec and the handler annotation are expected to
    # differ and that's not an error.
    explicit_param_specs: frozenset = field(default_factory=frozenset)


class RouteInfo(NamedTuple):
    """Immutable snapshot of a single registered route entry.

    Returned by :meth:`BlackBull.get_routes`.  One entry is produced per
    ``(route, method)`` pair — a route registered with
    ``methods=[GET, POST]`` yields two ``RouteInfo`` records.

    Attributes:
        method: HTTP method string (e.g. ``"GET"``, ``"BREW"``).
        path: URL template (e.g. ``"/api/echo/{name}"``).
        name: Endpoint name, or ``""`` if the route was registered unnamed.
    """
    method: str
    path: str
    name: str = ""


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


_ASGI_PARAMS = frozenset({'scope', 'receive', 'send'})


def _is_simplified_handler(fn) -> bool:
    """Return True if *fn* lacks the full ASGI (scope, receive, send) signature.

    Middlewares (call_next/inner) and variadic handlers (*args/**kwargs) are
    never considered simplified — they can already accept any arguments.
    """
    if _middleware_param(fn) is not None:
        return False
    params = inspect.signature(fn).parameters
    # *args / **kwargs handlers are variadic — leave them untouched
    for p in params.values():
        if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            return False
    return not _ASGI_PARAMS.issubset(params)


def _to_jsonable(value: Any) -> Any:
    """Recursively turn dataclass instances into dicts so ``json.dumps`` works.

    Lists and dicts of dataclasses are also walked.  Anything that's already
    JSON-friendly is returned unchanged.
    """
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, tuple):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    return value


def _is_body_dataclass_annotation(ann: Any) -> bool:
    """``True`` when *ann* is a Python dataclass (the deserialization target)."""
    return ann is not inspect.Parameter.empty and dataclasses.is_dataclass(ann)


def _coerce_value(ann: Any, value: Any) -> Any:
    """Recursively coerce JSON-parsed *value* to match the Python annotation *ann*.

    Handles dataclasses (constructed from dicts), ``list[T]`` / ``tuple[T, ...]``
    (each element coerced), ``T | None`` (None passes through, otherwise coerced
    to the non-None branch), and primitives (passed through verbatim — JSON's
    own types align with Python's).

    Anything not recognised is passed through; the caller may rely on the
    dataclass's ``__init__`` to validate further.
    """
    if value is None:
        return None
    if dataclasses.is_dataclass(ann):
        if not isinstance(value, dict):
            raise TypeError(
                f"Expected JSON object for {ann.__name__!r}, got "
                f"{type(value).__name__}")
        return _instantiate_dataclass(ann, value)

    origin = get_origin(ann)
    args = get_args(ann)

    # T | None / Optional[T]
    if origin is Union or origin is types.UnionType:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _coerce_value(non_none[0], value)
        # Multiple non-None branches: try each in order, keep the first
        # that constructs cleanly.  Dataclass branches are tried first so
        # that ``Cat | Dog`` doesn't get short-circuited by a primitive.
        ordered = sorted(non_none, key=lambda a: 0 if dataclasses.is_dataclass(a) else 1)
        for branch in ordered:
            try:
                return _coerce_value(branch, value)
            except (TypeError, ValueError):
                continue
        raise TypeError(
            f"Value {value!r} matches no branch of union {ann!r}")

    if origin is list or origin is tuple:
        # ``origin`` is itself the constructor (``list`` / ``tuple``); both
        # accept an iterable of the element-coerced values.
        item_t = args[0] if args else None
        if not isinstance(value, list):
            kind = 'array' if origin is list else 'array for tuple'
            raise TypeError(
                f"Expected JSON {kind}, got {type(value).__name__}")
        return origin(_coerce_value(item_t, v) if item_t is not None else v
                      for v in value)

    if origin is dict:
        v_t = args[1] if len(args) == 2 else None
        if not isinstance(value, dict):
            raise TypeError(
                f"Expected JSON object, got {type(value).__name__}")
        return {k: (_coerce_value(v_t, v) if v_t is not None else v)
                for k, v in value.items()}

    return value


def _instantiate_dataclass(cls: Any, data: dict) -> Any:
    """Construct *cls* (a dataclass) from a JSON-parsed ``dict``.

    Resolves forward references via ``typing.get_type_hints`` and recurses
    into dataclass / generic-container fields via :func:`_coerce_value`.
    Unknown keys raise ``TypeError`` rather than being silently dropped —
    if a client sends ``{"titel": ...}`` we want them to know.
    """
    try:
        hints = typing.get_type_hints(cls)
    except Exception:
        hints = {}

    field_names = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - field_names
    if unknown:
        raise TypeError(
            f"Unknown field(s) for {cls.__name__}: {sorted(unknown)}")

    kwargs: dict = {}
    for f in dataclasses.fields(cls):
        if f.name not in data:
            continue
        annotation = hints.get(f.name, f.type)
        kwargs[f.name] = _coerce_value(annotation, data[f.name])
    return cls(**kwargs)


def _decode_json_body(cls: Any, raw: bytes, handler_name: str) -> Any:
    """Parse *raw* JSON and instantiate the dataclass *cls*, mapping any
    client-input error to a 400 (bug 1.12).

    Malformed JSON, a wrong top-level shape, unknown/missing fields, or a
    field that fails type coercion are all *client* errors — they must surface
    as ``400 Bad Request``, not a generic ``500``.  ``_instantiate_dataclass``
    signals these with ``JSONDecodeError`` / ``TypeError`` / ``ValueError``,
    which this wrapper re-raises as :class:`HTTPException`.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            HTTPStatus.BAD_REQUEST,
            f'malformed JSON body for handler {handler_name!r}: {exc}',
        ) from exc
    try:
        return _instantiate_dataclass(cls, data)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            HTTPStatus.BAD_REQUEST,
            f'request body does not match {getattr(cls, "__name__", cls)!r}: {exc}',
        ) from exc


def _lookup_converter(converters: dict, result_type: type):
    """Find a registered converter for *result_type* (exact match, then MRO).

    Only reached on the cold path — after a handler returns a value that is
    none of the natively supported shapes — so the MRO walk costs nothing on
    the common path.  Returns the converter callable or ``None``.
    """
    fn = converters.get(result_type)
    if fn is not None:
        return fn
    for base in result_type.__mro__[1:]:
        fn = converters.get(base)
        if fn is not None:
            return fn
    return None


async def _send_native(result, scope, receive, send) -> bool:
    """Serialise a *natively supported* handler/converter return value to ASGI.

    The single source of truth for return-value → ASGI mapping, shared by the
    simplified-handler wrapper and the converter path.  Handles the shapes
    BlackBull sends without an app-registered converter:

    * ``None`` — send nothing;
    * an existing ``StreamingResponse`` / ``EventSourceResponse`` instance, or a
      bare async generator — driven directly so it owns its own start event;
    * a ``Response`` (sent as-is);
    * ``bytes`` / ``str`` (wrapped in a default ``Response``);
    * a JSON-able ``dict`` / ``list`` / dataclass instance (``JSONResponse``).

    Returns ``True`` when *result* matched one of those shapes (and has been
    sent), ``False`` otherwise — leaving the caller to apply its own fallback
    (the handler wrapper tries a registered converter, then raises;
    ``_send_converted`` raises, since a converter must itself return a native
    shape).
    """
    from .response import (
        Response as _Response, JSONResponse as _JSONResponse,
        StreamingResponse as _StreamingResponse,
    )
    if result is None:
        return True
    if isinstance(result, _StreamingResponse):
        # Existing StreamingResponse / EventSourceResponse instance — let it
        # drive scope/receive/send directly so subclasses keep control over
        # the start event.
        await result(scope, receive, send)
    elif inspect.isasyncgen(result):
        # ``async def stream(): yield ...`` shape — wrap in a default-typed
        # StreamingResponse.  Backpressure flows naturally because each
        # ``await send()`` on a body event blocks on flow-control credit
        # (HTTP/2) or drain (HTTP/1).
        await _StreamingResponse(result)(scope, receive, send)
    elif isinstance(result, _Response):
        await send(result)
    elif isinstance(result, bytes):
        await send(_Response(result))
    elif isinstance(result, str):
        await send(_Response(result.encode()))
    elif dataclasses.is_dataclass(result) and not isinstance(result, type):
        # Dataclass instance → recurse so nested dataclasses serialise too.
        await send(_JSONResponse(_to_jsonable(result)))
    elif isinstance(result, (dict, list)):
        await send(_JSONResponse(_to_jsonable(result)))
    else:
        return False
    return True


async def _send_converted(value, scope, receive, send) -> None:
    """Serialise a converter's output and send it.

    A converter must return a *natively supported* sendable (see
    :func:`_send_native`).  Anything else raises here — the intended loud
    failure — rather than silently dropping the response.
    """
    if await _send_native(value, scope, receive, send):
        return
    raise TypeError(
        'a registered converter must return a Response, StreamingResponse, '
        f'bytes, str, dict, list, dataclass, or None; got {type(value).__name__!r}')


def _adapt_handler(fn, path: str, converters: dict | None = None):
    """Wrap a simplified handler in an ASGI (scope, receive, send) coroutine.

    *converters* is the app's ``type → callable`` registry (shared by
    reference so converters registered after this route are still visible).
    When a handler returns a value that is none of the natively supported
    shapes, a matching converter — if any — maps it to a sendable.

    Parameter resolution:
    - Name matches a {param} in the path pattern → scope['path_params'][name],
      coerced to the annotated type if one is given.
    - Annotation is a Python ``@dataclass`` → request body parsed as JSON and
      instantiated; nested dataclasses, ``list[T]``, and ``T | None`` are
      handled recursively.  ``body: SomeDataclass`` also works.
    - 'body' (un-annotated, or annotated as ``bytes``) → await read_body(receive)
    - 'scope' → the raw scope dict
    - Annotation is ``Request`` (any name), or the name is ``request`` with no
      annotation → a per-request ``Request(scope, receive)`` context object.
      Its ``body()`` cache is the drain point for 'body' / dataclass params
      too, so the body is read at most once per request.
    - Anything else → TypeError raised at registration time (fail fast).

    Return values: Response → send(result); bytes → send(Response(result));
    str → send(Response(result.encode())); dict → send(JSONResponse(result));
    None → no send; other → TypeError at call time.
    """
    from .request import Request as _Request, read_body as _read_body

    path_param_names: set[str] = {m.group(1) for m in Router._param_pattern.finditer(path)}
    params = inspect.signature(fn).parameters
    annotations = {n: p.annotation for n, p in params.items()}

    # Resolve PEP 563 / string-form annotations once, here, so the wrapper
    # below can rely on real types in its isinstance / get_origin calls.
    try:
        resolved_hints = typing.get_type_hints(fn)
        for n in annotations:
            if n in resolved_hints:
                annotations[n] = resolved_hints[n]
    except Exception:
        pass  # unresolved forward refs / bad annotations → keep the raw annotations.

    # Request-param recognition happens here, at registration time, so the
    # wrapper below never reflects per request.  Path params keep precedence;
    # a dataclass annotation on a param named 'request' stays a body param.
    request_param_names: frozenset[str] = frozenset(
        n for n, ann in annotations.items()
        if n not in path_param_names and n not in ('body', 'scope') and (
            ann is _Request
            or (n == 'request' and ann is inspect.Parameter.empty)
        )
    )

    # A handler may have **at most one** body parameter — either a literal
    # name ``body`` or a dataclass-typed parameter (or both, if they're the
    # same parameter).  Trying to consume the body twice would hang the
    # second ``read_body`` call indefinitely.  A ``Request`` param does not
    # count: it drains lazily through the same cache the branches below use.
    body_param_count = sum(
        1 for n, p in params.items()
        if n not in path_param_names and n != 'scope' and (
            n == 'body' or _is_body_dataclass_annotation(annotations.get(n))
        )
    )

    for name in params:
        if name in path_param_names or name in ('body', 'scope'):
            continue
        if name in request_param_names:
            continue
        if _is_body_dataclass_annotation(annotations.get(name)):
            continue
        raise TypeError(
            f"Simplified handler {fn.__name__!r}: cannot resolve parameter {name!r}. "
            f"Expected path params {sorted(path_param_names)!r}, 'body', "
            f"'scope', a parameter annotated with Request (or named "
            f"'request'), or a parameter annotated with a dataclass."
        )

    if body_param_count > 1:
        raise TypeError(
            f"Simplified handler {fn.__name__!r}: more than one parameter would "
            f"consume the request body.  Pick one of 'body' or a dataclass-typed "
            f"parameter, not both."
        )

    is_async = inspect.iscoroutinefunction(fn)
    has_request_param = bool(request_param_names)

    @wraps(fn)
    async def _wrapper(scope, receive, send):
        # One Request per call when the signature asks for it; its body()
        # cache is the single drain point shared with the body branches.
        req = _Request(scope, receive) if has_request_param else None
        kwargs: dict = {}
        for name in params:
            ann = annotations.get(name, inspect.Parameter.empty)
            if name == 'scope':
                kwargs[name] = scope
            elif name == 'body':
                raw = await req.body() if req is not None else await _read_body(receive)
                if _is_body_dataclass_annotation(ann):
                    kwargs[name] = _decode_json_body(ann, raw, fn.__name__)
                else:
                    kwargs[name] = raw
            elif name in request_param_names:
                kwargs[name] = req
            elif _is_body_dataclass_annotation(ann) and name not in path_param_names:
                raw = await req.body() if req is not None else await _read_body(receive)
                kwargs[name] = _decode_json_body(ann, raw, fn.__name__)
            else:
                raw = scope.get('path_params', {}).get(name, '')
                if (ann is not inspect.Parameter.empty and isinstance(ann, type)
                        and not isinstance(raw, ann)):
                    try:
                        kwargs[name] = ann(raw)
                    except (ValueError, TypeError) as exc:
                        raise TypeError(
                            f"Path param {name!r}: cannot coerce {raw!r} to {ann.__name__}"
                        ) from exc
                else:
                    kwargs[name] = raw

        result = (await fn(**kwargs)) if is_async else fn(**kwargs)

        if await _send_native(result, scope, receive, send):
            return
        if converters and (conv := _lookup_converter(converters, type(result))) is not None:
            # Cold path: an app-registered type→sendable converter.  Guarded by
            # ``converters`` truthiness so an empty registry costs nothing here
            # and the common shapes above never reach this branch at all.
            await _send_converted(conv(result), scope, receive, send)
            return
        raise TypeError(
            f"Simplified handler {fn.__name__!r} returned unsupported type "
            f"{type(result).__name__!r}.  Return a Response, str, bytes, "
            f"dict, list, or None, or register a converter with "
            f"app.register_converter({type(result).__name__}, ...)."
        )

    return _wrapper


# RFC 9110 §5.6.2: token = 1*tchar (visible US-ASCII, no separators)
_HTTP_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def _validate_method_token(method: str) -> None:
    if not _HTTP_TOKEN_RE.match(method):
        raise ValueError(
            f"Invalid HTTP method token {method!r}: RFC 9110 §5.6.2 requires "
            "a non-empty sequence of visible ASCII tchar characters."
        )


def _to_tuple(value: Any) -> tuple:
    """
    Normalise a value into a tuple.
    Strings and other non-iterables are wrapped in a single-element tuple.
    Any other iterable is converted with tuple().
    """
    if isinstance(value, Iterable) and not isinstance(value, str):
        return tuple(value)
    return (value,)


# http://taichino.com/programming/1538
class Router:
    """
    String paths live in the routing trie (sole store, including
    ``{param}`` segments); raw ``re.Pattern`` routes live in
    ``self._raw_regex`` and are scanned only on a trie miss.
    """
    f_string = re.compile(r'\{([a-zA-Z_]\w*?)\}', flags=re.ASCII)
    _param_pattern = re.compile(r'\{([a-zA-Z_]\w*?)(?::([a-zA-Z_]\w*?))?\}', flags=re.ASCII)

    # Default bound for the per-worker lookup cache (overridable per instance
    # via the ``cache_max`` constructor argument).
    _DEFAULT_CACHE_MAX: int = 2048

    def __init__(self, cache_max: int = _DEFAULT_CACHE_MAX):
        self._route_info: list[_RouteInfo] = []
        self._named_routes: dict[str, tuple[str, dict[str, str]]] = {}
        self._frozen: bool = False
        self._trie = _RouteTrie()
        self._string_paths: set[str] = set()  # registered string paths (for __contains__/__repr__)
        self._raw_regex: dict = {}  # raw re.Pattern routes (not compiled from string paths)
        # Per-worker lookup cache: maps (path, method, scheme) → resolved
        # handler; cleared whenever a route is registered.  ``cache_max`` bounds
        # it (0 disables caching entirely).  The get/set mechanics live in
        # ``_cache_get`` / ``_cache_set`` so a future cache strategy can be
        # swapped in without touching ``__getitem__`` or ``_resolve``.
        self.cache_max: int = cache_max
        self._lookup_cache: OrderedDict = OrderedDict()
        # type → callable registry for simplified-handler return coercion.
        # Empty by default (falsy) so the common return paths never consult it.
        # Shared by reference with every adapted handler, so a converter
        # registered after a route is still honoured.
        self._converters: dict[type, Callable] = {}

    def register_converter(self, type_: type, converter: Callable) -> None:
        """Register *converter* to turn a handler that returns *type_* into a
        sendable (a Response, str/bytes, or JSON-able)."""
        self._converters[type_] = converter

    def __setitem__(
        self,
        key: (Tuple[str | re.Pattern, str | HTTPMethod | Iterable[str | HTTPMethod]]
              | Tuple[str | re.Pattern, str | HTTPMethod | Iterable[str | HTTPMethod],
                      Scheme | Iterable[Scheme] | None]),
        value: Any,
    ):
        """
        If key[0] is a str:
            - Insert it into the routing trie under the normalised
              (path, methods, scheme) key.  ``{param}`` / ``{param:converter}``
              placeholders become parameter segments with converter functions.

        If key[0] is a re.Pattern:
            - Store it in self._raw_regex (scanned on trie miss).

        When scheme is omitted it is stored as _ANY_SCHEME,
        which matches any scheme at lookup time.
        """
        if self._frozen:
            raise RuntimeError(
                "Router is frozen — routes cannot be added after startup validation")

        self._lookup_cache.clear()

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
        for m in methods:
            if isinstance(m, str):
                _validate_method_token(m)
        scheme  = _ANY_SCHEME if scheme is None or isinstance(scheme, _AnyScheme) \
                  else _to_tuple(scheme)

        logger.debug("setitem key=%r", key)

        scheme_key = _ANY_SCHEME if isinstance(scheme, _AnyScheme) else tuple(scheme)

        # Dispatch on path type
        if isinstance(path, str):
            # Validate converter specs up front — the trie itself defaults an
            # unknown spec to str, which would silently mis-register the route.
            for m in self._param_pattern.finditer(path):
                spec = m.group(2) or 'str'
                if spec not in _CONVERTERS:
                    raise ValueError(
                        f"Unknown converter {spec!r} in path {path!r}. "
                        f"Valid converters: {sorted(_CONVERTERS)}")

            # String paths are matched literally (plus {param} placeholders).
            # Before the trie became the sole string-path store, every string
            # was also compiled as a regex, so a regex source string happened
            # to work — reject it loudly rather than 404 silently.
            stripped = self._param_pattern.sub('', path)
            if any(c in _REGEX_METACHARS for c in stripped):
                raise ValueError(
                    f"Path {path!r} contains regex metacharacters; string "
                    f"paths are matched literally (with {{param}} "
                    f"placeholders). Pass a compiled re.Pattern for custom "
                    f"regex routes.")

            self._trie.insert(path, tuple(methods), scheme_key, value)
            self._string_paths.add(path)

        elif isinstance(path, re.Pattern):
            self._raw_regex[(path, tuple(methods), scheme_key)] = value

        else:
            logger.error(f"Unexpected type for path: {key!r}")
            raise TypeError(f"path must be str or re.Pattern, got: {path!r}")


    def __getitem__(
        self,
        key: Tuple[str, str | HTTPMethod, Scheme],
    ):
        """
        key: (path: str, method: str | HTTPMethod, scheme: Scheme)

        Uses the routing trie for O(path-depth) lookup of string-path routes,
        then falls back to a linear scan of raw re.Pattern routes.

        Results are cached (up to ``cache_max`` entries) so repeated requests
        to the same (path, method, scheme) skip the trie traversal entirely
        after the first hit.  The query→miss→resolve→store flow lives here;
        the cache mechanics are delegated to ``_cache_get`` / ``_cache_set``
        so the cache strategy can be swapped without touching this method.
        """
        hit, result = self._cache_get(key)
        if hit:
            return result

        result = self._resolve(key)
        self._cache_set(key, result)
        return result

    def _cache_get(self, key: Tuple[str, str | HTTPMethod, Scheme]):
        """Return ``(hit: bool, result)`` for *key*.  On a hit, refreshes LRU
        order only when the cache is full (so ordering work is skipped until
        eviction is actually imminent)."""
        cache = self._lookup_cache
        if key in cache:
            if len(cache) >= self.cache_max:
                cache.move_to_end(key)
            return True, cache[key]
        return False, None

    def _cache_set(self, key: Tuple[str, str | HTTPMethod, Scheme], result) -> None:
        """Store *result* under *key*, evicting the least-recently-used entry
        when the cache is full.  A ``cache_max`` of 0 disables caching."""
        if self.cache_max <= 0:
            return
        cache = self._lookup_cache
        if len(cache) >= self.cache_max:
            cache.popitem(last=False)  # evict least-recently-used
        cache[key] = result

    def _resolve(
        self,
        key: Tuple[str, str | HTTPMethod, Scheme],
    ):
        """Core route lookup — trie first, regex fallback.  Raises on miss."""
        key_path, key_method, key_scheme = key
        logger.debug("getitem key=%r", key)

        # --- 1. Trie lookup (all string-path routes) ----------------------
        h, params, trie_allowed = self._trie.lookup(key_path, key_method, key_scheme)
        if h is not None:
            logger.debug("trie hit: handler=%r params=%r", h, params)
            if params:
                _fn, _params = h, params
                async def _inject(scope, receive, send,
                                  _fn=_fn, _params=_params):
                    scope.setdefault('path_params', {}).update(_params)
                    return await _fn(scope, receive, send)
                return _inject
            return h

        # --- 2. Regex scan (fallback: raw re.Pattern routes only) ----------
        allowed_methods: set = set(trie_allowed)
        for (pattern, ms, ss), fn in self._raw_regex.items():
            m = pattern.match(key_path)
            if not m:
                continue
            if not self._scheme_matches(key_scheme, ss):
                continue
            allowed_methods.update(ms)
            if self._method_matches(key_method, ms):
                logger.debug("raw-regex hit: pattern=%r fn=%r", pattern, fn)
                if gdict := m.groupdict():
                    _fn, _params = fn, gdict
                    async def _inject(scope, receive, send,
                                      _fn=_fn, _params=_params):
                        scope.setdefault('path_params', {}).update(_params)
                        return await _fn(scope, receive, send)
                    return _inject
                return fn

        # --- 3. Raise appropriate exception --------------------------------
        logger.debug("No match: key=%r allowed=%r", key, allowed_methods)
        if allowed_methods:
            raise MethodNotApplicable(allowed_methods)
        raise PathNotRegistered(key_path)

    def __contains__(self, item) -> bool:
        """
        Accept either a plain str (path only) or a (path, method, scheme) tuple.
        True when the path was registered verbatim as a string route, or when
        any raw re.Pattern route matches it.
        """
        # Extract only the path when a tuple is given
        if isinstance(item, tuple):
            path = item[0]
        else:
            path = item

        if path in self._string_paths:
            return True

        # Check whether any raw re.Pattern route matches
        for (pattern, *_) in self._raw_regex:
            m = pattern.match(path)
            if m:
                logger.debug("%r matches %r? %r", pattern, path, m)
                return True

        return False

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"paths={sorted(self._string_paths)!r}, "
            f"raw_regex={self._raw_regex!r})"
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
                 methods: str | HTTPMethod | Iterable[str | HTTPMethod] = [HTTPMethod.GET],
                 path: str | re.Pattern = '/',
                 scheme: Scheme | Iterable[Scheme] = Scheme.http,
                 name: str | None = None):

        logger.debug('Router.route_fn() is called.')
        methods = _to_tuple(methods)
        for m in methods:
            if isinstance(m, str):
                _validate_method_token(m)

        def register(fn):
            logger.debug(f'Router.route_fn.register() is called. {fn}')

            original = fn
            if _is_simplified_handler(fn):
                fn = _adapt_handler(fn, path, self._converters)

            logger.debug((path, methods, scheme))
            self[(path, methods, scheme)] = fn

            self._record_route(path, original, name,
                               methods=_to_tuple(methods), scheme=scheme)
            return fn

        return register

    def _register_chain(self, functions, path, methods, scheme,
                        name: str | None = None, original_handler=None):
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
        self._record_route(path, original_handler or fns[-1], name,
                           methods=_to_tuple(methods), scheme=scheme)

    def _record_route(self, path: 'str | re.Pattern', handler: Callable,
                      name: str | None,
                      methods: tuple = (), scheme: Any = None) -> None:
        """Store route metadata for validation, url_path_for, and OpenAPI.

        *path* may be a raw ``re.Pattern`` route (the documented custom-regex
        form): its source string becomes the template and it has no
        ``{param}`` specs — path params come from the pattern's named groups
        at match time.
        """
        if isinstance(path, re.Pattern):
            template, param_specs, explicit_param_specs = path.pattern, {}, frozenset()
        else:
            template = path
            matches = list(self._param_pattern.finditer(path))
            param_specs = {m.group(1): (m.group(2) or 'str') for m in matches}
            explicit_param_specs = frozenset(m.group(1) for m in matches if m.group(2))
        if name is not None:
            if name in self._named_routes:
                raise ValueError(f"Duplicate route name {name!r}")
            self._named_routes[name] = (template, param_specs)
        self._route_info.append(_RouteInfo(
            template=template,
            handler=handler,
            param_specs=param_specs,
            methods=methods,
            scheme=scheme,
            name=name,
            explicit_param_specs=explicit_param_specs,
        ))

    def url_path_for(self, name: str, /, **params) -> str:
        """Return the path for the named route with *params* substituted.

        Raises KeyError if the name is unknown, ValueError if required params
        are missing.
        """
        if name not in self._named_routes:
            raise KeyError(f"No route named {name!r}")
        template, specs = self._named_routes[name]
        missing = set(specs) - set(params)
        if missing:
            raise ValueError(f"url_path_for({name!r}): missing params {sorted(missing)}")
        return self._param_pattern.sub(lambda m: str(params[m.group(1)]), template)

    def get_routes(self) -> list[RouteInfo]:
        """Return a snapshot of all registered routes as :class:`RouteInfo`.

        Routes are returned in registration order.  A route registered with
        multiple methods (e.g. ``methods=[GET, POST]``) produces one entry
        per method, in the order the methods were declared.  The returned
        list is a fresh shallow copy — callers may sort, filter, or mutate
        it without affecting the live router.
        """
        routes: list[RouteInfo] = []
        for info in self._route_info:
            name = info.name or ""
            for method in info.methods:
                routes.append(RouteInfo(
                    method=method.value if isinstance(method, HTTPMethod) else str(method),
                    path=info.template,
                    name=name,
                ))
        return routes

    def validate(self) -> None:
        """Check all route definitions for consistency, then freeze the router.

        Checks performed:

        - Every converter spec names a known converter.
        - Every path param appears in the handler signature (simplified handlers).
        - Converter output type matches the handler's annotation.

        Raises :class:`ConfigurationError` listing all violations found.
        Sets ``self._frozen = True`` on success so no further routes can
        be added.  Called once at app boot from :meth:`BlackBull.run` /
        :meth:`BlackBull.serve` — handler bugs that violate the contract
        surface before the first request is served, not after.
        """
        from beartype.door import die_if_unbearable
        from beartype.roar import BeartypeDoorHintViolation, BeartypeException

        errors: list[str] = []

        for info in self._route_info:
            for param_name, spec in info.param_specs.items():
                if spec not in _CONVERTERS:
                    errors.append(
                        f"Route {info.template!r}: unknown converter {spec!r} "
                        f"for {{{param_name}}}")
                    continue

                try:
                    sig = inspect.signature(info.handler)
                except (ValueError, TypeError):
                    continue

                p = sig.parameters.get(param_name)
                if p is None:
                    continue

                # Resolve string annotations (forward refs / PEP 563) to real
                # types before handing to beartype.  inspect.Parameter.annotation
                # returns the raw string when __future__.annotations is active,
                # and beartype's code generator cannot handle unresolved strings.
                try:
                    hints = typing.get_type_hints(info.handler)
                except Exception:
                    hints = {}
                annotation = hints.get(param_name, inspect.Parameter.empty)
                if annotation is inspect.Parameter.empty:
                    continue

                # A bare {param} (no explicit :converter) defaults to a
                # 'str' router-level spec, but _adapt_handler re-coerces
                # the captured string to the handler's own annotation at
                # call time — so a 'str' spec next to an `int` annotation
                # here is the documented pattern (docs/getting-started/
                # first-app.md), not a bug. Only an *explicit* {param:type}
                # promises the router itself will produce that type.
                if param_name not in info.explicit_param_specs:
                    continue

                _regex_str, converter_fn = _CONVERTERS[spec]
                sample = converter_fn(_SAMPLE_INPUTS[spec])
                try:
                    die_if_unbearable(sample, annotation)
                except BeartypeDoorHintViolation as exc:
                    errors.append(
                        f"Route {info.template!r} param {param_name!r}: "
                        f"converter {spec!r} yields {type(sample).__name__!r} "
                        f"but annotation is {annotation!r}: {exc}")
                except BeartypeException:
                    # beartype internal error (e.g. code-gen bug with a
                    # partially-resolved forward ref) — skip this check.
                    pass

        if errors:
            raise ConfigurationError('\n'.join(errors))

        self._frozen = True

    def route(self, methods: str | HTTPMethod | Iterable[str | HTTPMethod] = [HTTPMethod.GET],
              path: str | re.Pattern = '/', scheme: Scheme | Iterable[Scheme] = Scheme.http,
              functions: list = [], middlewares: list = [],
              name: str | None = None):
        """Register a function or middleware chain in the routing table.

        Three calling conventions:
        1. Decorator with no extra middlewares (``functions`` and ``middlewares``
           both empty) — returns a decorator via ``route_fn``.
        2. ``functions=[...]`` — registers a pre-built chain immediately;
           returns None (same as before).
        3. ``middlewares=[...]`` — returns a decorator; the decorated handler is
           appended to the middleware list before the chain is registered.

        ``name`` registers the route for use with ``url_path_for()``.
        """
        logger.debug('Router.route() is called. functions=%r middlewares=%r', functions, middlewares)

        if self._frozen:
            raise RuntimeError(
                "Router is frozen — routes cannot be added after startup validation")

        if functions:
            fns = list(functions)
            original = fns[-1]
            if _is_simplified_handler(fns[-1]):
                fns[-1] = _adapt_handler(fns[-1], path, self._converters)
            self._register_chain(fns, path, methods, scheme,
                                 name=name, original_handler=original)
            return lambda fn: fn

        if middlewares:
            def decorator(fn):
                adapted = _adapt_handler(fn, path, self._converters) if _is_simplified_handler(fn) else fn
                self._register_chain(list(middlewares) + [adapted], path, methods, scheme,
                                     name=name, original_handler=fn)
                return fn
            return decorator

        return self.route_fn(methods, path, scheme, name=name)


class ErrorRouter:
    """Maps HTTP error statuses and exception classes to ASGI error-handler functions.

    Keys accepted by __setitem__ / __getitem__:
      - HTTPStatus value  (e.g. HTTPStatus.NOT_FOUND)
      - Exception class   (e.g. ValueError)

    Lookup rules:
      - HTTPStatus key: exact match only.
      - Exception class: walks the MRO so a handler registered for a base class
        (e.g. Exception) catches all unhandled subclasses.
      - On a miss, returns the *default* handler passed at construction
        (``None`` when no default was given — caller decides the fallback).

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

    def __init__(self, default: Callable | None = None):
        """*default* is returned on any lookup miss (error statuses and
        unmatched exceptions) instead of ``None``.  Only explicitly
        registered handlers appear in the two registries, so "which statuses
        have custom handlers" stays inspectable."""
        self._status_handlers: dict[HTTPStatus, Callable] = {}
        self._exc_handlers: dict[Type[BaseException], Callable] = {}
        self._default = default

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
            return self._status_handlers.get(key, self._default)

        # Normalise instance → class
        exc_class = key if isinstance(key, type) else type(key)
        if not issubclass(exc_class, BaseException):
            raise TypeError(f"Key must be HTTPStatus or exception class/instance, got {key!r}")

        for cls in exc_class.__mro__:
            if cls in self._exc_handlers:
                return self._exc_handlers[cls]
        return self._default

    def __contains__(self, key: HTTPStatus | Type[BaseException] | BaseException) -> bool:
        return self[key] is not None

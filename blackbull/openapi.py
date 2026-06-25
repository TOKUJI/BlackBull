"""OpenAPI 3.1 spec generation + Swagger UI for BlackBull apps.

Walk the router and emit a minimal but valid OpenAPI document describing every
HTTP route the app exposes.  v1 covers what the router itself already knows:

* paths, methods, scheme (HTTP-only — WebSocket routes are skipped);
* path parameters with schemas derived from the converter (``str`` / ``int`` /
  ``uuid`` / ``path``);
* handler docstring → ``summary`` / ``description``;
* a stub ``200`` response and, for write methods, a permissive
  ``requestBody: object`` placeholder.

What v1 does **not** do:

* Request-body schemas — handlers do not yet declare body models, so the spec
  describes them as ``{"type": "object"}``.  Add a model layer (Pydantic or
  dataclass-with-schema) to lift this.
* Security schemes — auth is application-defined here, so no global
  ``securitySchemes`` are emitted.  Add per-app via the override parameter.
* Tags / grouping — single flat list per path.

Usage from an app::

    app = BlackBull()
    app.enable_openapi(title='My API', version='1.0.0')

    @app.route(path='/items/{item_id:int}')
    async def get_item(item_id: int):
        return {'id': item_id}

After ``app.run()`` the spec is reachable at ``/openapi.json`` and the
Swagger UI at ``/docs``.
"""
from __future__ import annotations

import dataclasses
import inspect
import json
import types
import typing
from http import HTTPMethod
from typing import Any

from .extension import Extension
from .router import _AnyScheme
from .utils import Scheme


# Map of path-converter spec → OpenAPI parameter schema.  Mirrors the
# converter table in router.py but expresses each as JSON-schema rather than
# a regex.
_CONVERTER_SCHEMAS: dict[str, dict] = {
    'str':  {'type': 'string'},
    'int':  {'type': 'integer'},
    'uuid': {'type': 'string', 'format': 'uuid'},
    'path': {'type': 'string'},
}

# HTTP methods that conventionally carry a request body.  These get a
# placeholder ``requestBody`` block.  GET / HEAD / DELETE / OPTIONS do not.
_BODY_METHODS = frozenset({HTTPMethod.POST, HTTPMethod.PUT, HTTPMethod.PATCH})

# Methods that OpenAPI considers operations on a path item.  We emit
# only these; CONNECT and TRACE are valid HTTP but not OpenAPI operations.
_OPENAPI_METHODS = frozenset({
    HTTPMethod.GET, HTTPMethod.PUT, HTTPMethod.POST, HTTPMethod.DELETE,
    HTTPMethod.OPTIONS, HTTPMethod.HEAD, HTTPMethod.PATCH,
})


def _route_serves_http(scheme: Any) -> bool:
    """``True`` when the route is callable over HTTP (not WebSocket-only)."""
    if scheme is None or isinstance(scheme, _AnyScheme):
        return True
    if isinstance(scheme, Scheme):
        return scheme is Scheme.http
    # tuple of schemes
    return any(s is Scheme.http for s in scheme)


def _path_to_oas(template: str) -> str:
    """Convert ``/items/{id:int}`` → ``/items/{id}`` (OpenAPI drops converter spec)."""
    # The router uses `{name}` and `{name:converter}` syntax — replace the
    # latter with the bare name form OpenAPI expects.
    import re  # noqa: PLC0415
    return re.sub(r'\{([a-zA-Z_][a-zA-Z0-9_]*):[^}]+\}', r'{\1}', template)


def _path_parameters(param_specs: dict[str, str]) -> list[dict]:
    """Build the OpenAPI ``parameters`` list for a route's path placeholders."""
    out = []
    for name, spec in param_specs.items():
        schema = _CONVERTER_SCHEMAS.get(spec, {'type': 'string'})
        out.append({
            'name': name,
            'in': 'path',
            'required': True,
            'schema': schema,
        })
    return out


# ---------------------------------------------------------------------------
# Type → JSON-schema synthesis (v2)
#
# Supported:
#   - primitives           — str / int / float / bool / bytes
#   - container generics   — list[X], dict[str, X], tuple[X, ...]
#   - union types          — X | None, Optional[X], X | Y, Union[X, Y]
#   - Python dataclasses   — including nested dataclasses and forward refs
# Unsupported (emits {}):
#   - TypedDict / NamedTuple
#   - Pydantic models      — out of scope; users may post-process the spec
#   - Generic dataclasses with TypeVars
# ---------------------------------------------------------------------------

_PRIMITIVE_SCHEMAS: dict[type, dict] = {
    str:   {'type': 'string'},
    int:   {'type': 'integer'},
    float: {'type': 'number'},
    bool:  {'type': 'boolean'},
    bytes: {'type': 'string', 'format': 'binary'},
}


def _type_to_schema(tp: Any) -> dict:
    """Convert a Python annotation to an OpenAPI 3.1 / JSON-schema dict.

    Unknown types fall through to an empty ``{}`` — OpenAPI 3.1 treats this
    as "no constraint", which is the right default rather than guessing.
    """
    if tp is None or tp is type(None):
        return {'type': 'null'}

    primitive = _PRIMITIVE_SCHEMAS.get(tp)
    if primitive is not None:
        return dict(primitive)

    origin = typing.get_origin(tp)
    args = typing.get_args(tp)

    # X | Y, Union[X, Y], Optional[X]
    if origin is typing.Union or origin is types.UnionType:
        non_none = [a for a in args if a is not type(None)]
        has_none = len(non_none) != len(args)
        if len(non_none) == 1:
            schema = _type_to_schema(non_none[0])
            if has_none:
                # Cleanest OpenAPI 3.1 expression of "T or null".
                return {'anyOf': [schema, {'type': 'null'}]}
            return schema
        sub = [_type_to_schema(a) for a in non_none]
        if has_none:
            sub.append({'type': 'null'})
        return {'anyOf': sub}

    # list[X], tuple[X, ...]
    if origin is list or origin is tuple:
        item = args[0] if args else None
        items_schema = _type_to_schema(item) if item is not None else {}
        return {'type': 'array', 'items': items_schema}

    # dict[K, V] — JSON keys are always strings; we describe the value type.
    if origin is dict:
        v = args[1] if len(args) == 2 else None
        return {
            'type': 'object',
            'additionalProperties': _type_to_schema(v) if v is not None else True,
        }

    if dataclasses.is_dataclass(tp):
        return _dataclass_to_schema(tp)

    return {}


def _dataclass_to_schema(cls: Any) -> dict:
    """Synthesize an OpenAPI schema object for *cls*, a Python dataclass."""
    # Resolve forward references and string-form annotations once, here, so
    # field.type may be either a real class or a `from __future__ import
    # annotations`-style string.
    try:
        hints = typing.get_type_hints(cls)
    except Exception:
        hints = {}

    props: dict[str, dict] = {}
    required: list[str] = []
    for f in dataclasses.fields(cls):
        annotation = hints.get(f.name, f.type)
        field_schema = _type_to_schema(annotation)
        if f.default is not dataclasses.MISSING:
            default = f.default
        elif f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
            try:
                default = f.default_factory()  # type: ignore[misc]
            except Exception:
                default = dataclasses.MISSING
        else:
            default = dataclasses.MISSING
        if default is dataclasses.MISSING:
            required.append(f.name)
        else:
            # Only attach the default if it round-trips through JSON.
            try:
                field_schema['default'] = json.loads(json.dumps(default))
            except (TypeError, ValueError):
                pass  # default isn't JSON-serialisable → omit it from the schema.
        props[f.name] = field_schema

    schema: dict[str, Any] = {
        'type': 'object',
        'title': cls.__name__,
        'properties': props,
    }
    if required:
        schema['required'] = required
    return schema


# ---------------------------------------------------------------------------
# Handler introspection — find dataclass-annotated request body / response
# ---------------------------------------------------------------------------

def _resolved_hints(handler) -> dict[str, Any]:
    """Resolve a handler's annotations, including forward refs.  Returns ``{}`` on failure."""
    try:
        return typing.get_type_hints(handler)
    except Exception:
        return {}


def _find_body_type(handler, param_specs: dict[str, str]) -> Any:
    """Return the annotation of the first dataclass-typed handler parameter
    that is *not* one of the route's path parameters, or ``None``.
    """
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return None
    hints = _resolved_hints(handler)
    for name, param in sig.parameters.items():
        if name in param_specs:
            continue
        if name in ('scope', 'receive', 'send', 'call_next', 'inner', 'self'):
            continue
        ann = hints.get(name, param.annotation)
        if ann is inspect.Parameter.empty:
            continue
        if dataclasses.is_dataclass(ann):
            return ann
    return None


def _find_response_type(handler) -> Any:
    """Return the handler's return annotation when it carries schema-able information."""
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return None
    hints = _resolved_hints(handler)
    ann = hints.get('return', sig.return_annotation)
    if ann is inspect.Signature.empty or ann is None or ann is type(None):
        return None
    return ann


def _docstring_split(handler) -> tuple[str, str]:
    """Return (summary, description) extracted from the handler's docstring."""
    doc = inspect.getdoc(handler)
    if not doc:
        return '', ''
    lines = doc.splitlines()
    summary = lines[0].strip()
    description = '\n'.join(lines[1:]).strip()
    return summary, description


def _operation(handler, param_specs: dict[str, str],
               method: HTTPMethod) -> dict:
    """Build one OpenAPI ``Operation Object`` for the given handler + method."""
    summary, description = _docstring_split(handler)
    op: dict[str, Any] = {}

    if summary:
        op['summary'] = summary
    if description:
        op['description'] = description

    params = _path_parameters(param_specs)
    if params:
        op['parameters'] = params

    if method in _BODY_METHODS:
        # Prefer a synthesized schema from a dataclass-typed handler param.
        # When no annotation is present we fall back to the opaque object
        # schema from v1 so a JSON body is still required.
        body_type = _find_body_type(handler, param_specs)
        body_schema = _type_to_schema(body_type) if body_type is not None else {'type': 'object'}
        op['requestBody'] = {
            'content': {'application/json': {'schema': body_schema}},
        }

    # Response schema from the return annotation, when one is given.
    return_type = _find_response_type(handler)
    response_schema = _type_to_schema(return_type) if return_type is not None else None
    if response_schema:
        op['responses'] = {
            '200': {
                'description': 'OK',
                'content': {'application/json': {'schema': response_schema}},
            },
        }
    else:
        op['responses'] = {'200': {'description': 'OK'}}

    return op


def generate_spec(app, *, title: str = 'BlackBull API',
                  version: str = '0.1.0',
                  description: str | None = None) -> dict:
    """Walk *app*'s router and return a dict matching OpenAPI 3.1.

    The dict is JSON-serialisable.  Pass it through ``json.dumps`` (or
    ``JSONResponse``) on the spec route.
    """
    info: dict[str, Any] = {'title': title, 'version': version}
    if description:
        info['description'] = description

    paths: dict[str, dict] = {}
    seen_paths_at_method: set[tuple[str, str]] = set()
    for ri in app._router._route_info:
        if not _route_serves_http(ri.scheme):
            continue
        if getattr(ri.handler, '__blackbull_openapi_internal__', False):
            # Routes the framework itself published (the spec endpoint and
            # the Swagger UI host) — keep them out of their own documentation.
            continue
        oas_path = _path_to_oas(ri.template)
        path_item = paths.setdefault(oas_path, {})
        for method in ri.methods:
            if not isinstance(method, HTTPMethod):
                # methods may already be tuples of HTTPMethod, but be lenient.
                try:
                    method = HTTPMethod(method)
                except ValueError:
                    continue
            if method not in _OPENAPI_METHODS:
                continue
            key = (oas_path, method.value)
            if key in seen_paths_at_method:
                # Duplicate registration (or an app.use chain that re-wrapped
                # an existing route) — skip rather than emit a clashing op.
                continue
            seen_paths_at_method.add(key)
            path_item[method.value.lower()] = _operation(
                ri.handler, ri.param_specs, method)

    return {
        'openapi': '3.1.0',
        'info': info,
        'paths': paths,
    }


# ---------------------------------------------------------------------------
# Swagger UI
# ---------------------------------------------------------------------------

# Pinned upstream release.  Swagger UI is loaded from the CDN — no offline
# story in v1, but the request is small and cacheable.  Bumping the version
# requires no other changes here; the URLs are versioned identically for the
# JS bundle and the CSS.
_SWAGGER_UI_VERSION = '5.17.14'

# Minimal HTML host page for Swagger UI.  The {{spec_url}} placeholder is
# substituted at serve time so users may override the spec route.
_SWAGGER_UI_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@{version}/swagger-ui.css">
</head>
<body>
<div id="swagger-ui"></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@{version}/swagger-ui-bundle.js"></script>
<script>
window.onload = () => {{
  window.ui = SwaggerUIBundle({{
    url: "{spec_url}",
    dom_id: "#swagger-ui",
    deepLinking: true,
  }});
}};
</script>
</body>
</html>
"""


def swagger_ui_html(spec_url: str, title: str = 'BlackBull API — Swagger UI') -> str:
    """Return a self-contained HTML page hosting Swagger UI pointed at *spec_url*."""
    return _SWAGGER_UI_HTML.format(
        title=title, version=_SWAGGER_UI_VERSION, spec_url=spec_url)


# ---------------------------------------------------------------------------
# Extension class — reference implementation of the ``init_app(app)`` convention
# (see docs/guide/extensions.md).
#
# Mounts ``/openapi.json`` (and optionally ``/docs``) on the application, and
# registers itself at ``app.extensions['openapi']`` so collaborators can look
# up the live extension instance.  ``BlackBull.enable_openapi(...)`` is a thin
# convenience wrapper around this class.
# ---------------------------------------------------------------------------


class OpenAPIExtension(Extension):
    """Mount an OpenAPI 3.1 spec endpoint and Swagger UI on a BlackBull app.

    Accepts the same arguments as ``BlackBull.enable_openapi``.  Two
    construction styles are supported, following the framework's
    ``init_app(app)`` extension convention:

    >>> # Eager — wire on construction.
    >>> OpenAPIExtension(app, title='My API', version='1.0.0')

    >>> # Deferred — useful when the app is configured elsewhere.
    >>> ext = OpenAPIExtension(title='My API', version='1.0.0')
    >>> ext.init_app(app)

    After ``init_app``:

    * ``app.extensions['openapi']`` is *self* — collaborators can read the
      configured ``title``/``version``/``spec_path`` from it.
    * Two GET routes are registered: ``spec_path`` (JSON) and ``docs_path``
      (HTML host for Swagger UI), the latter skipped when ``docs_path`` is
      ``None``.
    * Both routes are flagged ``__blackbull_openapi_internal__`` so the spec
      doesn't include itself.
    """

    #: Key under which the extension registers itself in ``app.extensions``.
    #: Follows the ``blackbull-<name>`` → ``<name>`` convention from the
    #: extensions guide; not configurable to avoid a collision-bypass loophole.
    extension_key: str = 'openapi'

    def __init__(self, app: object | None = None, *,
                 title: str = 'BlackBull API',
                 version: str = '0.1.0',
                 description: str | None = None,
                 spec_path: str = '/openapi.json',
                 docs_path: str | None = '/docs') -> None:
        self.title = title
        self.version = version
        self.description = description
        self.spec_path = spec_path
        self.docs_path = docs_path
        # Hold references to the registered handler functions so they survive
        # past ``init_app`` return — the router stores them weakly via
        # ``functools.wraps``, and a GC of the closure would cause the route
        # to point at a dead function.
        self._spec_handler = None
        self._docs_handler = None
        if app is not None:
            self.init_app(app)

    def init_app(self, app) -> None:
        """Wire the spec + docs routes onto *app* through the public API."""
        # Late imports keep this module importable without the rest of the
        # framework being initialised (e.g. in spec-only tools).
        from .response import Response, JSONResponse  # noqa: PLC0415

        existing = app.extensions.get(self.extension_key)
        if existing is not None and existing is not self:
            existing_origin = type(existing).__module__
            raise RuntimeError(
                f"app.extensions[{self.extension_key!r}] is already registered "
                f"by {existing_origin}. Cannot initialise "
                f"{type(self).__module__}.{type(self).__name__}.")

        title, version, description = self.title, self.version, self.description
        spec_path, docs_path = self.spec_path, self.docs_path

        async def _openapi_spec(scope, receive, send):  # noqa: ARG001
            spec = generate_spec(app, title=title, version=version,
                                 description=description)
            await send(JSONResponse(spec))
        # Mark before registration so the original handler stored in
        # ``Router._route_info`` carries the flag — ``functools.wraps`` does
        # not copy arbitrary attributes onto the wrapper, so setting it
        # post-hoc on the decorated form would not propagate back.
        _openapi_spec.__blackbull_openapi_internal__ = True
        app.route(methods=HTTPMethod.GET, path=spec_path)(_openapi_spec)
        self._spec_handler = _openapi_spec

        if docs_path is not None:
            ui_title = f'{title} — Swagger UI'

            async def _swagger_ui(scope, receive, send):  # noqa: ARG001
                html = swagger_ui_html(spec_path, title=ui_title)
                await send(Response(html))
            _swagger_ui.__blackbull_openapi_internal__ = True
            app.route(methods=HTTPMethod.GET, path=docs_path)(_swagger_ui)
            self._docs_handler = _swagger_ui

        app.extensions[self.extension_key] = self

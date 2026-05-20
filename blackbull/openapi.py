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

import inspect
from http import HTTPMethod
from typing import Any

from .router import _CONVERTERS, _AnyScheme
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
    op: dict[str, Any] = {
        'responses': {
            '200': {'description': 'OK'},
        },
    }
    if summary:
        op['summary'] = summary
    if description:
        op['description'] = description
    params = _path_parameters(param_specs)
    if params:
        op['parameters'] = params
    if method in _BODY_METHODS:
        # Placeholder schema — handlers do not yet declare body models.
        # See v2 of the docs work; for now we just say "some JSON, please".
        op['requestBody'] = {
            'content': {'application/json': {'schema': {'type': 'object'}}},
        }
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

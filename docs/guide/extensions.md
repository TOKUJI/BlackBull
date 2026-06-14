# Extensions

BlackBull's core surface is intentionally narrow — protocols,
routing, middleware, events, error handling.  Everything else
(auth, admin dashboards, ORMs, template engines, ...) lives
outside the core as **extensions**: small packages that wire
themselves into an application using the existing public APIs
(`app.use`, `app.route`, `app.on`, `app.on_error`).

The extension surface is one attribute and one convention:

- **`app.extensions`** — a `dict[str, object]` for extension
  instances to register themselves under a stable key.
- **`init_app(app)`** — a method extensions implement so the
  application author can wire them in explicitly.

There is no plugin registry, no auto-discovery, no dependency
injection.  Composition is the application author's job.

## The `app.extensions` namespace

Every `BlackBull` instance carries an `extensions` dict, empty
at construction:

```python
from blackbull import BlackBull

app = BlackBull()
assert app.extensions == {}
```

Extensions write themselves into it under a documented key:

```python
app.extensions['auth'] = my_auth_instance
```

Application code reads it back when one extension needs to
look up another (for example a route handler needing the
configured auth instance).

## The `init_app(app)` convention

An extension is any class with an `init_app(app)` method that
registers its routes, middleware, events, and error handlers
through the existing `app.*` APIs:

```python
from blackbull import BlackBull


class HelloExtension:
    def __init__(self, greeting: str = 'Hello'):
        self._greeting = greeting

    def init_app(self, app: BlackBull) -> None:
        app.extensions['hello'] = self

        @app.route(path='/hello')
        async def hello():
            return {'message': f'{self._greeting} from extension'}

        # Keep a reference so the registered handler is not GC'd
        # if the user does not retain it.
        self._handler = hello


app = BlackBull()
HelloExtension(greeting='Howdy').init_app(app)
```

Optionally accept `app` in the constructor and call
`init_app` for the user:

```python
class HelloExtension:
    def __init__(self, app: BlackBull | None = None, *, greeting: str = 'Hello'):
        self._greeting = greeting
        if app is not None:
            self.init_app(app)

    def init_app(self, app: BlackBull) -> None:
        ...
```

Both styles are supported by convention; pick one per extension
and document it.

## Extension keys in `app.extensions`

The `app.extensions` dict is a shared namespace.  Keys follow
one rule:

- **For published packages** named `blackbull-<name>` on PyPI,
  the key MUST be `<name>` (the package name with the
  `blackbull-` prefix stripped).  A package named
  `blackbull-auth` registers itself at `app.extensions['auth']`;
  `blackbull-session` at `app.extensions['session']`.
- **For application-private extensions** (not published),
  pick a key unlikely to collide with any published
  `blackbull-*` package — prefix with an underscore or the
  application name:

  ```python
  app.extensions['_myapp_cache'] = MyAppCache(...)
  ```

If a collision is detected at `init_app` time, the extension
SHOULD raise `RuntimeError` rather than silently overwriting:

```python
def init_app(self, app: BlackBull) -> None:
    if 'auth' in app.extensions:
        existing = type(app.extensions['auth']).__module__
        raise RuntimeError(
            f"app.extensions['auth'] is already registered by "
            f"{existing}. Cannot initialise {type(self).__module__}.")
    app.extensions['auth'] = self
```

## Extension dependencies and `init_app` ordering

When extension A depends on extension B (for example, an admin
dashboard that needs an auth extension to protect its routes),
the application author calls `init_app(app)` in dependency
order — B before A:

```python
auth = AuthExtension(jwt_secret=os.environ['JWT_SECRET'])
auth.init_app(app)

admin = AdminExtension()
admin.init_app(app)   # looks up app.extensions['auth']
```

BlackBull does NOT enforce ordering — that would require a
dependency resolver inside the core.  Instead the dependent
extension validates its prerequisites at `init_app` time and
raises a clear error if they are missing:

```python
class AdminExtension:
    def init_app(self, app: BlackBull) -> None:
        if 'auth' not in app.extensions:
            raise RuntimeError(
                'AdminExtension requires an auth extension. '
                'Initialise it first: auth.init_app(app)')
        self._auth = app.extensions['auth']
        app.extensions['admin'] = self
```

The application author owns the dependency graph; the
extension author owns the prerequisite check.  The core stays
free of resolution logic.

## Extension vs library

Not every reusable component needs to be an extension.

| Use an extension when | Just import the library when |
| --- | --- |
| The component registers routes, middleware, events, or error handlers on the app | The component is a pure function or class used inside a handler |
| The component needs configuration tied to the app's lifecycle | The component is configured at module import time |
| Multiple parts of the app need to look the component up by name | One handler uses it and nobody else needs a reference |

A database client, JSON serialiser, or password hasher is a
library — import it directly.  An auth layer that adds login
routes and a `scope['user']` middleware is an extension —
wire it through `init_app`.

## In-tree reference: `OpenAPIExtension`

BlackBull's own OpenAPI publication is implemented as
[`blackbull.openapi.OpenAPIExtension`](openapi.md#the-openapiextension-class).
It is the reference for this convention — small enough to read
end-to-end, and shipped as part of the framework so it never
drifts from the public extension surface:

```python
from blackbull.openapi import OpenAPIExtension

OpenAPIExtension(app, title='My API', version='1.0.0')
# app.extensions['openapi'] is the live instance.
```

`BlackBull.enable_openapi(...)` is a thin convenience wrapper
around the same class — so even the core's own ergonomics call
into the public extension API.

## ASGI middleware via `app.use()`

`app.use()` accepts any ASGI 3.0 middleware following the
`(scope, receive, send, call_next)` shape, so an extension can
simply pass a middleware callable through it:

```python
async def x_request_id_mw(scope, receive, send, call_next):
    scope['x-request-id'] = generate_id()
    await call_next(scope, receive, send)


class RequestIdExtension:
    def init_app(self, app: BlackBull) -> None:
        app.use(x_request_id_mw)
        app.extensions['request_id'] = self
```

See [Middleware](middleware.md) for the full middleware
contract, including how to short-circuit the chain and how to
inspect responses via `intercepting_send`.

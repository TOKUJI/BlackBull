# Extensions

BlackBull's core surface is intentionally narrow — protocols,
routing, middleware, events, error handling.  Everything else
(auth, admin dashboards, ORMs, template engines, ...) lives
outside the core as **extensions**: small packages that wire
themselves into an application using the existing public APIs
(`app.use`, `app.route`, `app.on`, `app.on_error`).

The extension surface is one base class, one registration method,
one attribute, and one convention:

- **`Extension`** — `blackbull.extension.Extension`, the base class
  extensions subclass.  It declares `extension_key`, an abstract
  `init_app(app)`, and optional async `startup(app)` / `shutdown(app)`
  lifecycle hooks.
- **`app.add_extension(ext)`** — the single registration entry point
  on the core.  It calls `ext.init_app(app)`, wires any
  `startup`/`shutdown` into the app's lifespan, and returns `ext`.
- **`app.extensions`** — a `dict[str, object]` where extension
  instances register themselves under a stable key.
- **`init_app(app)`** — the method an extension implements to wire
  itself in.

`add_extension` duck-types on `init_app`, so legacy extensions that
predate the `Extension` base class keep working unchanged.  There is
no plugin registry, no auto-discovery, no dependency injection.
Composition is the application author's job.

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

## The `Extension` base class and `app.add_extension`

Prefer subclassing `blackbull.extension.Extension` and registering
through `app.add_extension`.  The base class gives you a typed
contract and a `_register` helper (the duplicate-key guard plus
self-storage in `app.extensions`); `add_extension` is the single
registration seam on the core and returns the instance for chaining:

```python
from blackbull import BlackBull
from blackbull.extension import Extension


class HelloExtension(Extension):
    extension_key = 'hello'

    def __init__(self, greeting: str = 'Hello'):
        self._greeting = greeting

    def init_app(self, app: BlackBull) -> None:
        @app.route(path='/hello')
        async def hello():
            return {'message': f'{self._greeting} from extension'}
        self._handler = hello
        self._register(app)            # dup-check + app.extensions['hello'] = self


app = BlackBull()
hello = app.add_extension(HelloExtension(greeting='Howdy'))
```

`add_extension` accepts any object with an `init_app(app)` method, so
the duck-typed style above still works — adopting the base class is
recommended, not required.

### Lifecycle: `startup` / `shutdown`

Override the optional async hooks for resources that must open and
close with the application.  `add_extension` wires them into the
`app_startup` / `app_shutdown` lifespan events, so they run under the
ASGI lifespan **and** per worker in multi-worker mode:

```python
class PoolExtension(Extension):
    extension_key = 'pool'

    def init_app(self, app):
        self._register(app)

    async def startup(self, app):
        self.pool = await open_pool()

    async def shutdown(self, app):
        await self.pool.close()
```

### Protocol extensions use the same mechanism

A non-HTTP protocol is just an extension whose `init_app` calls
`app.register_protocol_handler(...)`.  The in-tree MQTT broker
(`blackbull.mqtt.MQTTExtension`) is the reference: it
registers the broker on a port and exposes its own `on_message`
decorator, with **zero** MQTT-specific code on the `BlackBull` class.
See [MQTT broker](mqtt.md) and [Non-ASGI protocols](raw-protocols.md).

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
routes and a `conn.state['user']` middleware is an extension —
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

## Middleware via `app.use()`

`app.use()` accepts any middleware following the
`(conn, receive, send, call_next)` shape, so an extension can
simply pass a middleware callable through it:

```python
async def x_request_id_mw(conn, receive, send, call_next):
    conn.state['x-request-id'] = generate_id()
    await call_next(conn, receive, send)


class RequestIdExtension:
    def init_app(self, app: BlackBull) -> None:
        app.use(x_request_id_mw)
        app.extensions['request_id'] = self
```

See [Middleware](middleware.md) for the full middleware
contract, including how to short-circuit the chain and how to
inspect responses via `intercepting_send`.

## Packaging an extension

### Pick a `dependencies` floor that matches what you import

`app.extensions` landed in BlackBull 0.36.0.
An extension that touches it must pin at least that floor:

```toml
dependencies = [
    "blackbull >= 0.36.0",
]
```

If your extension uses something newer — `intercepting_send`, a
specific event name, a `scope` field — bump the floor to match.
Don't pin an exact version; that fights with downstream apps that
want a different patch level.

### Reuse the framework's public middleware helpers

Two BlackBull APIs are useful to extension authors and are
public APIs; respect the declared minimum BlackBull version:

- `from blackbull.middleware import as_middleware` — normalizes `Response`
  objects for the active native or external-ASGI lane. A send wrapper must
  handle that lane's messages; see [Middleware](middleware.md).
- `from blackbull.asgi import ASGIEvent` — symbolic constants for
  ASGI event type strings (`HTTP_RESPONSE_START`, etc.).  Prefer
  these over hard-coded literals.

If you find yourself reaching for `from blackbull._something_private`,
stop and ask whether the symbol should be promoted to a public
re-export.  Opening an issue is the right move; private imports
will break across MINOR versions.

### Eager + deferred construction

Follow the same dual-construction shape as `OpenAPIExtension`:

```python
class MyExtension:
    extension_key: str = 'myext'

    def __init__(self, app=None, *, opt=...):
        # validate/store config first
        self._opt = opt
        # then wire on-construction if eager
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        existing = app.extensions.get(self.extension_key)
        if existing is not None and existing is not self:
            raise RuntimeError(...)
        app.use(self)                                # or app.route(...), etc.
        app.extensions[self.extension_key] = self
```

The `existing is not self` guard makes `init_app` idempotent for the
same instance — a defensive nicety for users who wire eagerly and
then call `init_app` explicitly anyway.

### Make `extension_key` a class attribute, not configurable

A `__init__` kwarg like `extension_key='session-v2'` looks
flexible but lets users sneak past the collision check (`if 'session'
in app.extensions: raise` is bypassed when a second extension
registers itself under a different key).  Pin the key as a class
attribute and document it in your `README`.

### Place `app` as the first positional argument

`SessionExtension(app, secret=...)` reads cleanly only when `app`
is positional-first.  If you place it after a `*` keyword separator
you force `SessionExtension(secret=..., app=app)` — readable but
inverts the convention every other extension already follows.

### Tests: depend on `blackbull[testing]` for the parent framework

```toml
[project.optional-dependencies]
testing = [
    "pytest",
    "pytest-asyncio",
    "blackbull[testing] >= 0.36.0",
]
```

This pulls in `pytest-asyncio`, `httpx[http2]`, `websockets`,
`hypothesis`, and `openapi-spec-validator` — the same testing
surface BlackBull itself uses.  You don't have to re-declare those.

### Deprecating an in-tree class you're extracting

If your extension replaces something that previously lived in
BlackBull, the in-tree form should emit a `DeprecationWarning` on
construction:

```python
import warnings

class Session:           # in-tree, deprecated form
    def __init__(self, ...):
        warnings.warn(
            "blackbull.middleware.Session is deprecated; install "
            "'blackbull-session' and use SessionExtension instead. "
            "Will be removed no earlier than BlackBull v0.41 "
            "(and not before 2026-07-14).",
            DeprecationWarning,
            stacklevel=2,
        )
        ...
```

Schedule removal at least three MINOR releases and one calendar month after
announcing the deprecation, using whichever boundary is later. State both
gates and the replacement in the warning so users can plan their migration.

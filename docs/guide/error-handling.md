# Error handling

BlackBull installs a default handler for every HTTP error status
plus the `Exception` base class, so unhandled errors always produce
a response (no naked connection drops).  You can override any of
them with `@app.on_error(...)`.

## Default behaviour

| Condition | Default response |
|---|---|
| Path not registered | 404 |
| Method not allowed | 405 + `Allow` header listing valid methods |
| Unhandled exception in handler | 500 |

The default handler adapts its body to the `BLACKBULL_ENV`
environment variable and the request's `Accept` header.

### `development` (default)

```bash
# BLACKBULL_ENV=development  (or unset — development is the default)
```

For 500 errors with an exception:

- `Accept: text/html` (browser) → styled HTML page with the full
  Python traceback inline.
- Everything else (curl, fetch, etc.) → text/plain with the status
  line, exception class, exception message, and full traceback.

Goal: when you hit an unexpected 500 in dev, the failure point is
visible in the response body — you don't have to dig through the
access log.

For 404 / 405 / other client-error statuses without an exception,
the body is just the status code + phrase (with the `Allow` header
on 405).

**4xx `HTTPException`s carry their detail, not a traceback** (since
v0.56.0): when the framework itself diagnoses a client fault — a
missing required query parameter, a malformed JSON body — the dev
page shows the status and the human-readable detail line
(`missing required query parameter 'q' for handler 'search'`) and
omits the Python traceback.  The frames would only show framework
internals; the detail line is the actionable part.  5xx
`HTTPException`s and unexpected exceptions keep the full traceback.

### `production`

```bash
BLACKBULL_ENV=production python myapp.py
```

Terse output — status code + phrase only.  Exception class and
message are **not** leaked to the network.  Browsers get a
minimal HTML page; everything else gets text/plain.

This is the right posture for public endpoints: production users
of your service shouldn't see Python internals when something
fails.  Log the exception server-side via your normal logging /
observability pipeline.

The same rule applies to the `Content-Type` and `Content-Length`
headers — both are set explicitly on every error response in both
environments.

## Custom error handlers

`@app.on_error(...)` registers a handler for a specific status
code or exception type:

```python
from http import HTTPStatus
from blackbull import JSONResponse

@app.on_error(HTTPStatus.NOT_FOUND)
async def handle_404(scope, receive, send):
    await send(JSONResponse({'error': 'not found'},
                            status=HTTPStatus.NOT_FOUND))

@app.on_error(HTTPStatus.METHOD_NOT_ALLOWED)
async def handle_405(scope, receive, send):
    allowed = ', '.join(scope['state'].get('allowed_methods', ()))
    await send(JSONResponse({'error': f'allowed: {allowed}'},
                            status=HTTPStatus.METHOD_NOT_ALLOWED))

@app.on_error(ValueError)
async def handle_value_error(scope, receive, send):
    exc = scope['state'].get('error_exception')
    await send(JSONResponse({'error': str(exc)},
                            status=HTTPStatus.BAD_REQUEST))
```

Exception handlers use MRO walk: a handler registered for
`Exception` catches all unhandled subclasses.  More specific
handlers (e.g. `ValueError`) take priority over base-class
handlers.

## What's in `scope['state']`

The framework populates `scope['state']` with information about
the error before calling your handler:

| Key | Type | Present when |
|---|---|---|
| `'error_status'` | `HTTPStatus` | Always |
| `'error_exception'` | `Exception` | Triggered by an uncaught exception |
| `'allowed_methods'` | tuple of `str` | 405 Method Not Allowed |

Reading these is how a custom handler builds a response that
matches the framework's view of what failed.

## Custom HTML error pages

To customize the look of the DEV-mode traceback page or the
PROD-mode minimal page, register a handler for the status code
you want to override:

```python
@app.on_error(HTTPStatus.INTERNAL_SERVER_ERROR)
async def custom_500(scope, receive, send):
    exc = scope['state'].get('error_exception')
    # ... your rendering ...
    await send(Response(body, status=HTTPStatus.INTERNAL_SERVER_ERROR,
                        content_type='text/html'))
```

The default handler is only used when no custom one is registered.
Per-status registration overrides only that status; other errors
keep the default behaviour.

## Next

- [Events](events.md) — `before_handler` / `after_handler` /
  `request_received` for observational hooks on the error path.
- [Logging](logging.md) — access log, framework loggers,
  structured error logging.

# OpenAPI and Swagger UI

One call publishes a machine-readable spec of your API and an
interactive UI for exploring it.

```python
from blackbull import BlackBull

app = BlackBull()

@app.route(path='/items/{item_id:int}')
async def get_item(item_id: int):
    """Get one item.

    Returns the item with the given numeric id.
    """
    return {'id': item_id}

app.enable_openapi(title='Items API', version='1.0.0')
```

Browse to:

- `http://localhost:8000/openapi.json` — OpenAPI 3.1 JSON
- `http://localhost:8000/docs` — Swagger UI (loads from CDN)

Both routes are GET-only and self-documenting: the spec
describes every HTTP route the app exposes but *excludes itself*
and the docs page.

## What ends up in the spec

| Source | Mapped to |
|---|---|
| Route template (`/items/{item_id:int}`) | OpenAPI path (`/items/{item_id}`) with a `parameters` entry |
| Path-param converter (`int` / `str` / `uuid` / `path`) | Path-parameter `schema` (see [Typed routes](routing.md#typed-routes)) |
| HTTP methods on the route | Operation objects under the path |
| Handler docstring — first line | Operation `summary` |
| Handler docstring — rest | Operation `description` |
| HTTP-only (no `scheme=Scheme.websocket`) | Included.  WebSocket routes are skipped. |
| `POST` / `PUT` / `PATCH` route | A placeholder `requestBody: {type: object}` — replaced by a real schema when the handler is annotated with a dataclass (below) |

## `app.enable_openapi(...)` parameters

| Parameter | Default | Notes |
|---|---|---|
| `title` | `'BlackBull API'` | Spec `info.title` and the Swagger UI page title. |
| `version` | `'0.1.0'` | Spec `info.version`. |
| `description` | `None` | Spec `info.description` (Markdown — Swagger UI renders it). |
| `spec_path` | `'/openapi.json'` | Route that returns the JSON.  Regenerated on every request. |
| `docs_path` | `'/docs'` | Route that returns the Swagger UI host page.  Pass `None` to skip the UI and serve only the JSON. |

Call `enable_openapi()` once, **after** the rest of your routes
are registered.

## Real schemas via dataclasses

Annotate a handler parameter with a Python `dataclass` and the
spec gains a real request-body schema; annotate the return type
and the spec gains a real response schema.  No extra dependency
— `dataclasses` is in the standard library.

```python
from dataclasses import dataclass, field
from http import HTTPMethod

@dataclass
class CreateTask:
    title: str
    completed: bool = False
    tags: list[str] = field(default_factory=list)

@dataclass
class Task:
    id: int
    title: str
    completed: bool

@app.route(methods=HTTPMethod.POST, path='/tasks')
async def create_task(body: CreateTask) -> Task:
    ...
```

Produces, in the spec:

```json
"/tasks": {
  "post": {
    "requestBody": {
      "content": {"application/json": {"schema": {
        "type": "object",
        "title": "CreateTask",
        "properties": {
          "title":     {"type": "string"},
          "completed": {"type": "boolean", "default": false},
          "tags":      {"type": "array", "items": {"type": "string"}, "default": []}
        },
        "required": ["title"]
      }}}
    },
    "responses": {
      "200": {
        "description": "OK",
        "content": {"application/json": {"schema": {
          "type": "object", "title": "Task",
          "properties": {
            "id":        {"type": "integer"},
            "title":     {"type": "string"},
            "completed": {"type": "boolean"}
          },
          "required": ["id", "title", "completed"]
        }}}
      }
    }
  }
}
```

Supported in field annotations and return types:

| Python annotation | OpenAPI schema |
|---|---|
| `str` / `int` / `float` / `bool` / `bytes` | `{"type": "string"}` etc. |
| `T \| None`, `Optional[T]` | `{"anyOf": [<T>, {"type": "null"}]}` |
| `T \| U` (PEP 604 union) | `{"anyOf": [<T>, <U>]}` |
| `list[T]`, `tuple[T, ...]` | `{"type": "array", "items": <T>}` |
| `dict[str, V]` | `{"type": "object", "additionalProperties": <V>}` |
| Nested `@dataclass` | recursive object schema |
| Field default | `default:` attached when JSON-serializable |
| Field without default | added to `required:` |

Anything not in this list (`TypedDict`, `NamedTuple`, generic
dataclasses with `TypeVar`s, Pydantic models, etc.) falls
through to `{}` — OpenAPI 3.1 treats that as "no constraint",
which is the right default rather than a wrong guess.

## Body deserialization

When a simplified handler's parameter is annotated with a
dataclass, the router reads the request body, parses it as JSON,
and constructs an instance for you.  No `read_body` /
`json.loads` boilerplate in the handler:

```python
@dataclass
class CreateTask:
    title: str
    completed: bool = False
    tags: list[str] = field(default_factory=list)

@app.route(methods=HTTPMethod.POST, path='/tasks')
async def create_task(body: CreateTask) -> Task:
    new = Task(id=next_id(), title=body.title, completed=body.completed)
    ...
    return new
```

The annotation drives detection — the parameter name does not
have to be `body`.  `async def create_task(item: CreateTask)`
works the same way.  A handler may have at most one body
parameter; the router rejects two-body signatures at
registration.

Coercion rules:

| JSON shape | Constructs |
|---|---|
| `{"field": ...}` matching a `@dataclass` | the dataclass with that field populated |
| nested `{...}` inside a field typed as another `@dataclass` | recursive construction |
| array inside a field typed `list[T]` / `tuple[T, ...]` | each element coerced to `T` |
| `null` inside a field typed `T \| None` | `None` |
| primitive into `T \| U` | first union branch that constructs cleanly (dataclass branches tried first) |

Unknown JSON keys raise `TypeError` rather than being silently
dropped — a client typo like `{"titel": ...}` should surface, not
vanish.

Handlers may also **return** a dataclass (or a list of
dataclasses).  The adapter serializes it via
`dataclasses.asdict` recursively, so the same model works on
both ends of the wire.

Errors propagate to the framework's error router:

- Malformed JSON → `json.JSONDecodeError`
- Missing required field / unknown field / type mismatch →
  `TypeError`

Register handlers via `@app.on_error(json.JSONDecodeError)` and
`@app.on_error(TypeError)` to convert them to 400 / 422
responses if the default 500 isn't what you want.

!!! note "No external model library required"
    This works with the standard library's `dataclasses`.
    Pydantic, attrs, msgspec, etc. are not directly supported —
    if you want one of those, parse the body yourself from a
    `body: bytes` parameter.

## What's not yet automated

- **Security schemes.**  Auth is application-defined, so no
  global `securitySchemes` are emitted.  Add them post-hoc by
  editing the spec returned by `generate_spec()` and serving
  the result yourself.
- **Tags / grouping.**  Operations are flat under their path;
  tag-based grouping in Swagger UI requires manual annotation
  today.
- **Status-code variants.**  Every operation emits a single
  `200: OK` response.  Distinguishing `201 Created` for POST,
  `204 No Content` for DELETE, etc. needs more cues than the
  return annotation alone.

## Next

- [Routing — Typed routes](routing.md#typed-routes) — how path
  converters drive the OpenAPI path-parameter schemas.
- [Error handling](error-handling.md) — turning the
  `TypeError` / `JSONDecodeError` cases above into the response
  codes that fit your API.

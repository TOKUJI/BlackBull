"""
Simple Task Manager
===================
REST + HTML task management application demonstrating BlackBull's event-driven
API alongside per-route function middleware.

Cross-cutting concerns:
    Request logging  → @app.on('request_received')   observer (fire-and-forget)
    Bearer auth      → @app.intercept('before_handler') (short-circuits to 401)
    HTTP/2 priority  → @app.intercept('before_handler') (reads urgency hint)
    JSON body        → json_body_mw per-route middleware (injects scope['json'])
    DB startup       → @app.on_startup

Why @app.on for logging, not middleware?
    Logging must never block or abort the request.  Observers run in an
    independent asyncio.Task — exceptions are isolated, the handler is
    never short-circuited.

Why @app.intercept for auth?
    Auth must run synchronously before the handler; raising Unauthorized
    prevents the handler from running.  The @app.on_error(Unauthorized)
    handler maps that exception to a 401 JSON response.

Why json_body_mw stays as per-route middleware?
    JSON parsing is a per-request concern only for POST/PUT routes; making
    it global via intercept would parse the body on GET requests where no
    body exists.

Authentication: Bearer token in the Authorization header, stored in
sessionStorage by the browser.  Avoids HttpOnly-cookie/SameSite browser
quirks over plain HTTP.

Run:
    python app.py [--port 8000]

Default credentials: admin / admin  (seeded into tasks.db at first startup)
"""
import argparse
import asyncio
import json
import logging
import pathlib
import secrets
from http import HTTPMethod, HTTPStatus

import db
from blackbull import (
    BlackBull,
    JSONResponse,
    Response,
    read_body,
)

_TEMPLATES = pathlib.Path(__file__).parent / 'templates'


def _load(name: str) -> bytes:
    return (_TEMPLATES / name).read_bytes()


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO,
                    format='%(levelname)s %(name)s: %(message)s')

# ---------------------------------------------------------------------------
# In-memory session store
# ---------------------------------------------------------------------------

SESSIONS: dict[str, str] = {}   # token → username

# ---------------------------------------------------------------------------
# Per-route middleware (request-scoped; not suitable for global intercept)
# ---------------------------------------------------------------------------

async def json_body_mw(scope, receive, send, call_next):
    """Read and parse the request body as JSON; inject scope['json']."""
    raw = await read_body(receive)
    try:
        scope['json'] = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        await send(JSONResponse({'error': 'Invalid JSON'},
                                status=HTTPStatus.BAD_REQUEST))
        return
    await call_next(scope, receive, send)

# ---------------------------------------------------------------------------
# Custom exception for auth failures — caught by @app.on_error below
# ---------------------------------------------------------------------------

class Unauthorized(Exception):
    pass

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = BlackBull()


@app.on_startup
async def startup():
    logger.info('Initialising database…')
    await db.init_db()
    logger.info('Database ready. Default user: admin / admin')


@app.on_error(Unauthorized)
async def handle_unauthorized(scope, receive, send):
    """Convert Unauthorized raised by the auth interceptor into a 401 response."""
    await send(JSONResponse({'error': 'Unauthorized'}, status=HTTPStatus.UNAUTHORIZED))


@app.intercept('before_handler')
async def require_auth(event):
    """Interceptor: validate Bearer token for protected routes.

    Raises Unauthorized when the token is missing or invalid; the
    @app.on_error(Unauthorized) handler above sends the 401 response.
    Routes that do not start with a protected prefix are passed through.
    """
    path = event.detail['path']
    if not (path.startswith('/tasks') or path == '/logout'):
        return
    scope = event.detail['scope']
    auth = scope['headers'].get(b'authorization', b'')
    token = auth[7:].decode() if auth.startswith(b'Bearer ') else ''
    username = SESSIONS.get(token)
    if not username:
        raise Unauthorized('Unauthorized')
    scope['user'] = username
    scope['token'] = token


@app.intercept('before_handler')
async def handle_priority(event):
    """Interceptor: read HTTP/2 priority hint and annotate the scope.

    urgency 0-1  → high-priority: log prominently.
    urgency 2-7  → normal/background: annotate scope only.
    """
    scope = event.detail['scope']
    hint = scope.get('http2_priority', {'urgency': 3, 'incremental': False})
    urgency = hint['urgency']
    scope['_priority_urgency'] = urgency
    if urgency <= 1:
        logger.info('HIGH-PRIORITY u=%d: %s %s',
                    urgency, event.detail['method'], event.detail['path'])


@app.on('request_received')
async def log_request(event):
    """Observer: log every incoming request — fire-and-forget, never short-circuits."""
    logger.info('%s %s', event.detail['method'], event.detail['path'])


@app.on('request_completed')
async def log_response(event):
    """Observer: log completion with status code and timing."""
    logger.info('  → %s (%.1f ms)', event.detail['status'], event.detail['duration_ms'])

# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------

@app.route(methods=[HTTPMethod.GET], path='/')
async def handle_login_page(scope, receive, send):  # noqa: ARG001
    await send(Response(_load('login.html')))


@app.route(methods=[HTTPMethod.GET], path='/register')
async def handle_register_page(scope, receive, send):  # noqa: ARG001
    await send(Response(_load('register.html')))


@app.route(methods=[HTTPMethod.POST], path='/register',
           middlewares=[json_body_mw])
async def handle_register(scope, receive, send):  # noqa: ARG001
    data = scope['json']
    username = str(data.get('username', '')).strip()
    password = str(data.get('password', ''))

    if not username or not password:
        await send(JSONResponse({'error': 'username and password required'},
                                status=HTTPStatus.BAD_REQUEST))
        return

    created = await db.create_user(username, password)
    if not created:
        await send(JSONResponse({'error': 'Username already taken'},
                                status=HTTPStatus.CONFLICT))
        return

    token = secrets.token_urlsafe(32)
    SESSIONS[token] = username
    await send(JSONResponse({'ok': True, 'token': token}))


@app.route(methods=[HTTPMethod.POST], path='/login',
           middlewares=[json_body_mw])
async def handle_login(scope, receive, send):  # noqa: ARG001
    data = scope['json']
    username = str(data.get('username', '')).strip()
    password = str(data.get('password', ''))

    if not username or not password:
        await send(JSONResponse({'error': 'username and password required'},
                                status=HTTPStatus.BAD_REQUEST))
        return

    if not await db.verify_user(username, password):
        await send(JSONResponse({'error': 'Invalid credentials'},
                                status=HTTPStatus.UNAUTHORIZED))
        return

    token = secrets.token_urlsafe(32)
    SESSIONS[token] = username
    await send(JSONResponse({'ok': True, 'token': token}))


# /app — always public; the page JS checks auth via GET /tasks on load
@app.route(methods=[HTTPMethod.GET], path='/app')
async def handle_app_page(scope, receive, send):  # noqa: ARG001
    await send(Response(_load('index.html')))

# ---------------------------------------------------------------------------
# REST API — tasks (protected by require_auth interceptor above)
# ---------------------------------------------------------------------------

@app.route(methods=[HTTPMethod.GET], path='/tasks')
async def handle_get_tasks(scope, receive, send):  # noqa: ARG001
    tasks = await db.get_tasks(scope['user'])
    await send(JSONResponse(tasks))


@app.route(methods=[HTTPMethod.POST], path='/tasks',
           middlewares=[json_body_mw])
async def handle_create_task(scope, receive, send):
    title = str(scope['json'].get('title', '')).strip()
    if not title:
        await send(JSONResponse({'error': 'title is required'},
                                status=HTTPStatus.BAD_REQUEST))
        return
    task = await db.create_task(scope['user'], title)
    await send(JSONResponse(task, status=HTTPStatus.CREATED))


@app.route(methods=[HTTPMethod.PUT], path='/tasks/{task_id}',
           middlewares=[json_body_mw])
async def handle_update_task(scope, receive, send):
    try:
        task_id = int(scope['path_params']['task_id'])
    except (KeyError, ValueError):
        await send(JSONResponse({'error': 'invalid task id'},
                                status=HTTPStatus.BAD_REQUEST))
        return

    task = await db.update_task(
        scope['user'], task_id,
        scope['json'].get('title'),
        scope['json'].get('completed'),
    )
    if task is None:
        await send(JSONResponse({'error': 'Not found'},
                                status=HTTPStatus.NOT_FOUND))
        return
    await send(JSONResponse(task))


@app.route(methods=[HTTPMethod.DELETE], path='/tasks/{task_id}')
async def handle_delete_task(scope, receive, send):
    try:
        task_id = int(scope['path_params']['task_id'])
    except (KeyError, ValueError):
        await send(JSONResponse({'error': 'invalid task id'},
                                status=HTTPStatus.BAD_REQUEST))
        return

    if not await db.delete_task(scope['user'], task_id):
        await send(JSONResponse({'error': 'Not found'},
                                status=HTTPStatus.NOT_FOUND))
        return
    await send(JSONResponse({'ok': True}))


@app.route(methods=[HTTPMethod.POST], path='/logout')
async def handle_logout(scope, receive, send):
    SESSIONS.pop(scope.get('token', ''), None)
    await send(JSONResponse({'ok': True}))

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='BlackBull Simple Task Manager')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--cert', default=None, help='TLS certificate (enables HTTPS)')
    parser.add_argument('--key',  default=None, help='TLS private key')
    args = parser.parse_args()

    proto = 'https' if args.cert else 'http'
    logger.info('Listening on %s://localhost:%d', proto, args.port)

    try:
        asyncio.run(app.run(certfile=args.cert, keyfile=args.key, port=args.port))
    except KeyboardInterrupt:
        logger.info('Stopped.')

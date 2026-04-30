"""
Simple Task Manager
===================
REST + HTML task management application demonstrating BlackBull's event-driven
and middleware APIs together.

Cross-cutting concerns:
    Request logging  → @app.on('before_handler')  observer (fire-and-forget)
    Error wrapping   → error_mw per-route middleware (must wrap send)
    Bearer auth      → auth_mw per-route-group middleware (short-circuits to 401)
    JSON body        → json_body_mw per-route middleware (injects scope['json'])

Why @app.on for logging, not middleware?
    Logging must never block or abort the request.  Observers run in an
    independent asyncio.Task — they cannot short-circuit the handler chain,
    and any exception they raise is caught and logged rather than surfaced.
    Per-route middleware (auth, error handling) is still the right tool when
    the cross-cutting concern must run synchronously or needs to short-circuit.

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
# Middleware
# ---------------------------------------------------------------------------

async def error_mw(scope, receive, send, call_next):
    """Catch unhandled exceptions and return a JSON 500 response."""
    try:
        await call_next(scope, receive, send)
    except Exception as exc:
        logger.exception('Unhandled error')
        await send(JSONResponse({'error': str(exc)},
                                status=HTTPStatus.INTERNAL_SERVER_ERROR))


async def auth_mw(scope, receive, send, call_next):
    """Validate Bearer token from Authorization header; inject scope['user']."""
    auth = scope['headers'].get(b'authorization', b'')
    token = auth[7:].decode() if auth.startswith(b'Bearer ') else ''
    username = SESSIONS.get(token)
    if not username:
        await send(JSONResponse({'error': 'Unauthorized'},
                                status=HTTPStatus.UNAUTHORIZED))
        return
    scope['user'] = username
    scope['token'] = token
    await call_next(scope, receive, send)


async def priority_mw(scope, receive, send, call_next):
    """Read HTTP/2 priority hint and adjust downstream behaviour.

    scope['http2_priority'] is populated by BlackBull for HTTP/2 requests.
    For HTTP/1.1 it is absent; this middleware defaults gracefully.

    urgency 0-1  → high-priority: log the request prominently.
    urgency 2-7  → normal/background: no change.
    """
    hint = scope.get('http2_priority', {'urgency': 3, 'incremental': False})
    urgency = hint['urgency']
    scope['_priority_urgency'] = urgency

    if urgency <= 1:
        logger.info('HIGH-PRIORITY request u=%d: %s %s',
                    urgency, scope.get('method', '?'), scope.get('path', '?'))

    await call_next(scope, receive, send)


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
# Application + route groups
# ---------------------------------------------------------------------------

app = BlackBull()

public = app.group(middlewares=[error_mw])
api    = app.group(middlewares=[error_mw, priority_mw, auth_mw])


@app.on('before_handler')
async def log_request(event):
    """Observer: log every request — fire-and-forget, never short-circuits."""
    logger.info('%s %s', event.detail['method'], event.detail['path'])


@app.on('after_handler')
async def log_response(event):
    """Observer: log completion and any unhandled exception."""
    exc = event.detail.get('exception')
    if exc:
        logger.error('  → unhandled %s: %s', type(exc).__name__, exc)
    else:
        logger.info('  → OK')


@app.on_startup
async def startup():
    logger.info('Initialising database…')
    await db.init_db()
    logger.info('Database ready. Default user: admin / admin')

# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------

@public.route(methods=[HTTPMethod.GET], path='/')
async def handle_login_page(scope, receive, send):  # noqa: ARG001
    await send(Response(_load('login.html')))


@public.route(methods=[HTTPMethod.GET], path='/register')
async def handle_register_page(scope, receive, send):  # noqa: ARG001
    await send(Response(_load('register.html')))


@public.route(methods=[HTTPMethod.POST], path='/register',
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


@public.route(methods=[HTTPMethod.POST], path='/login',
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
@public.route(methods=[HTTPMethod.GET], path='/app')
async def handle_app_page(scope, receive, send):  # noqa: ARG001
    await send(Response(_load('index.html')))

# ---------------------------------------------------------------------------
# REST API — tasks (require Bearer auth)
# ---------------------------------------------------------------------------

@api.route(methods=[HTTPMethod.GET], path='/tasks')
async def handle_get_tasks(scope, receive, send):  # noqa: ARG001
    tasks = await db.get_tasks(scope['user'])
    await send(JSONResponse(tasks))


@api.route(methods=[HTTPMethod.POST], path='/tasks',
           middlewares=[json_body_mw])
async def handle_create_task(scope, receive, send):
    title = str(scope['json'].get('title', '')).strip()
    if not title:
        await send(JSONResponse({'error': 'title is required'},
                                status=HTTPStatus.BAD_REQUEST))
        return
    task = await db.create_task(scope['user'], title)
    await send(JSONResponse(task, status=HTTPStatus.CREATED))


@api.route(methods=[HTTPMethod.PUT], path='/tasks/{task_id}',
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


@api.route(methods=[HTTPMethod.DELETE], path='/tasks/{task_id}')
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


@api.route(methods=[HTTPMethod.POST], path='/logout')
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

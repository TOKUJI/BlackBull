"""
Simple Task Manager — minimal version
=====================================
Same functionality as ``app.py`` (login/register, CRUD tasks, Bearer auth)
but without using middleware or the event API.  Cross-cutting concerns are
handled by inline helper calls in each route:

    Bearer auth  → ``_authenticate(conn)`` called at the top of each
                   protected handler; rejects with 401 inline.
    JSON body    → ``_parse_json(receive, send)`` called at the top of
                   each POST / PUT handler.
    DB startup   → ``await db.init_db()`` from an explicit ``main()``
                   coroutine before ``app.run()``.

See ``app.py`` for the equivalent using BlackBull's middleware + event API.

Run:
    python app-simple.py [--port 8000]

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

SESSIONS: dict[str, str] = {}   # token → username

# ---------------------------------------------------------------------------
# Helpers — called inline from each handler
# ---------------------------------------------------------------------------

async def _parse_json(receive, send) -> dict | None:
    """Read + parse JSON body.  Returns ``None`` after sending 400 on failure."""
    raw = await read_body(receive)
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        await send(JSONResponse({'error': 'Invalid JSON'},
                                status=HTTPStatus.BAD_REQUEST))
        return None


async def _authenticate(conn, send) -> tuple[str | None, str]:
    """Validate Bearer token.  Returns ``(username, token)`` or ``(None, '')`` after sending 401.

    *conn* is the native :class:`~blackbull.connection.Connection`."""
    auth = conn.headers.get(b'authorization', b'')
    token = auth[7:].decode() if auth.startswith(b'Bearer ') else ''
    username = SESSIONS.get(token)
    if username is None:
        await send(JSONResponse({'error': 'Unauthorized'},
                                status=HTTPStatus.UNAUTHORIZED))
        return None, ''
    return username, token

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = BlackBull()

# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------

@app.route(methods=[HTTPMethod.GET], path='/')
async def handle_login_page(conn, receive, send):  # noqa: ARG001
    await send(Response(_load('login.html')))


@app.route(methods=[HTTPMethod.GET], path='/register')
async def handle_register_page(conn, receive, send):  # noqa: ARG001
    await send(Response(_load('register.html')))


@app.route(methods=[HTTPMethod.POST], path='/register')
async def handle_register(conn, receive, send):
    data = await _parse_json(receive, send)
    if data is None:
        return
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


@app.route(methods=[HTTPMethod.POST], path='/login')
async def handle_login(conn, receive, send):
    data = await _parse_json(receive, send)
    if data is None:
        return
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


@app.route(methods=[HTTPMethod.GET], path='/app')
async def handle_app_page(conn, receive, send):  # noqa: ARG001
    await send(Response(_load('index.html')))

# ---------------------------------------------------------------------------
# Protected REST API — Bearer token required
# ---------------------------------------------------------------------------

@app.route(methods=[HTTPMethod.GET], path='/tasks')
async def handle_get_tasks(conn, receive, send):  # noqa: ARG001
    user, _ = await _authenticate(conn, send)
    if user is None:
        return
    tasks = await db.get_tasks(user)
    await send(JSONResponse(tasks))


@app.route(methods=[HTTPMethod.POST], path='/tasks')
async def handle_create_task(conn, receive, send):
    user, _ = await _authenticate(conn, send)
    if user is None:
        return
    data = await _parse_json(receive, send)
    if data is None:
        return
    title = str(data.get('title', '')).strip()
    if not title:
        await send(JSONResponse({'error': 'title is required'},
                                status=HTTPStatus.BAD_REQUEST))
        return
    task = await db.create_task(user, title)
    await send(JSONResponse(task, status=HTTPStatus.CREATED))


@app.route(methods=[HTTPMethod.PUT], path='/tasks/{task_id}')
async def handle_update_task(conn, receive, send):
    user, _ = await _authenticate(conn, send)
    if user is None:
        return
    data = await _parse_json(receive, send)
    if data is None:
        return
    try:
        task_id = int(conn.path_params['task_id'])
    except (KeyError, ValueError):
        await send(JSONResponse({'error': 'invalid task id'},
                                status=HTTPStatus.BAD_REQUEST))
        return

    task = await db.update_task(user, task_id,
                                data.get('title'),
                                data.get('completed'))
    if task is None:
        await send(JSONResponse({'error': 'Not found'},
                                status=HTTPStatus.NOT_FOUND))
        return
    await send(JSONResponse(task))


@app.route(methods=[HTTPMethod.DELETE], path='/tasks/{task_id}')
async def handle_delete_task(conn, receive, send):
    user, _ = await _authenticate(conn, send)
    if user is None:
        return
    try:
        task_id = int(conn.path_params['task_id'])
    except (KeyError, ValueError):
        await send(JSONResponse({'error': 'invalid task id'},
                                status=HTTPStatus.BAD_REQUEST))
        return

    if not await db.delete_task(user, task_id):
        await send(JSONResponse({'error': 'Not found'},
                                status=HTTPStatus.NOT_FOUND))
        return
    await send(JSONResponse({'ok': True}))


@app.route(methods=[HTTPMethod.POST], path='/logout')
async def handle_logout(conn, receive, send):
    _, token = await _authenticate(conn, send)
    if not token:
        return
    SESSIONS.pop(token, None)
    await send(JSONResponse({'ok': True}))

# ---------------------------------------------------------------------------
# Entry point — DB init then serve
# ---------------------------------------------------------------------------

async def main(certfile, keyfile, port):
    logger.info('Initialising database…')
    await db.init_db()
    logger.info('Database ready. Default user: admin / admin')
    await app.run(certfile=certfile, keyfile=keyfile, port=port)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='BlackBull Simple Task Manager (minimal)')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--cert', default=None, help='TLS certificate (enables HTTPS)')
    parser.add_argument('--key',  default=None, help='TLS private key')
    args = parser.parse_args()

    proto = 'https' if args.cert else 'http'
    logger.info('Listening on %s://localhost:%d', proto, args.port)

    try:
        asyncio.run(main(args.cert, args.key, args.port))
    except KeyboardInterrupt:
        logger.info('Stopped.')

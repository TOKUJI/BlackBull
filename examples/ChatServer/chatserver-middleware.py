"""
Chat Server (Middleware style)
==============================
Same functionality as chatserver-flasklike.py, but uses BlackBull's middleware
API to eliminate cross-cutting boilerplate from route handlers:

  - ``session_mw``   — reads the session cookie, rejects unauthenticated
                        requests with 401, and injects ``scope['session']``
  - ``json_body_mw`` — reads and parses the request body, rejects malformed
                        JSON with 400, and injects ``scope['json']``

Protected routes are registered through a ``RouteGroup`` (``app.group()``) so
``session_mw`` is applied automatically without listing it on every decorator.

Run (plain HTTP/1.1 + Long Polling / WebSocket only):
    python chatserver-middleware.py

Run (HTTPS + HTTP/2, enables SSE):
    python chatserver-middleware.py --cert server.crt --key server.key

Generate a self-signed certificate for testing:
    openssl req -x509 -newkey rsa:4096 -keyout server.key \\
        -out server.crt -days 365 -nodes -subj '/CN=localhost'
"""

import argparse
import asyncio
import json
import logging
import pathlib
import secrets
import time
import uuid
from http import HTTPMethod, HTTPStatus

from blackbull import (
    BlackBull,
    JSONResponse,
    Response,
    WebSocketResponse,
    cookie_header,
    parse_cookies,
    read_body,
)
from blackbull.utils import Scheme

_TEMPLATES = pathlib.Path(__file__).parent / 'templates'


def _load(name: str) -> bytes:
    return (_TEMPLATES / name).read_bytes()


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')

# ---------------------------------------------------------------------------
# Server-side state  (identical to chatserver-flasklike.py)
# ---------------------------------------------------------------------------

MAX_MESSAGES = 100
SESSION_TTL = 30 * 60
DISCONNECT_TTL = 5 * 60
POLL_TIMEOUT = 25


class Session:
    def __init__(self, session_id: str, username: str, comm_type: str):
        self.session_id = session_id
        self.username = username
        self.comm_type = comm_type
        self.last_seen = time.monotonic()
        self.connected = False
        self.disconnect_time: float | None = None
        self.queue: asyncio.Queue = asyncio.Queue()


class ChatState:
    def __init__(self):
        self.sessions: dict[str, Session] = {}
        self.messages: list[dict] = []
        self._lock = asyncio.Lock()

    def _expired(self, session: Session) -> bool:
        now = time.monotonic()
        if now - session.last_seen > SESSION_TTL:
            return True
        if not session.connected and session.disconnect_time is not None:
            if now - session.disconnect_time > DISCONNECT_TTL:
                return True
        return False

    def get(self, session_id: str) -> Session | None:
        session = self.sessions.get(session_id)
        if session is None or self._expired(session):
            if session is not None:
                del self.sessions[session_id]
            return None
        session.last_seen = time.monotonic()
        return session

    async def create(self, username: str, comm_type: str) -> Session:
        session_id = secrets.token_urlsafe(32)
        session = Session(session_id, username, comm_type)
        async with self._lock:
            self.sessions[session_id] = session
        return session

    async def remove(self, session_id: str) -> Session | None:
        async with self._lock:
            return self.sessions.pop(session_id, None)

    async def add_message(self, session: Session, text: str) -> dict:
        msg = {
            'id': str(uuid.uuid4()),
            'username': session.username,
            'message': text,
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        }
        async with self._lock:
            self.messages.append(msg)
            if len(self.messages) > MAX_MESSAGES:
                self.messages = self.messages[-MAX_MESSAGES:]
        return msg

    async def broadcast(self, event: dict) -> None:
        async with self._lock:
            sessions = list(self.sessions.values())
        for s in sessions:
            if s.connected:
                await s.queue.put(event)

    def participants(self) -> list[dict]:
        return [
            {'session_id': s.session_id, 'username': s.username}
            for s in self.sessions.values()
            if s.connected
        ]


state = ChatState()


def _sse_encode(event: dict) -> bytes:
    return b'data: ' + json.dumps(event).encode() + b'\n\n'

# ---------------------------------------------------------------------------
# Middleware definitions
# ---------------------------------------------------------------------------

async def session_mw(scope, receive, send, call_next):
    """Resolve the session cookie → Session; reject with 401 if missing."""
    cookies = parse_cookies(scope)
    sid = cookies.get('session_id', '')
    session = state.get(sid) if sid else None
    if session is None:
        await send(JSONResponse({'error': 'Unauthorized'}, status=HTTPStatus.UNAUTHORIZED))
        return
    scope['session'] = session
    await call_next(scope, receive, send)


async def json_body_mw(scope, receive, send, call_next):
    """Read and parse the request body as JSON; reject with 400 on error."""
    raw = await read_body(receive)
    try:
        scope['json'] = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        await send(JSONResponse({'error': 'Invalid JSON'}, status=HTTPStatus.BAD_REQUEST))
        return
    await call_next(scope, receive, send)

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = BlackBull()
protected = app.group(middlewares=[session_mw])


@app.on('request_received')
async def log_request(event):
    """Observer: log every HTTP request — fire-and-forget, never short-circuits."""
    logger.info('%s %s', event.detail['method'], event.detail['path'])


@app.on('websocket_connected')
async def on_ws_connected(event):
    """Observer: log each new WebSocket connection."""
    logger.info('WebSocket connected id=%s path=%s',
                event.detail['connection_id'], event.detail['path'])


@app.on('websocket_disconnected')
async def on_ws_disconnected(event):
    """Observer: log WebSocket disconnections (fires even on abrupt close)."""
    logger.info('WebSocket disconnected id=%s code=%s',
                event.detail['connection_id'], event.detail['code'])


@app.route(methods=[HTTPMethod.GET], path='/')
async def handle_login_page(scope, receive, send):  # noqa: ARG001
    await send(Response(_load('login.html')))


@app.route(methods=[HTTPMethod.POST], path='/login', middlewares=[json_body_mw])
async def handle_do_login(scope, receive, send):
    data = scope['json']

    username = str(data.get('username', '')).strip()
    comm_type = str(data.get('method', 'poll'))

    if not username:
        await send(JSONResponse({'error': 'Username required'}, status=HTTPStatus.BAD_REQUEST))
        return
    if comm_type not in ('websocket', 'sse', 'poll'):
        await send(JSONResponse({'error': 'Unsupported method'}, status=HTTPStatus.BAD_REQUEST))
        return

    cookies = parse_cookies(scope)
    existing_sid = cookies.get('session_id', '')
    session = state.get(existing_sid) if existing_sid else None

    if session is None:
        session = await state.create(username, comm_type)
    else:
        session.comm_type = comm_type

    extra = [
        cookie_header('session_id', session.session_id, http_only=True),
        cookie_header('chat_method', comm_type, http_only=False),
    ]
    await send(JSONResponse({'ok': True}, headers=extra))


@app.route(methods=[HTTPMethod.GET], path='/chat')
async def handle_chat_page(scope, receive, send):
    # Redirect to login if no valid session; 302 differs from 401 so this
    # route is intentionally outside the `protected` group.
    cookies = parse_cookies(scope)
    sid = cookies.get('session_id', '')
    if not sid or state.get(sid) is None:
        await send({'type': 'http.response.start', 'status': 302,
                    'headers': [(b'location', b'/')]})
        await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
        return
    await send(Response(_load('chat.html')))


@protected.route(methods=[HTTPMethod.POST], path='/send', middlewares=[json_body_mw])
async def handle_send(scope, receive, send):
    session: Session = scope['session']
    data: dict = scope['json']

    text = str(data.get('message', '')).strip()
    if not text:
        await send(JSONResponse({'error': 'Empty message'}, status=HTTPStatus.BAD_REQUEST))
        return

    msg = await state.add_message(session, text)
    await state.broadcast({'type': 'message', 'payload': msg})
    await send(JSONResponse({'ok': True}))


@protected.route(methods=[HTTPMethod.POST], path='/logout')
async def handle_logout(scope, receive, send):  # noqa: ARG001
    session: Session = scope['session']
    removed = await state.remove(session.session_id)
    if removed:
        leave_event = {
            'type': 'user_leave',
            'payload': {'username': removed.username, 'participants': state.participants()},
        }
        await state.broadcast(leave_event)

    expire = 'Thu, 01 Jan 1970 00:00:00 GMT'
    await send(JSONResponse({'ok': True}, headers=[
        (b'set-cookie', f'session_id=; Path=/; Expires={expire}; HttpOnly; SameSite=Lax'.encode()),
        (b'set-cookie', f'chat_method=; Path=/; Expires={expire}; SameSite=Lax'.encode()),
    ]))


@protected.route(methods=[HTTPMethod.GET], path='/sse')
async def handle_sse(scope, receive, send):  # noqa: ARG001
    if scope.get('http_version') != '2':
        await send(JSONResponse(
            {'error': 'SSE requires HTTP/2. Connect via HTTPS with a client that supports HTTP/2.'},
            status=HTTPStatus.BAD_REQUEST,
        ))
        return

    session: Session = scope['session']

    await send({
        'type': 'http.response.start',
        'status': 200,
        'headers': [
            (b'content-type', b'text/event-stream'),
            (b'cache-control', b'no-cache'),
            (b'x-accel-buffering', b'no'),
        ],
    })

    session.connected = True
    session.disconnect_time = None

    history = {'type': 'history', 'payload': list(state.messages)}
    await send({'type': 'http.response.body', 'body': _sse_encode(history), 'more_body': True})

    join_event = {
        'type': 'user_join',
        'payload': {'username': session.username, 'participants': state.participants()},
    }
    await state.broadcast(join_event)

    try:
        while True:
            try:
                evt = await asyncio.wait_for(session.queue.get(), timeout=25)
                await send({'type': 'http.response.body', 'body': _sse_encode(evt), 'more_body': True})
            except asyncio.TimeoutError:
                await send({'type': 'http.response.body', 'body': b': keepalive\n\n', 'more_body': True})
    except Exception:
        pass
    finally:
        session.connected = False
        session.disconnect_time = time.monotonic()
        leave_event = {
            'type': 'user_leave',
            'payload': {'username': session.username, 'participants': state.participants()},
        }
        await state.broadcast(leave_event)
        await send({'type': 'http.response.body', 'body': b'', 'more_body': False})


@protected.route(methods=[HTTPMethod.GET], path='/poll')
async def handle_poll(scope, receive, send):  # noqa: ARG001
    session: Session = scope['session']

    was_connected = session.connected
    session.connected = True
    session.disconnect_time = None

    events = []

    if not was_connected:
        events.append({'type': 'history', 'payload': list(state.messages)})
        join_event = {
            'type': 'user_join',
            'payload': {'username': session.username, 'participants': state.participants()},
        }
        await state.broadcast(join_event)
        events.append(join_event)

    while not session.queue.empty():
        events.append(session.queue.get_nowait())

    if not events:
        try:
            evt = await asyncio.wait_for(session.queue.get(), timeout=POLL_TIMEOUT)
            events.append(evt)
            while not session.queue.empty():
                events.append(session.queue.get_nowait())
        except asyncio.TimeoutError:
            pass

    session.last_seen = time.monotonic()
    await send(JSONResponse(events))


@app.route(methods=[HTTPMethod.GET], path='/ws', scheme=Scheme.websocket)
async def handle_websocket(scope, receive, send):
    # WebSocket auth: close with 4401 instead of 401, so it's handled inline
    # rather than via session_mw (which sends HTTP 401, invalid for WS upgrade).
    event = await receive()
    if event.get('type') != 'websocket.connect':
        return

    cookies = parse_cookies(scope)
    sid = cookies.get('session_id', '')
    session = state.get(sid) if sid else None
    if session is None:
        await send({'type': 'websocket.close', 'code': 4401})
        return

    await send({'type': 'websocket.accept'})
    session.connected = True
    session.disconnect_time = None

    history = {'type': 'history', 'payload': list(state.messages)}
    await send(WebSocketResponse(history))

    join_event = {
        'type': 'user_join',
        'payload': {'username': session.username, 'participants': state.participants()},
    }
    await state.broadcast(join_event)

    async def _sender():
        while session.connected:
            try:
                evt = await asyncio.wait_for(session.queue.get(), timeout=30)
                await send(WebSocketResponse(evt))
            except asyncio.TimeoutError:
                pass
            except Exception:
                break

    async def _receiver():
        while True:
            evt = await receive()
            if evt.get('type', '') == 'websocket.disconnect':
                break

    try:
        await asyncio.gather(_sender(), _receiver())
    finally:
        session.connected = False
        session.disconnect_time = time.monotonic()
        leave_event = {
            'type': 'user_leave',
            'payload': {'username': session.username, 'participants': state.participants()},
        }
        await state.broadcast(leave_event)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='BlackBull Chat Server (Middleware)')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--cert', default=None, help='TLS certificate file (enables HTTP/2 + SSE)')
    parser.add_argument('--key',  default=None, help='TLS private key file')
    args = parser.parse_args()

    proto = 'https' if args.cert else 'http'
    logger.info('Listening on %s://localhost:%d', proto, args.port)
    if not args.cert:
        logger.info('TLS not configured — SSE (HTTP/2) unavailable; WebSocket and Long Polling work.')

    try:
        asyncio.run(app.run(certfile=args.cert, keyfile=args.key, port=args.port))
    except KeyboardInterrupt:
        logger.info('Stopped.')

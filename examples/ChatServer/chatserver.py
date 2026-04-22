"""
Chat Server
===========
Multi-user real-time chat supporting WebSocket, SSE, and Long Polling.

Communication method ↔ HTTP version mapping:
    WebSocket    → HTTP/1.1 (Upgrade mechanism)
    SSE          → HTTP/2   (stream multiplexing; needs TLS)
    Long Polling → HTTP/1.1

Run (plain HTTP/1.1 + Long Polling / WebSocket only):
    python chatserver.py

Run (HTTPS + HTTP/2, enables SSE):
    python chatserver.py --cert server.crt --key server.key

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
from http import HTTPStatus

from blackbull import WebSocketResponse
from blackbull.server.server import ASGIServer

_TEMPLATES = pathlib.Path(__file__).parent / 'templates'


def _load(name: str) -> bytes:
    return (_TEMPLATES / name).read_bytes()

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')

# ---------------------------------------------------------------------------
# Server-side state
# ---------------------------------------------------------------------------

MAX_MESSAGES = 100
SESSION_TTL = 30 * 60      # 30 min inactivity → expire
DISCONNECT_TTL = 5 * 60    # 5 min after disconnect → expire
POLL_TIMEOUT = 25           # long-poll hold time (seconds)


class Session:
    def __init__(self, session_id: str, username: str, comm_type: str):
        self.session_id = session_id
        self.username = username
        self.comm_type = comm_type          # 'websocket' | 'sse' | 'poll'
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_cookie(scope) -> dict[str, str]:
    raw = scope['headers'].get(b'cookie')
    result = {}
    if not raw:
        return result
    for part in raw.split(b';'):
        k, _, v = part.strip().partition(b'=')
        result[k.strip().decode(errors='replace')] = v.strip().decode(errors='replace')
    return result


def _session_from_scope(scope) -> Session | None:
    cookies = _parse_cookie(scope)
    sid = cookies.get('session_id', '')
    return state.get(sid) if sid else None


async def _read_body(receive) -> bytes:
    body = b''
    while True:
        event = await receive()
        body += event.get('body', b'')
        if not event.get('more_body', False):
            break
    return body


def _set_cookie_header(name: str, value: str, path: str = '/',
                       http_only: bool = True) -> tuple[bytes, bytes]:
    flags = '; HttpOnly' if http_only else ''
    return (b'set-cookie', f'{name}={value}; Path={path}{flags}; SameSite=Lax'.encode())


async def _send_html(send, html: bytes, status: HTTPStatus = HTTPStatus.OK,
                     extra_headers: list | None = None):
    headers = [(b'content-type', b'text/html; charset=utf-8')]
    if extra_headers:
        headers.extend(extra_headers)
    await send({
        'type': 'http.response.start',
        'status': status.value,
        'headers': headers,
    })
    await send({'type': 'http.response.body', 'body': html, 'more_body': False})


async def _send_json(send, data, status: HTTPStatus = HTTPStatus.OK,
                     extra_headers: list | None = None):
    body = json.dumps(data).encode()
    headers = [(b'content-type', b'application/json')]
    if extra_headers:
        headers.extend(extra_headers)
    await send({
        'type': 'http.response.start',
        'status': status.value,
        'headers': headers,
    })
    await send({'type': 'http.response.body', 'body': body, 'more_body': False})


def _sse_encode(event: dict) -> bytes:
    return b'data: ' + json.dumps(event).encode() + b'\n\n'

# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

async def handle_login_page(scope, receive, send):  # noqa: ARG001
    await _send_html(send, _load('login.html'))


async def handle_do_login(scope, receive, send):
    body = await _read_body(receive)
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        await _send_json(send, {'error': 'Invalid JSON'}, HTTPStatus.BAD_REQUEST)
        return

    username = str(data.get('username', '')).strip()
    comm_type = str(data.get('method', 'poll'))

    if not username:
        await _send_json(send, {'error': 'Username required'}, HTTPStatus.BAD_REQUEST)
        return
    if comm_type not in ('websocket', 'sse', 'poll'):
        await _send_json(send, {'error': 'Unsupported method'}, HTTPStatus.BAD_REQUEST)
        return

    # Check for existing valid session (reconnect)
    cookies = _parse_cookie(scope)
    existing_sid = cookies.get('session_id', '')
    session = state.get(existing_sid) if existing_sid else None

    if session is None:
        session = await state.create(username, comm_type)
    else:
        session.comm_type = comm_type

    extra = [
        _set_cookie_header('session_id', session.session_id, http_only=True),
        _set_cookie_header('chat_method', comm_type, http_only=False),
    ]
    await _send_json(send, {'ok': True}, extra_headers=extra)


async def handle_chat_page(scope, receive, send):
    session = _session_from_scope(scope)
    if session is None:
        headers = [(b'location', b'/')]
        await send({'type': 'http.response.start', 'status': 302, 'headers': headers})
        await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
        return
    await _send_html(send, _load('chat.html'))


async def handle_send(scope, receive, send):
    session = _session_from_scope(scope)
    if session is None:
        await _send_json(send, {'error': 'Unauthorized'}, HTTPStatus.UNAUTHORIZED)
        return

    body = await _read_body(receive)
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        await _send_json(send, {'error': 'Invalid JSON'}, HTTPStatus.BAD_REQUEST)
        return

    text = str(data.get('message', '')).strip()
    if not text:
        await _send_json(send, {'error': 'Empty message'}, HTTPStatus.BAD_REQUEST)
        return

    msg = await state.add_message(session, text)
    await state.broadcast({'type': 'message', 'payload': msg})
    await _send_json(send, {'ok': True})


async def handle_websocket(scope, receive, send):
    # Receive websocket.connect
    event = await receive()
    if event.get('type') != 'websocket.connect':
        return

    session = _session_from_scope(scope)
    if session is None:
        await send({'type': 'websocket.close', 'code': 4401})
        return

    await send({'type': 'websocket.accept'})
    session.connected = True
    session.disconnect_time = None

    # Send history + join event
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
                # Send a ping-like keepalive (empty message won't confuse client)
                pass
            except Exception:
                break

    async def _receiver():
        while True:
            event = await receive()
            t = event.get('type', '')
            if t == 'websocket.disconnect':
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


async def handle_sse(scope, receive, send):
    if scope.get('http_version') != '2':
        await _send_json(
            send,
            {'error': 'SSE requires HTTP/2. Connect via HTTPS with a client that supports HTTP/2.'},
            HTTPStatus.BAD_REQUEST,
        )
        return

    session = _session_from_scope(scope)
    if session is None:
        await _send_json(send, {'error': 'Unauthorized'}, HTTPStatus.UNAUTHORIZED)
        return

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

    # Send history
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
                # Send SSE comment as keepalive
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


async def handle_poll(scope, receive, send):
    session = _session_from_scope(scope)
    if session is None:
        await _send_json(send, {'error': 'Unauthorized'}, HTTPStatus.UNAUTHORIZED)
        return

    was_connected = session.connected
    session.connected = True
    session.disconnect_time = None

    events = []

    if not was_connected:
        # First connection: send history + join
        events.append({'type': 'history', 'payload': list(state.messages)})
        join_event = {
            'type': 'user_join',
            'payload': {'username': session.username, 'participants': state.participants()},
        }
        await state.broadcast(join_event)
        events.append(join_event)

    # Drain any queued events immediately
    while not session.queue.empty():
        events.append(session.queue.get_nowait())

    if not events:
        # Hold the connection waiting for the next event
        try:
            evt = await asyncio.wait_for(session.queue.get(), timeout=POLL_TIMEOUT)
            events.append(evt)
            # Drain any extras that arrived while we were waiting
            while not session.queue.empty():
                events.append(session.queue.get_nowait())
        except asyncio.TimeoutError:
            pass

    session.last_seen = time.monotonic()
    await _send_json(send, events)

async def handle_logout(scope, receive, send):  # noqa: ARG001
    cookies = _parse_cookie(scope)
    sid = cookies.get('session_id', '')
    if sid:
        session = await state.remove(sid)
        if session:
            leave_event = {
                'type': 'user_leave',
                'payload': {'username': session.username, 'participants': state.participants()},
            }
            await state.broadcast(leave_event)

    expire = 'Thu, 01 Jan 1970 00:00:00 GMT'
    await _send_json(send, {'ok': True}, extra_headers=[
        (b'set-cookie', f'session_id=; Path=/; Expires={expire}; HttpOnly; SameSite=Lax'.encode()),
        (b'set-cookie', f'chat_method=; Path=/; Expires={expire}; SameSite=Lax'.encode()),
    ])


# ---------------------------------------------------------------------------
# ASGI application
# ---------------------------------------------------------------------------

ROUTES: dict[tuple[str, str], object] = {
    ('GET',  '/'):        handle_login_page,
    ('POST', '/login'):   handle_do_login,
    ('GET',  '/chat'):    handle_chat_page,
    ('POST', '/send'):    handle_send,
    ('POST', '/logout'):  handle_logout,
    ('GET',  '/sse'):     handle_sse,
    ('GET',  '/poll'):    handle_poll,
}


async def app(scope, receive, send):
    t = scope.get('type')

    if t == 'lifespan':
        while True:
            event = await receive()
            if event['type'] == 'lifespan.startup':
                logger.info('Chat server starting up')
                await send({'type': 'lifespan.startup.complete'})
            elif event['type'] == 'lifespan.shutdown':
                logger.info('Chat server shutting down')
                await send({'type': 'lifespan.shutdown.complete'})
                return

    elif t == 'websocket':
        await handle_websocket(scope, receive, send)

    elif t == 'http':
        method = scope.get('method', 'GET').upper()
        path = scope.get('path', '/')
        handler = ROUTES.get((method, path))
        if handler is None:
            await _send_json(send, {'error': 'Not Found'}, HTTPStatus.NOT_FOUND)
        else:
            await handler(scope, receive, send)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='BlackBull Chat Server')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--cert', default=None, help='TLS certificate file (enables HTTP/2 + SSE)')
    parser.add_argument('--key',  default=None, help='TLS private key file')
    args = parser.parse_args()

    server = ASGIServer(app, certfile=args.cert, keyfile=args.key)
    server.open_socket(args.port)

    proto = 'https' if args.cert else 'http'
    logger.info('Listening on %s://localhost:%d', proto, args.port)
    if not args.cert:
        logger.info('TLS not configured — SSE (HTTP/2) unavailable; WebSocket and Long Polling work.')

    try:
        asyncio.run(server.run(port=args.port))
    except KeyboardInterrupt:
        logger.info('Stopped.')

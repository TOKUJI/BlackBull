"""
Chat Server
===========
Multi-user real-time chat supporting WebSocket, SSE, and Long Polling so
authors can compare the three transports side-by-side in one app.

Communication method ↔ HTTP version mapping:
    WebSocket    → HTTP/1.1 (Upgrade) or HTTP/2 (RFC 8441 Extended CONNECT via TLS)
    SSE          → HTTP/2   (stream multiplexing; needs TLS)
    Long Polling → HTTP/1.1 or HTTP/2

The example uses the framework's middleware pipeline so the protocol code stays
focused on the protocol itself:

  - ``session_mw``   — inline native signed-cookie sessions (HMAC-SHA256);
    handlers read / write ``conn.state['session']`` and it re-emits Set-Cookie
    when the session was mutated (a self-contained replacement for the external
    ``blackbull-session`` package, updated for the native Connection model)
  - :class:`blackbull.middleware.Compression` — gzips JSON / HTML responses
    when the client accepts them; streaming responses (SSE) pass through
  - ``auth_mw``      — resolves the cookie's opaque session id to the rich
    server-side :class:`ChatSession` record and injects ``conn.state['chat_session']``
  - ``json_body_mw`` — parses the request body as JSON, injects ``conn.state['json']``

Protected routes are registered through a ``RouteGroup`` (``app.group()``) so
``auth_mw`` is applied automatically without listing it on every decorator.

Run (plain HTTP/1.1 — WebSocket and Long Polling; no SSE):
    python chatserver.py

Run (HTTPS + HTTP/2 — all three modes including SSE; WebSocket via RFC 8441):
    python chatserver.py --cert server.crt --key server.key

Generate a self-signed certificate for testing:
    openssl req -x509 -newkey rsa:4096 -keyout server.key \\
        -out server.crt -days 365 -nodes -subj '/CN=localhost'
"""

import argparse
import asyncio
import json
import logging
import os
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
    read_body,
)
from blackbull.middleware import Compression
from blackbull.utils import Scheme

import base64
import hashlib
import hmac

_TEMPLATES = pathlib.Path(__file__).parent / 'templates'


def _load(name: str) -> bytes:
    return (_TEMPLATES / name).read_bytes()


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')

# ---------------------------------------------------------------------------
# Server-side state
# ---------------------------------------------------------------------------

MAX_MESSAGES = 100
SESSION_TTL = 30 * 60
DISCONNECT_TTL = 5 * 60
POLL_TIMEOUT = 25


class ChatSession:
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
        self.sessions: dict[str, ChatSession] = {}
        self.messages: list[dict] = []
        self._lock = asyncio.Lock()

    def _expired(self, session: ChatSession) -> bool:
        now = time.monotonic()
        if now - session.last_seen > SESSION_TTL:
            return True
        if not session.connected and session.disconnect_time is not None:
            if now - session.disconnect_time > DISCONNECT_TTL:
                return True
        return False

    def get(self, session_id: str) -> ChatSession | None:
        session = self.sessions.get(session_id)
        if session is None or self._expired(session):
            if session is not None:
                del self.sessions[session_id]
            return None
        session.last_seen = time.monotonic()
        return session

    async def create(self, username: str, comm_type: str) -> ChatSession:
        session_id = secrets.token_urlsafe(32)
        session = ChatSession(session_id, username, comm_type)
        async with self._lock:
            self.sessions[session_id] = session
        return session

    async def remove(self, session_id: str) -> ChatSession | None:
        async with self._lock:
            return self.sessions.pop(session_id, None)

    async def add_message(self, session: ChatSession, text: str) -> dict:
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
# Signed-cookie session (native Connection)
# ---------------------------------------------------------------------------
# A self-contained, native replacement for the external ``blackbull-session``
# package: BlackBull threads a typed :class:`Connection` (Sprint 80), so a
# middleware receives ``conn`` and shares per-request data through ``conn.state``
# — not by mutating an ASGI scope dict. The session payload is HMAC-SHA256
# signed so a client cannot forge it.

_SESSION_COOKIE = 'session'
_SESSION_SECRET = os.environ.get(
    # Demo secret — set BB_SESSION_SECRET in production.  DO NOT ship this
    # inline default: anyone who can read the source can forge a session.
    'BB_SESSION_SECRET', 'chat-demo-secret-not-for-production').encode()


class _SessionDict(dict):
    """A dict that records whether it was mutated, so the session middleware
    re-emits ``Set-Cookie`` only when the handler actually changed it."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.modified = False

    def __setitem__(self, key, value) -> None:
        super().__setitem__(key, value)
        self.modified = True

    def __delitem__(self, key) -> None:
        super().__delitem__(key)
        self.modified = True

    def clear(self) -> None:
        super().clear()
        self.modified = True

    # `modified` is transient dirty-tracking, deliberately excluded from
    # equality: a session's identity is its *content*, not whether this
    # particular instance has been written to. Define __eq__ explicitly
    # (delegating to dict) so that exclusion is an intentional decision, not
    # an accident of subclassing.
    def __eq__(self, other) -> bool:
        return dict.__eq__(self, other)

    __hash__ = None   # dicts (and thus sessions) are unhashable


def _b64(raw: bytes) -> str:
    # Strip '=' padding so the token is a clean cookie value.
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def _sign(payload: str) -> str:
    return _b64(hmac.new(_SESSION_SECRET, payload.encode(), hashlib.sha256).digest())


def _encode_session(data: dict) -> str:
    payload = _b64(json.dumps(data).encode())
    return f'{payload}.{_sign(payload)}'


def _decode_session(raw: str) -> dict:
    try:
        payload, sig = raw.split('.', 1)
        if not hmac.compare_digest(_sign(payload), sig):
            return {}   # tampered or wrong secret → empty session
        return json.loads(_unb64(payload))
    except (ValueError, json.JSONDecodeError):
        return {}


async def session_mw(conn, receive, send, call_next):
    """Load the signed ``session`` cookie into ``conn.state['session']`` and
    re-emit ``Set-Cookie`` on the response when a handler mutated it.

    WebSocket has no ``http.response.start`` to attach the cookie to, so it gets
    a read-only session load with no Set-Cookie.
    """
    raw = conn.cookies.get(_SESSION_COOKIE, '')
    session = _SessionDict(_decode_session(raw) if raw else {})
    conn.state['session'] = session

    if conn.type == 'websocket':
        await call_next(conn, receive, send)
        return

    async def wrapped_send(event):
        if (isinstance(event, dict)
                and event.get('type') == 'http.response.start'
                and session.modified):
            headers = list(event.get('headers', []))
            if session:
                value = _encode_session(dict(session))
                cookie = (f'{_SESSION_COOKIE}={value}; Path=/; '
                          'HttpOnly; SameSite=Lax')
            else:
                # Emptied session → evict the cookie from the browser.
                cookie = (f'{_SESSION_COOKIE}=; Path=/; HttpOnly; '
                          'SameSite=Lax; Max-Age=0')
            headers.append((b'set-cookie', cookie.encode()))
            event = {**event, 'headers': headers}
        await send(event)

    await call_next(conn, receive, wrapped_send)


# ---------------------------------------------------------------------------
# Middleware definitions
# ---------------------------------------------------------------------------

async def auth_mw(conn, receive, send, call_next):
    """Resolve the signed-cookie session id to a rich ``ChatSession`` record.

    ``session_mw`` (above) has already populated ``conn.state['session']`` (a
    dict) by decoding the signed cookie.  This middleware reads its ``id``
    field, looks up the server-side state, and injects
    ``conn.state['chat_session']`` for handlers — or rejects:

    HTTP:      sends 401 JSON response.
    WebSocket: consumes the connect event, then closes with 4401.
    """
    sid = conn.state.get('session', {}).get('id', '')
    chat_session = state.get(sid) if sid else None

    if conn.type == 'websocket':
        event = await receive()
        if event.get('type') != 'websocket.connect':
            return
        if chat_session is None:
            await send({'type': 'websocket.close', 'code': 4401})
            return
    elif chat_session is None:
        await send(JSONResponse({'error': 'Unauthorized'}, status=HTTPStatus.UNAUTHORIZED))
        return

    conn.state['chat_session'] = chat_session
    await call_next(conn, receive, send)


async def json_body_mw(conn, receive, send, call_next):
    """Parse JSON body → conn.state['json']; wrap send so bare dicts become JSON responses.

    Rejects malformed JSON with 400.  Handlers that pass a bare dict (no ``type``
    key) to ``send`` receive an automatic ``JSONResponse``; handlers that need a
    custom status or headers still use ``JSONResponse(...)`` explicitly.
    """
    raw = await read_body(receive)
    try:
        conn.state['json'] = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        await send(JSONResponse({'error': 'Invalid JSON'}, status=HTTPStatus.BAD_REQUEST))
        return

    async def json_send(event):
        if isinstance(event, dict) and 'type' not in event:
            await send(JSONResponse(event))
        else:
            await send(event)

    await call_next(conn, receive, json_send)

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = BlackBull()
app.use(Compression())          # gzips JSON / HTML responses when the client accepts it
app.use(session_mw)             # signed-cookie session → conn.state['session']
protected = app.group(middlewares=[auth_mw])


@app.route(methods=[HTTPMethod.GET], path='/')
async def handle_login_page(conn, receive, send):  # noqa: ARG001
    await send(Response(_load('login.html')))


@app.route(methods=[HTTPMethod.POST], path='/login', middlewares=[json_body_mw])
async def handle_do_login(conn, receive, send):
    data = conn.state['json']

    username = str(data.get('username', '')).strip()
    comm_type = str(data.get('method', 'poll'))

    if not username:
        await send(JSONResponse({'error': 'Username required'}, status=HTTPStatus.BAD_REQUEST))
        return
    if comm_type not in ('websocket', 'sse', 'poll'):
        await send(JSONResponse({'error': 'Unsupported method'}, status=HTTPStatus.BAD_REQUEST))
        return

    existing_sid = conn.state['session'].get('id', '')
    chat_session = state.get(existing_sid) if existing_sid else None

    if chat_session is None:
        chat_session = await state.create(username, comm_type)
    else:
        chat_session.comm_type = comm_type

    # ``session_mw`` re-emits Set-Cookie because assigning ``id`` marks the
    # session modified; we only add the plain ``chat_method`` cookie that the
    # client-side JS reads to pick its transport.
    conn.state['session']['id'] = chat_session.session_id
    await send(JSONResponse({'ok': True}, headers=[
        cookie_header('chat_method', comm_type, http_only=False),
    ]))


@app.route(methods=[HTTPMethod.GET], path='/chat')
async def handle_chat_page(conn, receive, send):
    # Redirect to login if no valid session; 302 differs from 401 so this
    # route is intentionally outside the `protected` group.
    sid = conn.state['session'].get('id', '')
    if not sid or state.get(sid) is None:
        await send({'type': 'http.response.start', 'status': 302,
                    'headers': [(b'location', b'/')]})
        await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
        return
    await send(Response(_load('chat.html')))


@protected.route(methods=[HTTPMethod.POST], path='/send', middlewares=[json_body_mw])
async def handle_send(conn, receive, send):
    session: ChatSession = conn.state['chat_session']
    data: dict = conn.state['json']

    text = str(data.get('message', '')).strip()
    if not text:
        await send(JSONResponse({'error': 'Empty message'}, status=HTTPStatus.BAD_REQUEST))
        return

    msg = await state.add_message(session, text)
    await state.broadcast({'type': 'message', 'payload': msg})
    await send({'ok': True})


@app.route(methods=[HTTPMethod.POST], path='/logout')
async def handle_logout(conn, receive, send):  # noqa: ARG001
    # Not in `protected`: logout must succeed even with an expired session cookie.
    sid = conn.state['session'].get('id', '')
    if sid:
        session = await state.remove(sid)
        if session:
            leave_event = {
                'type': 'user_leave',
                'payload': {'username': session.username, 'participants': state.participants()},
            }
            await state.broadcast(leave_event)

    # ``clear()`` empties the session dict and marks it modified, so
    # ``session_mw`` emits a Max-Age=0 cookie to evict it from the browser.
    conn.state['session'].clear()
    expire = 'Thu, 01 Jan 1970 00:00:00 GMT'
    await send(JSONResponse({'ok': True}, headers=[
        (b'set-cookie', f'chat_method=; Path=/; Expires={expire}; SameSite=Lax'.encode()),
    ]))


@protected.route(methods=[HTTPMethod.GET], path='/sse')
async def handle_sse(conn, receive, send):  # noqa: ARG001
    if conn.http_version != '2':
        await send(JSONResponse(
            {'error': 'SSE requires HTTP/2. Connect via HTTPS with a client that supports HTTP/2.'},
            status=HTTPStatus.BAD_REQUEST,
        ))
        return

    session: ChatSession = conn.state['chat_session']

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

    async def _watch_disconnect():
        while True:
            event = await receive()
            if event.get('type') == 'http.disconnect':
                session.connected = False  # unblock _push without waiting for its timeout
                break

    async def _push():
        while session.connected:
            try:
                evt = await asyncio.wait_for(session.queue.get(), timeout=25)
                await send({'type': 'http.response.body', 'body': _sse_encode(evt), 'more_body': True})
            except asyncio.TimeoutError:
                await send({'type': 'http.response.body', 'body': b': keepalive\n\n', 'more_body': True})
            except Exception:
                session.connected = False
                break

    await asyncio.gather(_watch_disconnect(), _push())

    session.connected = False
    session.disconnect_time = time.monotonic()
    leave_event = {
        'type': 'user_leave',
        'payload': {'username': session.username, 'participants': state.participants()},
    }
    await state.broadcast(leave_event)
    await send({'type': 'http.response.body', 'body': b'', 'more_body': False})


@protected.route(methods=[HTTPMethod.GET], path='/poll')
async def handle_poll(conn, receive, send):  # noqa: ARG001
    session: ChatSession = conn.state['chat_session']

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
            pass  # no events within the poll window → return an empty batch.

    session.last_seen = time.monotonic()
    await send(JSONResponse(events))


@protected.route(methods=[HTTPMethod.GET], path='/ws', scheme=Scheme.websocket)
async def handle_websocket(conn, receive, send):
    session: ChatSession = conn.state['chat_session']
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
                pass  # idle keepalive tick → loop and wait for the next event.
            except Exception:
                break

    async def _receiver():
        while True:
            evt = await receive()
            if evt.get('type') == 'websocket.receive':
                logger.info('WebSocket message user=%s text=%r bytes=%r',
                            session.username, evt.get('text'), evt.get('bytes'))
            elif evt.get('type', '') == 'websocket.disconnect':
                session.connected = False   # unblock _sender immediately
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
    parser = argparse.ArgumentParser(description='BlackBull Chat Server')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--cert', default=None, help='TLS certificate file (enables HTTP/2 + SSE)')
    parser.add_argument('--key',  default=None, help='TLS private key file')
    args = parser.parse_args()

    proto = 'https' if args.cert else 'http'
    logger.info('Listening on %s://localhost:%d', proto, args.port)
    if args.cert:
        logger.info('TLS active — HTTP/2 via ALPN; WebSocket (RFC 8441), SSE, and Long Polling all work.')
    else:
        logger.info('TLS not configured — SSE (HTTP/2) unavailable; WebSocket and Long Polling work.')

    try:
        app.run(certfile=args.cert, keyfile=args.key, port=args.port)
    except KeyboardInterrupt:
        logger.info('Stopped.')

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
import secrets
import time
import uuid
from http import HTTPStatus

from blackbull import WebSocketResponse
from blackbull.server.server import ASGIServer

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
# HTML pages (inlined)
# ---------------------------------------------------------------------------

LOGIN_HTML = b'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Chat &mdash; Login</title>
  <style>
    body{font-family:sans-serif;display:flex;justify-content:center;align-items:center;
         height:100vh;margin:0;background:#f0f2f5}
    .card{background:#fff;padding:2rem;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.15);width:320px}
    h1{margin:0 0 1.5rem;font-size:1.5rem;text-align:center}
    label{display:block;margin-bottom:.25rem;font-size:.875rem;font-weight:600}
    input,select{width:100%;box-sizing:border-box;padding:.5rem;border:1px solid #ccc;
                 border-radius:4px;margin-bottom:1rem;font-size:1rem}
    button{width:100%;padding:.6rem;background:#4f46e5;color:#fff;border:none;
           border-radius:4px;font-size:1rem;cursor:pointer}
    button:hover{background:#4338ca}
    .error{color:#dc2626;font-size:.875rem;margin-bottom:.75rem;display:none}
    .note{font-size:.75rem;color:#6b7280;margin-top:-.5rem;margin-bottom:.75rem}
  </style>
</head>
<body>
  <div class="card">
    <h1>Chat Login</h1>
    <div id="err" class="error"></div>
    <form onsubmit="login(event)">
      <label for="username">Username</label>
      <input id="username" type="text" required minlength="1" placeholder="Your name" autocomplete="off">
      <label for="method">Communication method</label>
      <select id="method">
        <option value="websocket">WebSocket (HTTP/1.1)</option>
        <option value="sse">Server-Sent Events (HTTP/2)</option>
        <option value="poll">Long Polling (HTTP/1.1)</option>
      </select>
      <p class="note" id="method-note"></p>
      <button type="submit">Enter Chat</button>
    </form>
  </div>
  <script>
    const notes={
      websocket:'Real-time bidirectional; requires HTTP/1.1.',
      sse:'Server-push stream; requires HTTP/2 (TLS).',
      poll:'Compatible with plain HTTP/1.1; polls every ~25 s.'
    };
    const sel=document.getElementById('method');
    const note=document.getElementById('method-note');
    sel.addEventListener('change',()=>{note.textContent=notes[sel.value]||'';});
    note.textContent=notes[sel.value]||'';

    async function login(e){
      e.preventDefault();
      const username=document.getElementById('username').value.trim();
      const method=sel.value;
      if(!username)return;
      const r=await fetch('/login',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({username,method})});
      if(!r.ok){
        const d=await r.json().catch(()=>({}));
        const err=document.getElementById('err');
        err.textContent=d.error||'Login failed';
        err.style.display='block';
        return;
      }
      window.location.href='/chat';
    }
  </script>
</body>
</html>'''

CHAT_HTML = b'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Chat</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:sans-serif;display:flex;flex-direction:column;height:100vh;background:#f0f2f5}
    header{background:#4f46e5;color:#fff;padding:.75rem 1rem;
           display:flex;justify-content:space-between;align-items:center}
    header h1{font-size:1.25rem}
    #status{font-size:.75rem;opacity:.8}
    .main{display:flex;flex:1;overflow:hidden;padding:.75rem;gap:.75rem}
    #msg-pane{flex:1;display:flex;flex-direction:column;background:#fff;border-radius:8px;overflow:hidden}
    #messages{flex:1;overflow-y:auto;padding:1rem}
    .msg{margin-bottom:.75rem}
    .meta{font-size:.75rem;color:#6b7280;margin-bottom:.125rem}
    .meta strong{color:#1f2937}
    .text{background:#f3f4f6;padding:.5rem .75rem;border-radius:6px;display:inline-block;max-width:80%}
    .msg.sys .text{background:#ede9fe;color:#5b21b6;font-style:italic}
    #send-area{display:flex;gap:.5rem;padding:.75rem;border-top:1px solid #e5e7eb}
    #inp{flex:1;padding:.5rem;border:1px solid #d1d5db;border-radius:6px;font-size:1rem}
    #send-btn{padding:.5rem 1rem;background:#4f46e5;color:#fff;border:none;
              border-radius:6px;cursor:pointer;font-size:1rem}
    #send-btn:hover{background:#4338ca}
    #part-pane{width:200px;background:#fff;border-radius:8px;padding:1rem;overflow-y:auto}
    #part-pane h2{font-size:.875rem;font-weight:700;color:#374151;margin-bottom:.75rem;
                  text-transform:uppercase;letter-spacing:.05em}
    .participant{padding:.375rem 0;font-size:.9rem;display:flex;align-items:center;gap:.5rem}
    .participant::before{content:'';width:8px;height:8px;background:#10b981;
                         border-radius:50%;flex-shrink:0}
  </style>
</head>
<body>
  <header>
    <h1>Chat</h1>
    <span id="status">Connecting&hellip;</span>
  </header>
  <div class="main">
    <div id="msg-pane">
      <div id="messages"></div>
      <div id="send-area">
        <input id="inp" type="text" placeholder="Type a message&hellip;" autocomplete="off">
        <button id="send-btn" onclick="sendMsg()">Send</button>
      </div>
    </div>
    <div id="part-pane">
      <h2>Participants</h2>
      <div id="participants"></div>
    </div>
  </div>
  <script>
    const msgsEl=document.getElementById('messages');
    const partEl=document.getElementById('participants');
    const statusEl=document.getElementById('status');
    const inp=document.getElementById('inp');
    inp.addEventListener('keydown',e=>{if(e.key==='Enter')sendMsg();});

    function getCookie(name){
      const m=document.cookie.match('(?:^|;)\\s*'+name+'=([^;]*)');
      return m?decodeURIComponent(m[1]):null;
    }
    function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

    function addMsg(msg,sys){
      const d=document.createElement('div');
      d.className='msg'+(sys?' sys':'');
      if(!sys){
        const ts=new Date(msg.timestamp).toLocaleTimeString();
        d.innerHTML='<div class="meta"><strong>'+esc(msg.username)+'</strong> '+ts+'</div>'+
                    '<div class="text">'+esc(msg.message)+'</div>';
      } else {
        d.innerHTML='<div class="text">'+esc(msg.message)+'</div>';
      }
      msgsEl.appendChild(d);
      msgsEl.scrollTop=msgsEl.scrollHeight;
    }

    function setParts(list){
      partEl.innerHTML=list.map(p=>'<div class="participant">'+esc(p.username)+'</div>').join('');
    }

    function handle(evt){
      if(evt.type==='message')         addMsg(evt.payload);
      else if(evt.type==='history')    evt.payload.forEach(m=>addMsg(m));
      else if(evt.type==='user_join')  {setParts(evt.payload.participants);addMsg({message:esc(evt.payload.username)+' joined',timestamp:new Date().toISOString()},true);}
      else if(evt.type==='user_leave') {setParts(evt.payload.participants);addMsg({message:esc(evt.payload.username)+' left', timestamp:new Date().toISOString()},true);}
    }

    async function sendMsg(){
      const text=inp.value.trim();if(!text)return;inp.value='';
      await fetch('/send',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({message:text})});
    }

    const method=getCookie('chat_method')||'poll';

    // WebSocket
    function connectWS(){
      const proto=location.protocol==='https:'?'wss':'ws';
      const ws=new WebSocket(proto+'://'+location.host+'/ws');
      ws.onopen=()=>{statusEl.textContent='Connected (WebSocket)';};
      ws.onmessage=e=>{handle(JSON.parse(e.data));};
      ws.onerror=()=>{statusEl.textContent='WebSocket error - retrying...';};
      ws.onclose=()=>{statusEl.textContent='Disconnected';setTimeout(connectWS,3000);};
    }

    // SSE
    function connectSSE(){
      if(!window.EventSource){statusEl.textContent='SSE not supported';return;}
      const es=new EventSource('/sse');
      es.onopen=()=>{statusEl.textContent='Connected (SSE)';};
      es.onmessage=e=>{handle(JSON.parse(e.data));};
      es.onerror=()=>{statusEl.textContent='SSE error - check HTTP/2 is available';};
    }

    // Long Polling
    let _polling=true;
    async function poll(){
      while(_polling){
        try{
          const r=await fetch('/poll');
          if(r.ok){const evts=await r.json();evts.forEach(handle);}
        }catch(e){await new Promise(r=>setTimeout(r,2000));}
      }
    }
    function connectPoll(){statusEl.textContent='Connected (Long Polling)';poll();}

    if(method==='websocket') connectWS();
    else if(method==='sse')  connectSSE();
    else                     connectPoll();
  </script>
</body>
</html>'''

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_cookie(scope) -> dict[str, str]:
    raw = scope['headers'].get_value(b'cookie')
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


def _set_cookie_header(name: str, value: str, path: str = '/') -> tuple[bytes, bytes]:
    return (b'set-cookie', f'{name}={value}; Path={path}; HttpOnly; SameSite=Lax'.encode())


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
    await _send_html(send, LOGIN_HTML)


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
        _set_cookie_header('session_id', session.session_id),
        _set_cookie_header('chat_method', comm_type),
    ]
    await _send_json(send, {'ok': True}, extra_headers=extra)


async def handle_chat_page(scope, receive, send):
    session = _session_from_scope(scope)
    if session is None:
        headers = [(b'location', b'/')]
        await send({'type': 'http.response.start', 'status': 302, 'headers': headers})
        await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
        return
    await _send_html(send, CHAT_HTML)


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

# ---------------------------------------------------------------------------
# ASGI application
# ---------------------------------------------------------------------------

ROUTES: dict[tuple[str, str], object] = {
    ('GET',  '/'):      handle_login_page,
    ('POST', '/login'): handle_do_login,
    ('GET',  '/chat'):  handle_chat_page,
    ('POST', '/send'):  handle_send,
    ('GET',  '/sse'):   handle_sse,
    ('GET',  '/poll'):  handle_poll,
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

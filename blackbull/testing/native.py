"""Native-path test client — the two tiers that drive BlackBull's *own*
request path rather than the ASGI compatibility boundary.

BlackBull threads a typed :class:`~blackbull.connection.Connection` end to
end; the ASGI ``scope`` dict survives only at two boundaries.  The
compatibility client in :mod:`blackbull.testing` reaches the app through
``httpx.ASGITransport`` → scope dict → ``Connection.from_scope()``, so the
``isinstance(conn, Connection)`` branch of ``BlackBull.__call__`` — the branch
every production request takes — is never exercised by it.  A defect can
therefore live on the native path while the whole compat-driven suite passes.

Two tiers close that, mirroring what every framework that owns its protocol
stack provides:

**Tier 1** — :func:`request` and the verb helpers build a ``Connection`` and
call ``app(conn, receive, send)`` directly.  No socket, no protocol actor:
everything from ``Connection`` inward (dispatcher, middleware chain, router,
handlers, DI, events, response serialisation).  The equivalent of actix-web's
``init_service`` or Fastify's ``.inject()``::

    resp = await native.get(app, '/hello')
    assert resp.status == 200

**Tier 2** — :class:`NativeTestServer` binds a real loopback socket and runs
BlackBull's own :class:`~blackbull.server.server.Server`, so a request
travels accept → ``HTTP1Actor`` parse → ``Connection`` → native dispatch →
wire bytes.  The equivalent of aiohttp's ``TestServer`` or Go's
``httptest.NewServer``::

    async with NativeTestServer(app) as server:
        resp = await server.client.get('/hello')

Both tiers are async-first because the app entry point is a coroutine and a
handler runs on the caller's event loop — which is also what production does.
:class:`NativeClient` and the synchronous form of :class:`NativeTestServer`
wrap them for tests written as plain ``def``, each owning one background
event loop for its whole lifetime rather than per request.

Which instrument to reach for is documented in ``docs/guide/testing.md``:
Tier 1 for application logic, Tier 2 for anything whose answer depends on the
wire, and :class:`~blackbull.testing.TestClient` for the ASGI boundary itself.
"""

from __future__ import annotations

import asyncio
import json as _json
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, Iterable, Mapping
from urllib.parse import unquote

import httpx

from ..connection import Connection, bind_receive_channel
from ..headers import Headers

__all__ = [
    'NativeResponse', 'NativeClient', 'NativeTestServer',
    'build_connection', 'request',
    'get', 'head', 'options', 'post', 'put', 'patch', 'delete',
]


#: Client/server addresses stamped onto a synthesised Connection.  The same
#: pair ``WebSocketTestSession`` uses, so a handler that logs or authorises on
#: ``conn.client`` sees one consistent identity across both test instruments.
_TEST_CLIENT_ADDR = ('testclient', 50000)
_TEST_SERVER_ADDR = ('testserver', 80)

_HeaderInput = Mapping[Any, Any] | Iterable[tuple[Any, Any]] | Headers | None


def _encode_header(value: Any) -> bytes:
    """Coerce a header name or value to the bytes the wire would carry."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    return str(value).encode('latin-1')


def _header_pairs(headers: _HeaderInput) -> list[tuple[bytes, bytes]]:
    """Normalise caller-supplied headers to lowercase byte pairs.

    Accepts what a test naturally writes — a ``dict`` of ``str`` or ``bytes``,
    a list of pairs, or a :class:`Headers` — because the alternative is every
    call site spelling ``{b'x-probe': b'seen'}`` by hand.  Names are
    lowercased to match the parser's index (``headers.get(b'content-type')``).
    """
    if headers is None:
        return []
    items = headers.items() if isinstance(headers, Mapping) else headers
    return [(_encode_header(k).lower(), _encode_header(v)) for k, v in items]


@dataclass
class NativeResponse:
    """What Tier 1 collected from the app's ``send`` channel.

    ``body`` is the concatenation of every ``http.response.body`` chunk, so a
    streaming handler and a single-shot one are compared the same way.
    ``events`` keeps the raw ASGI events for tests that assert on the emission
    sequence itself (chunk boundaries, ``more_body`` flags, trailers).
    """

    status: int
    headers: Headers
    body: bytes = b''
    events: list[dict] = field(default_factory=list, repr=False)

    def json(self) -> Any:
        """Parse the body as JSON."""
        return _json.loads(self.body)

    def text(self, encoding: str = 'utf-8') -> str:
        """Decode the body as text (``errors='replace'``)."""
        return self.body.decode(encoding, errors='replace')


def build_connection(
    method: str,
    path: str,
    *,
    headers: _HeaderInput = None,
    body: bytes | str = b'',
    http_version: str = '1.1',
    scheme: str = 'http',
    root_path: str = '',
    client: tuple[str, int | None] | None = _TEST_CLIENT_ADDR,
    server: tuple[str, int | None] | None = _TEST_SERVER_ADDR,
) -> Connection:
    """Build the :class:`Connection` an H/1.1 request line would have produced.

    The field derivations mirror :meth:`HTTP1Actor._parse` so a Tier 1 test and
    a real request agree on what the handler sees:

    - the query string is split off ``path`` and carried in ``query_string``,
      never in ``raw_path``;
    - ``path`` is percent-decoded, ``raw_path`` keeps the undecoded bytes;
    - a ``host`` header is supplied when the caller gave none, because every
      HTTP/1.1 request carries one (RFC 9112 §3.2) and code that reads it
      would otherwise behave differently under test than on the wire;
    - a ``content-length`` is derived from *body* for the same reason — an
      explicit one from the caller wins, so a test can still synthesise a
      mismatched framing header on purpose.
    """
    raw_path, _, query = path.partition('?')
    pairs = _header_pairs(headers)
    names = {name for name, _ in pairs}
    if b'host' not in names:
        pairs.append((b'host', _TEST_SERVER_ADDR[0].encode('latin-1')))
    if body and b'content-length' not in names:
        pairs.append((b'content-length', str(len(_encode_body(body))).encode('ascii')))
    return Connection(
        type='http',
        http_version=http_version,
        method=method.upper(),
        scheme=scheme,
        # UTF-8 (not latin-1) for parity with the parser: a non-ASCII path
        # such as '/café' must round-trip as the server would have received it.
        path=(unquote(raw_path, encoding='utf-8', errors='replace')
              if '%' in raw_path else raw_path),
        raw_path=raw_path.encode('utf-8'),
        query_string=query.encode('utf-8'),
        root_path=root_path,
        headers=Headers(pairs),
        client=client,
        server=server,
    )


def _encode_body(body: bytes | str) -> bytes:
    if isinstance(body, str):
        return body.encode('utf-8')
    return bytes(body)


async def request(
    app: Any,
    conn: Connection,
    *,
    body: bytes | str = b'',
) -> NativeResponse:
    """Call ``app(conn, receive, send)`` and collect the response.

    The full-control form: build (or mutate) a :class:`Connection` yourself and
    drive the app with it.  The verb helpers below are thin wrappers over this.

    The receive channel delivers *body* as one ``http.request`` event and then
    reports ``http.disconnect`` — the same terminal signal a real recipient
    gives once the body is drained, so a handler that over-reads gets the
    production answer rather than hanging.
    """
    payload = _encode_body(body)
    delivered = False

    async def receive() -> dict:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {'type': 'http.request', 'body': payload, 'more_body': False}
        return {'type': 'http.disconnect'}

    events: list[dict] = []
    chunks: list[bytes] = []
    status: int | None = None
    response_headers: list = []

    # Same dual-form signature as the protocol senders: a handler may emit ASGI
    # dicts, or the ``send(body, status, headers)`` convenience form that the
    # actor's sender also accepts.  Tier 1 has to accept both or it would
    # reject code the real server runs.
    async def send(event: Any, status_arg: HTTPStatus = HTTPStatus.OK,
                   headers_arg: Any = ()) -> None:
        nonlocal status, response_headers
        if not isinstance(event, dict):
            body_bytes = bytes(event) if not isinstance(event, bytes) else event
            status = int(status_arg)
            response_headers = list(headers_arg)
            chunks.append(body_bytes)
            events.append({'type': 'http.response.start', 'status': status,
                           'headers': response_headers})
            events.append({'type': 'http.response.body', 'body': body_bytes,
                           'more_body': False})
            return
        events.append(event)
        etype = event.get('type')
        if etype == 'http.response.start':
            status = int(event.get('status', 200))
            response_headers = list(event.get('headers') or [])
        elif etype == 'http.response.body':
            chunks.append(event.get('body', b'') or b'')
        elif etype == 'http.response.pathsend':
            # The ``http.response.pathsend`` extension hands the server a file
            # path instead of bytes so it can sendfile(2).  Tier 1 has no
            # transport to hand it to, so it does what the kernel would: read
            # the file.  The observable response is then the same either way.
            with open(event['path'], 'rb') as fp:
                chunks.append(fp.read())

    # Bind the body channel the way the protocol actor does, so ``conn.body()``
    # / ``conn.stream()`` drain this request's payload rather than nothing.
    bind_receive_channel(conn, receive)
    await app(conn, receive, send)

    if status is None:
        raise AssertionError(
            'The app produced no http.response.start event — the handler '
            'returned without sending a response.')
    return NativeResponse(status=status, headers=Headers(response_headers),
                          body=b''.join(chunks), events=events)


async def _verb(app: Any, method: str, path: str, *,
                body: bytes | str = b'',
                json: Any = None,
                headers: _HeaderInput = None,
                **kwargs: Any) -> NativeResponse:
    if json is not None:
        if body:
            raise TypeError("pass either 'body' or 'json', not both")
        body = _json.dumps(json).encode('utf-8')
        pairs = _header_pairs(headers)
        if not any(name == b'content-type' for name, _ in pairs):
            pairs.append((b'content-type', b'application/json'))
        headers = pairs
    conn = build_connection(method, path, headers=headers, body=body, **kwargs)
    return await request(app, conn, body=body)


async def get(app: Any, path: str, **kwargs: Any) -> NativeResponse:
    """Drive ``app`` with a GET through the native dispatch path."""
    return await _verb(app, 'GET', path, **kwargs)


async def head(app: Any, path: str, **kwargs: Any) -> NativeResponse:
    """Drive ``app`` with a HEAD through the native dispatch path.

    The handler sees ``HEAD``: rewriting it to ``GET`` and stripping the body
    is the H/1.1 actor's job (RFC 9110 §9.3.2), which is below this tier.  Use
    :class:`NativeTestServer` to assert HEAD's *wire* behaviour.
    """
    return await _verb(app, 'HEAD', path, **kwargs)


async def options(app: Any, path: str, **kwargs: Any) -> NativeResponse:
    """Drive ``app`` with an OPTIONS through the native dispatch path."""
    return await _verb(app, 'OPTIONS', path, **kwargs)


async def post(app: Any, path: str, **kwargs: Any) -> NativeResponse:
    """Drive ``app`` with a POST through the native dispatch path."""
    return await _verb(app, 'POST', path, **kwargs)


async def put(app: Any, path: str, **kwargs: Any) -> NativeResponse:
    """Drive ``app`` with a PUT through the native dispatch path."""
    return await _verb(app, 'PUT', path, **kwargs)


async def patch(app: Any, path: str, **kwargs: Any) -> NativeResponse:
    """Drive ``app`` with a PATCH through the native dispatch path."""
    return await _verb(app, 'PATCH', path, **kwargs)


async def delete(app: Any, path: str, **kwargs: Any) -> NativeResponse:
    """Drive ``app`` with a DELETE through the native dispatch path."""
    return await _verb(app, 'DELETE', path, **kwargs)


# ---------------------------------------------------------------------------
# Synchronous façade over Tier 1
# ---------------------------------------------------------------------------

class NativeClient:
    """Synchronous Tier 1 client, for tests written as plain ``def``.

    Owns one background event loop for the whole session — not one per
    request — and drives the ASGI ``lifespan`` protocol around it, so startup
    hooks have run before the first request the way they have in production::

        with NativeClient(app) as client:
            resp = client.get('/hello')
            assert resp.status == 200

    Prefer the module-level coroutines from an ``async def`` test: they call
    the app on the test's own loop with no thread hand-off at all.
    """

    __test__ = False        # not a pytest container despite the name shape

    def __init__(self, app: Any) -> None:
        self.app = app
        # Imported here rather than at module scope: ``blackbull.testing``
        # imports this module, so a top-level import would be circular.
        from . import _LifespanManager, _LoopThread  # noqa: PLC0415
        self._loop_thread = _LoopThread()
        self._lifespan_cls = _LifespanManager
        self._lifespan: Any = None
        self._entered = False

    def __enter__(self) -> 'NativeClient':
        self._loop_thread.start()
        self._lifespan = self._lifespan_cls(self.app, self._loop_thread)
        try:
            self._lifespan.startup()
        except Exception:
            self._loop_thread.stop()
            raise
        self._entered = True
        return self

    def __exit__(self, *exc_info) -> None:
        self._entered = False
        try:
            if self._lifespan is not None:
                self._lifespan.shutdown()
        finally:
            self._loop_thread.stop()

    def _run(self, fn, *args: Any, **kwargs: Any) -> NativeResponse:
        # The guard runs *before* the coroutine is constructed: building one
        # and then raising leaves a "coroutine was never awaited" warning
        # attached to the caller's test, pointing at the wrong problem.
        if not self._entered:
            raise RuntimeError(
                'NativeClient must be used as a context manager: '
                '`with NativeClient(app) as client: ...`')
        return self._loop_thread.run_coro(fn(self.app, *args, **kwargs))

    def request(self, conn: Connection, *, body: bytes | str = b'') -> NativeResponse:
        """Full-control form — see :func:`request`."""
        return self._run(request, conn, body=body)

    def get(self, path: str, **kwargs: Any) -> NativeResponse:
        return self._run(get, path, **kwargs)

    def head(self, path: str, **kwargs: Any) -> NativeResponse:
        return self._run(head, path, **kwargs)

    def options(self, path: str, **kwargs: Any) -> NativeResponse:
        return self._run(options, path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> NativeResponse:
        return self._run(post, path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> NativeResponse:
        return self._run(put, path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> NativeResponse:
        return self._run(patch, path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> NativeResponse:
        return self._run(delete, path, **kwargs)


# ---------------------------------------------------------------------------
# Tier 2 — real socket, full stack
# ---------------------------------------------------------------------------

class NativeTestServer:
    """BlackBull's own server on a loopback port, for full-stack tests.

    Every layer runs: TCP accept, ``ConnectionActor``, ``HTTP1Actor`` parsing,
    the native ``Connection``, dispatch, and the bytes the sender puts on the
    wire.  Anything whose answer depends on the wire — keep-alive reuse, the
    HEAD body strip, chunked framing, connection close semantics — is only
    observable here.

    Async form (preferred: the server shares the test's event loop, as it
    shares the process loop in production)::

        async with NativeTestServer(app) as server:
            resp = await server.client.get('/hello')

    Synchronous form, for plain ``def`` tests — the server runs on one
    background loop for the session::

        with NativeTestServer(app) as server:
            resp = server.client.get('/hello')

    The listener binds ``127.0.0.1`` only, so a test never exposes a port
    beyond the machine.  Plaintext HTTP/1.1 and WebSocket; TLS and HTTP/2 are
    out of scope for this tier — reach for ``blackbull.fault_injection`` or the
    conformance suites there.
    """

    __test__ = False        # not a pytest container despite the name shape

    def __init__(self, app: Any, *, host: str = '127.0.0.1',
                 port: int = 0, backlog: int = 128,
                 timeout: float = 10.0, **server_kwargs: Any) -> None:
        self.app = app
        self.host = host
        self.port = port
        #: TCP connections accepted since this server started — an *accept*
        #: count, not a request count, so four keep-alive requests on one
        #: connection leave it at 1.  Both context-manager forms maintain it.
        self.connections_served = 0
        self._backlog = backlog
        self._timeout = timeout
        self._server_kwargs = server_kwargs
        self._bb_server: Any = None
        self._asyncio_server: asyncio.AbstractServer | None = None
        self._lifespan: Any = None
        self._async_client: httpx.AsyncClient | None = None
        self._loop_thread: Any = None
        # ``Any``, not ``'_SyncServerClient | None'``: the class is defined
        # below this one, and beartype rejects a *relative* forward reference
        # in a local annotation (it can only resolve absolute ones there).
        self._sync_client: Any = None

    # -- async form ---------------------------------------------------------

    async def __aenter__(self) -> 'NativeTestServer':
        from ..server.server import LifespanManager, Server  # noqa: PLC0415

        self._bb_server = Server(self.app, **self._server_kwargs)

        async def _accept(reader, writer):
            # The only thing layered over the production callback: count the
            # accept.  A test asserting keep-alive reuse needs to know how many
            # TCP connections its requests actually opened, and the honest
            # place to learn that is the accept path — not a response header
            # the server merely *may* send, and not httpx's private pool.
            self.connections_served += 1
            await self._bb_server.client_connected_cb(reader, writer)

        # ``asyncio.start_server`` rather than ``Server.open_socket``: the
        # latter binds 0.0.0.0 + :: (right for a real deployment, wrong for a
        # test that should never leave the loopback).  The callback below
        # accept is the production one, so everything after it is identical.
        self._asyncio_server = await asyncio.start_server(
            _accept, self.host, self.port, backlog=self._backlog)
        sockets = self._asyncio_server.sockets or ()
        if not sockets:
            raise RuntimeError('NativeTestServer failed to bind a socket.')
        self.port = sockets[0].getsockname()[1]

        self._lifespan = LifespanManager(self.app)
        try:
            await self._lifespan.__aenter__()
        except BaseException:
            await self._close_socket()
            raise
        self._async_client = httpx.AsyncClient(base_url=self.url,
                                               timeout=self._timeout)
        return self

    async def __aexit__(self, *exc_info) -> None:
        try:
            if self._async_client is not None:
                # Close the client first: its keep-alive connections are the
                # ones ``wait_closed()`` would otherwise wait on.
                await self._async_client.aclose()
                self._async_client = None
        finally:
            try:
                if self._lifespan is not None:
                    await self._lifespan.__aexit__(*exc_info)
                    self._lifespan = None
            finally:
                await self._close_socket()

    async def _close_socket(self) -> None:
        if self._asyncio_server is None:
            return
        self._asyncio_server.close()
        try:
            await asyncio.wait_for(self._asyncio_server.wait_closed(),
                                   timeout=self._timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # A wedged connection handler must not turn a passing test into a
            # hang; the socket is closed either way.
            pass
        self._asyncio_server = None

    # -- sync form ----------------------------------------------------------

    def __enter__(self) -> 'NativeTestServer':
        from . import _LoopThread  # noqa: PLC0415
        self._loop_thread = _LoopThread()
        self._loop_thread.start()
        try:
            self._loop_thread.run_coro(self.__aenter__(), timeout=self._timeout)
        except BaseException:
            self._loop_thread.stop()
            raise
        self._sync_client = _SyncServerClient(self._loop_thread, self)
        return self

    def __exit__(self, *exc_info) -> None:
        try:
            self._loop_thread.run_coro(self.__aexit__(*exc_info),
                                       timeout=self._timeout + 5.0)
        finally:
            self._sync_client = None
            self._loop_thread.stop()
            self._loop_thread = None

    # -- accessors ----------------------------------------------------------

    @property
    def url(self) -> str:
        """Base URL of the running server, e.g. ``http://127.0.0.1:54321``."""
        return f'http://{self.host}:{self.port}'

    @property
    def client(self):
        """An HTTP client bound to this server.

        ``httpx.AsyncClient`` under ``async with``; a synchronous façade over
        the same client under plain ``with``.
        """
        if self._sync_client is not None:
            return self._sync_client
        if self._async_client is None:
            raise RuntimeError(
                'NativeTestServer must be used as a context manager: '
                '`async with NativeTestServer(app) as server: ...`')
        return self._async_client


class _SyncServerClient:
    """Blocking façade over :class:`NativeTestServer`'s ``httpx.AsyncClient``.

    Each call is scheduled on the server's own loop thread, so the request and
    the server it talks to share one loop — the same arrangement the async form
    gets for free.
    """

    def __init__(self, loop_thread: Any, server: NativeTestServer) -> None:
        self._loop_thread = loop_thread
        self._server = server

    def _client(self) -> httpx.AsyncClient:
        client = self._server._async_client
        if client is None:
            raise RuntimeError('NativeTestServer is not running.')
        return client

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        return self._loop_thread.run_coro(
            self._client().request(method, url, **kwargs))

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request('GET', url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request('HEAD', url, **kwargs)

    def options(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request('OPTIONS', url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request('POST', url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request('PUT', url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request('PATCH', url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request('DELETE', url, **kwargs)

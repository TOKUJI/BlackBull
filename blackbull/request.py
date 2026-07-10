"""Request body and cookie helpers, and the opt-in ``Request`` context object.

Provides:

- `Request`: opt-in context object for HTTP handlers — wraps ``(scope, receive)``
  and exposes the helpers below as cached properties/methods.
- `read_body`: buffers all ASGI ``http.request`` chunks into a single ``bytes`` object.
- `read_json`: buffers the body and parses it as JSON (``None`` on empty/invalid).
- `read_text`: buffers the body and decodes it as text.
- `parse_cookies`: parses the ``Cookie`` request header into a ``dict[str, str]``.
"""
import json
from typing import Any

from .asgi import ASGIEvent
from .headers import Headers


class ClientDisconnected(Exception):
    """Raised when the client disconnects before the request body is complete.

    ASGI signals a mid-body disconnect with an ``http.disconnect`` event that
    carries no ``body``/``more_body`` keys.  Treating it as end-of-message
    would return a *truncated* upload as if it were whole, so
    :func:`read_body` raises this instead — the handler must not process a
    partial body as complete.  The ``partial`` attribute holds whatever body
    bytes had arrived before the disconnect.
    """

    def __init__(self, partial: bytes = b''):
        super().__init__('client disconnected before the request body completed')
        self.partial = partial


async def read_body(receive) -> bytes:
    """Read the complete request body from the ASGI receive channel.

    Collects chunks in a list and joins once, rather than the O(n²) ``+=``
    growth.  A single-chunk body (the common case) is returned directly with
    no intermediate copy at all (copy-reduction-http1 P1).

    Raises :class:`ClientDisconnected` if an ``http.disconnect`` arrives
    before the body is complete, so a truncated upload is never silently
    returned as if whole.
    """
    chunks: list[bytes] = []
    while True:
        event = await receive()
        if event.get('type') == ASGIEvent.HTTP_DISCONNECT:
            # Peer went away mid-body — the accumulated bytes are a partial
            # upload, not a complete one.  Surface it rather than returning
            # the truncated body as if it were the whole message.
            raise ClientDisconnected(b''.join(chunks))
        chunk = event.get('body', b'')
        if chunk:
            chunks.append(chunk)
        if not event.get('more_body', False):
            break
    if not chunks:
        return b''
    if len(chunks) == 1:
        return chunks[0]
    return b''.join(chunks)


async def read_json(receive) -> Any:
    """Read the request body and parse it as JSON.

    Returns the parsed JSON value (``dict``, ``list``, ``str``, ``int``,
    ``float``, ``bool``), or ``None`` when the body is empty, not valid JSON,
    or not decodable.  Callers should treat ``None`` as a client error and
    respond ``400``::

        data = await read_json(receive)
        if data is None:
            await send(JSONResponse({'error': 'invalid JSON'},
                                    status=HTTPStatus.BAD_REQUEST))
            return

    Note that a literal JSON ``null`` body also parses to ``None``; if that
    distinction matters, read the body yourself with :func:`read_body`.

    A mid-body client disconnect propagates as :class:`ClientDisconnected`
    rather than being reported as invalid JSON — a truncated body is a
    transport failure, not a parse error.
    """
    body = await read_body(receive)
    return _json_or_none(body)


def _json_or_none(body: bytes) -> Any:
    """Parse *body* as JSON, returning ``None`` on empty/invalid input."""
    if not body:
        return None
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


async def read_text(receive, encoding: str = 'utf-8') -> str:
    """Read the request body and decode it as text.

    Uses ``errors='replace'`` so undecodable bytes become U+FFFD rather than
    raising — a malformed body never crashes the handler.  Override
    *encoding* for non-UTF-8 payloads.

    A mid-body client disconnect still propagates as
    :class:`ClientDisconnected`: a truncated upload must not be decoded and
    returned as if it were the complete text.
    """
    body = await read_body(receive)
    return body.decode(encoding, errors='replace')


def parse_cookies(scope) -> dict[str, str]:
    """Parse the ``Cookie`` request header from an ASGI scope into a dict.

    Works identically for HTTP/1.1, HTTP/2, and WebSocket scopes.
    HTTP/1.1 sends a single combined ``Cookie`` header; HTTP/2 may split it
    into multiple fields (RFC 7540 §8.1.2.5).  All ``cookie`` fields are
    collected and joined before parsing, so both wire formats produce the
    same result.

    Accepts either of the two header shapes that may appear on
    ``scope['headers']``:

    - A plain list/iterable of ``(name, value)`` bytes tuples — the
      standard ASGI 3.0 form, used by external servers (uvicorn,
      hypercorn, ``httpx.ASGITransport``).
    - A :class:`blackbull.headers.Headers` instance — what BlackBull's
      own server attaches as a handler ergonomics enhancement.
    """
    headers = scope.get('headers', ())
    getlist = getattr(headers, 'getlist', None)
    if getlist is not None:
        cookie_values = [v for _, v in getlist(b'cookie')]
    else:
        cookie_values = [v for (k, v) in headers if k.lower() == b'cookie']
    if not cookie_values:
        return {}
    raw = b'; '.join(cookie_values)
    result = {}
    for part in raw.split(b';'):
        k, _, v = part.strip().partition(b'=')
        result[k.strip().decode(errors='replace')] = v.strip().decode(errors='replace')
    return result


_BODY_UNREAD = None  # sentinel: b'' is a valid cached body, so None means "not read yet"


class Request:
    """Opt-in context object for HTTP handlers.

    Wraps an ASGI ``(scope, receive)`` pair and exposes this module's free
    functions as cached properties and methods — the HTTP counterpart of
    ``GrpcContext`` and ``ProtocolContext``.  Handlers receive one by
    declaring a parameter annotated ``Request`` (any name) or a parameter
    named ``request`` with no annotation; the router injects it at call
    time with no per-request reflection::

        @app.route(path='/users/{uid}')
        async def show(uid: int, request: Request):
            token = request.headers.get(b'authorization')
            data = await request.json()
            ...

    ``body()`` buffers the request body exactly once and caches it;
    ``json()`` and ``text()`` build on that cache.  When a handler also
    declares ``body: bytes`` (or a dataclass body parameter), the router
    sources those from the same cache, so ``receive`` is never drained
    twice.  The raw ``(scope, receive, send)`` handler form is unaffected
    and never pays for this class.
    """

    __slots__ = ('_scope', '_receive', '_headers', '_cookies', '_body')

    def __init__(self, scope: dict, receive):
        self._scope = scope
        self._receive = receive
        self._headers: Headers | None = None
        self._cookies: dict[str, str] | None = None
        self._body: bytes | None = _BODY_UNREAD

    # ---- scope-backed properties -----------------------------------------

    @property
    def scope(self) -> dict:
        """The raw ASGI scope dict (escape hatch)."""
        return self._scope

    @property
    def method(self) -> str:
        """HTTP method, e.g. ``'GET'``."""
        return self._scope['method']

    @property
    def path(self) -> str:
        """Request path, e.g. ``'/users/7'``."""
        return self._scope['path']

    @property
    def scheme(self) -> str:
        """URL scheme, e.g. ``'http'`` / ``'https'``."""
        return self._scope.get('scheme', 'http')

    @property
    def client(self) -> tuple | None:
        """``(host, port)`` of the peer, or ``None`` when unknown."""
        return self._scope.get('client')

    @property
    def headers(self) -> Headers:
        """Request headers as a case-insensitive :class:`~blackbull.headers.Headers` view.

        ``scope['headers']`` may be a plain ASGI pair-list (external servers,
        ``TestClient``) or already a ``Headers`` instance (BlackBull's own
        server); both shapes are served through the same view, computed once.
        """
        if self._headers is None:
            raw = self._scope.get('headers', ())
            self._headers = raw if isinstance(raw, Headers) else Headers(raw)
        return self._headers

    @property
    def cookies(self) -> dict[str, str]:
        """Cookies from the ``Cookie`` request header, parsed once."""
        if self._cookies is None:
            self._cookies = parse_cookies(self._scope)
        return self._cookies

    # ---- body access (single-drain cache) --------------------------------

    async def body(self) -> bytes:
        """Return the complete request body, buffering it on first use.

        The underlying ``receive`` channel is drained at most once; repeated
        calls (and ``json()`` / ``text()``) return the cached bytes.  A
        mid-body disconnect raises :class:`ClientDisconnected` and leaves the
        cache unset.
        """
        if self._body is _BODY_UNREAD:
            self._body = await read_body(self._receive)
        return self._body

    async def json(self) -> Any:
        """Parse the body as JSON — ``None`` on empty or invalid input.

        Same contract as :func:`read_json`; see its note about a literal
        JSON ``null`` also returning ``None``.
        """
        return _json_or_none(await self.body())

    async def text(self, encoding: str = 'utf-8') -> str:
        """Decode the body as text (``errors='replace'``), same contract as :func:`read_text`."""
        body = await self.body()
        return body.decode(encoding, errors='replace')

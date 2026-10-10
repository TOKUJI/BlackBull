"""Native send messages.

Presence is is not None, never truthiness: an empty body is real content.
Preserve absent header/body/trailers through middleware. Introduce ASGI event
dictionaries only at conversion boundaries.
"""
from __future__ import annotations

from .headers import _MinimalResponseHeaders, _as_response_fields, _owned_response_fields


class _HeaderView:
    """Zero-copy view over a [`NativeResponse`][] header or trailer list, whose
    names are lowercase tchar and values free of CTL.

    ``append`` validates and lowercases what it adds, so the list keeps that
    contract; lookups take a name in any case (RFC 9110 §5.1).  Mutations are
    visible to anything reading the response afterwards (the sender,
    ``to_asgi``).
    """

    __slots__ = ('_items', '_owner')

    def __init__(self, items: _MinimalResponseHeaders,
                 owner: NativeResponse | None = None) -> None:
        self._items = items
        self._owner = owner

    def __iter__(self):
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, name: bytes) -> bool:
        name = name.lower()
        return any(k == name for k, _ in self._items)

    def get(self, name: bytes, default: bytes = b'') -> bytes:
        name = name.lower()
        for k, v in self._items:
            if k == name:
                return v
        return default

    def getlist(self, name: bytes) -> list[tuple[bytes, bytes]]:
        name = name.lower()
        return [(k, v) for k, v in self._items if k == name]

    def append(self, name_or_pairs, value: bytes | None = None) -> None:
        if value is None:
            # One-arg form: a *list* of pairs to extend with.  A bare
            # (name, value) 2-tuple is a footgun — ``extend`` would walk its
            # elements (two bytes) as separate entries and corrupt the list —
            # so a 2-tuple of (bytes, bytes) is treated as one pair.
            if (isinstance(name_or_pairs, tuple) and len(name_or_pairs) == 2
                    and isinstance(name_or_pairs[0], (bytes, str))):
                name_or_pairs = (name_or_pairs,)
        else:
            name_or_pairs = ((name_or_pairs, value),)
        push = self._owner is not None and isinstance(self._owner._extension, _PushPath)
        for name, field_value in name_or_pairs:
            if push and name.lower() == b'host':
                raise ValueError(_PUSH_HOST)
            self._items.add(name, field_value)


class NativeWSMessage:
    """A native WebSocket send message.

    ACCEPT carries subprotocol/headers; SEND exactly one of text/data;
    CLOSE code/reason. to_asgi maps data to the ASGI bytes key at a boundary.
    """

    ACCEPT = 'accept'
    SEND = 'send'
    CLOSE = 'close'

    __slots__ = ('kind', 'text', 'data', 'code', 'reason', 'subprotocol',
                 'headers')

    def __init__(self, kind: str, *, text: str | None = None,
                 data: bytes | None = None,
                 code: int | None = None, reason: str = '',
                 subprotocol: str | None = None,
                 headers: list[tuple[bytes, bytes]] | None = None) -> None:
        self.kind = kind
        self.text = text
        self.data = data
        self.code = code
        self.reason = reason
        self.subprotocol = subprotocol
        self.headers = headers

    # --- constructors, one per kind ---------------------------------------

    @classmethod
    def accept(cls, subprotocol: str | None = None,
               headers: list[tuple[bytes, bytes]] | None = None
               ) -> 'NativeWSMessage':
        return cls(cls.ACCEPT, subprotocol=subprotocol, headers=headers)

    @classmethod
    def text_message(cls, text: str) -> 'NativeWSMessage':
        return cls(cls.SEND, text=text)

    @classmethod
    def binary_message(cls, data: bytes) -> 'NativeWSMessage':
        return cls(cls.SEND, data=data)

    @classmethod
    def close(cls, code: int = 1000, reason: str = '') -> 'NativeWSMessage':
        return cls(cls.CLOSE, code=code, reason=reason)

    # --- boundary conversion ----------------------------------------------

    def to_asgi(self) -> list[dict]:
        """Convert to the ASGI ``websocket.*`` event list.

        Used only at conversion boundaries — the external ASGI edge and the
        raw ``(conn, receive, send)`` compat surface — exactly like
        [`NativeResponse.to_asgi`][NativeResponse.to_asgi] on the HTTP side.
        """
        if self.kind == self.ACCEPT:
            event: dict = {'type': 'websocket.accept',
                           'subprotocol': self.subprotocol}
            if self.headers is not None:
                event['headers'] = list(self.headers)
            return [event]
        if self.kind == self.CLOSE:
            event = {'type': 'websocket.close', 'code': self.code}
            if self.reason:
                event['reason'] = self.reason
            return [event]
        # SEND — exactly the key that is set, which is what the ``WebSocket``
        # object put on this channel before it went native.  ASGI permits
        # either shape (both keys with one ``None``, or just the set one); the
        # compat surface must not change under existing consumers.
        if self.text is not None:
            return [{'type': 'websocket.send', 'text': self.text}]
        return [{'type': 'websocket.send', 'bytes': self.data}]


#: RFC 9113 §8.4 — a promised request's authority is its parent's.
_PUSH_HOST = 'push cannot carry host: its authority is the parent request\'s'


def _refuse_push_host(fields: _MinimalResponseHeaders | None) -> None:
    if fields is not None and any(name == b'host' for name, _ in fields):
        raise ValueError(_PUSH_HOST)


class _PushPath:
    __slots__ = ('path',)

    def __init__(self, path: str) -> None:
        self.path = path


class NativeResponse:
    """A response on the native send path: header and/or body and/or trailers.

    ``header`` and ``trailers`` keep the response-field contract from creation
    on: names are lowercase tchar and values carry no CTL.  A field that cannot
    meet it raises ``ValueError``/``TypeError`` here; readers do not check again.
    ``header`` is ``None`` when absent (never ``[]``).  ``body`` is ``None``
    when absent; ``b''`` is a real empty body.  ``more_body`` marks a
    non-terminal body chunk.  ``push`` makes this a promised request instead:
    its path is ``push`` and ``header`` holds request headers, with no
    response-only fields and no ``host`` (the authority is the parent's).
    ``more_trailers`` marks a non-terminal trailer event.  ``expects_trailers``
    (ASGI ``trailers: True``) makes the sender withhold the terminal chunk until
    the trailers event.
    """

    __slots__ = (
        '_body',
        '_header',
        'expects_trailers',
        '_extension',
        'more_body',
        'more_trailers',
        'status',
        '_trailers',
    )

    def __init__(self, *, status: int = 200,
                 header: list[tuple[bytes, bytes]] | None = None,
                 body: bytes | None = None,
                 more_body: bool = False,
                 trailers: list[tuple[bytes, bytes]] | None = None,
                 more_trailers: bool = False,
                 expects_trailers: bool = False,
                 file_path: str | None = None,
                 push: str | None = None) -> None:
        self.status = status
        # Keep constructor normalization synchronized with the header setter.
        self._header = (header._items if isinstance(header, _HeaderView)
                        else None if header is None else _as_response_fields(header))
        self._body = body
        self.more_body = more_body
        self._trailers = None if trailers is None else _as_response_fields(trailers)
        self.more_trailers = more_trailers
        self.expects_trailers = expects_trailers
        # Sendfile form: the response body *is* this file, and the sender is
        # free to hand it to ``loop.sendfile`` rather than read it into a
        # ``body``.  This is the ``http.response.pathsend`` ASGI extension's
        # function without its dict shape, so a framework-owned producer
        # (``StaticFiles``) can stay native and still get zero-copy.
        # Mutually exclusive with ``body``: the bytes come from the file.
        self._extension: str | _PushPath | None = file_path
        if push is not None:
            self.push = push

    # Shape-specific constructors avoid misordering adjacent completion flags.
    # Application code uses the public keyword constructor.

    @classmethod
    def complete(cls, status: int, header: list[tuple[bytes, bytes]],
                 body: bytes | None) -> 'NativeResponse':
        """Header plus terminal body — a whole response in one object."""
        self = cls.__new__(cls)
        self.status = status
        self._header = _as_response_fields(header)
        self._body = body
        self.more_body = False
        self._trailers = None
        self.more_trailers = False
        self.expects_trailers = False
        self._extension = None
        return self

    @classmethod
    def with_trailers(cls, status: int, header: list[tuple[bytes, bytes]],
                      body: bytes | None,
                      trailers: list[tuple[bytes, bytes]]) -> 'NativeResponse':
        """Header, body and trailers together — the gRPC unary shape.

        ``more_body`` is True by construction, and load-bearing:
        ``HTTP2Sender`` only takes its trailers-coalescing path for a
        *non-terminal* body chunk, holding HEADERS + DATA so they flush with
        the trailing HEADERS in one write.  END_STREAM rides the trailers
        either way (RFC 9113 §8.1).
        """
        self = cls.__new__(cls)
        self.status = status
        self._header = _as_response_fields(header)
        self._body = body
        self.more_body = True
        self._trailers = _as_response_fields(trailers)
        self.more_trailers = False
        self.expects_trailers = True
        self._extension = None
        return self

    # File sends and pushes are exclusive; only a push allocates its payload.
    @property
    def file_path(self) -> str | None:
        extension = self._extension
        return None if isinstance(extension, _PushPath) else extension

    @file_path.setter
    def file_path(self, value: str | None) -> None:
        if isinstance(self._extension, _PushPath):
            if value is not None:
                raise ValueError('push cannot carry a file')
        else:
            self._extension = value

    @property
    def push(self) -> str | None:
        extension = self._extension
        return extension.path if isinstance(extension, _PushPath) else None

    @push.setter
    def push(self, value: str | None) -> None:
        if value is None:
            if isinstance(self._extension, _PushPath):
                self._extension = None
            return
        if (self.status != 200 or self._body is not None or self.more_body
                or self._trailers is not None or self.more_trailers
                or self.expects_trailers
                or (self._extension is not None
                    and not isinstance(self._extension, _PushPath))):
            raise ValueError('push cannot carry response status, body, trailers, or file')
        _refuse_push_host(self._header)
        self._extension = _PushPath(value)

    # --- header: DX view, or None when absent -----------------------------
    @property
    def header(self) -> _HeaderView | None:
        """The header arm as a mutable view, or ``None`` when there is none."""
        if self._header is None:
            return None
        return _HeaderView(self._header, self)

    @header.setter
    def header(self, value) -> None:
        if value is None:
            self._header = None
        else:
            header = (value._items if isinstance(value, _HeaderView)
                      else _as_response_fields(value))
            if isinstance(self._extension, _PushPath):
                _refuse_push_host(header)
            self._header = header

    @property
    def trailers(self) -> _HeaderView | None:
        """The trailer arm as a mutable view, or ``None`` when there is none."""
        if self._trailers is None:
            return None
        return _HeaderView(self._trailers)

    @trailers.setter
    def trailers(self, value) -> None:
        self._trailers = None if value is None else _as_response_fields(value)

    # --- body: plain bytes; DX via helper properties -----------------------
    @property
    def body(self) -> bytes | None:
        """The body arm, or ``None`` when there is none.  ``b''`` is a real body."""
        return self._body

    @body.setter
    def body(self, value: bytes | None) -> None:
        self._body = value

    @property
    def content_length(self) -> int:
        """Octets in the body arm; ``0`` when there is no body."""
        return len(self._body) if self._body is not None else 0

    @property
    def is_empty(self) -> bool:
        """True when the body arm is absent *or* zero-length.

        Distinct from ``body is None``, which separates the two.
        """
        return self._body is None or self._body == b''

    @property
    def content_type(self) -> bytes:
        """The ``content-type`` header value, or ``b''`` if unset or headerless."""
        hv = self.header
        return hv.get(b'content-type') if hv is not None else b''

    # --- boundary conversion (asgi=True path only) -------------------------
    def to_asgi(self) -> list[dict]:
        """Convert to the ASGI event list (``http.response.*`` dicts).

        One object → one or more ASGI events, in wire order.  Used only at
        conversion boundaries — the external ASGI edge (external hosts /
        ``asgi=True``) and the middleware native-read arms (cache,
        compression); the native H1 sender path never materialises these
        dicts.
        """
        extension = self._extension
        if isinstance(extension, _PushPath):
            return [{'type': 'http.response.push', 'path': extension.path,
                     'headers': list(self._header) if self._header is not None else []}]
        # Both sections before the first event: the external-ASGI boundary
        # sends the returned list in order, so a bad trailer found after the
        # start/body would be too late.  Each is a copy, so an append on the
        # live response (CORS) cannot reach an event the cache middleware
        # stored.
        header = (_owned_response_fields(self._header)
                  if self._header is not None else None)
        trailers = (_owned_response_fields(self._trailers)
                    if self._trailers is not None else None)

        events: list[dict] = []
        if header is not None:
            start: dict = {'type': 'http.response.start',
                           'status': self.status,
                           'headers': header}
            if self.expects_trailers:
                start['trailers'] = True
            events.append(start)
        if extension is not None:
            events.append({'type': 'http.response.pathsend',
                           'path': extension})
        if self._body is not None:
            events.append({'type': 'http.response.body',
                           'body': self._body,
                           'more_body': self.more_body})
        if trailers is not None:
            trailer_event: dict = {
                'type': 'http.response.trailers',
                'headers': trailers,
            }
            if self.more_trailers:
                trailer_event['more_trailers'] = True
            events.append(trailer_event)
        return events


def _native_from_asgi(event):
    """Convert HTTP send events; leave other event types unchanged.

    Each converted field list is the result's own copy, checked unless it
    already keeps the response-field contract.
    """
    kind = event.get('type')
    if kind == 'http.response.start':
        return NativeResponse(
            status=int(event.get('status', 200)),
            header=_owned_response_fields(event.get('headers') or ()),
            expects_trailers=bool(event.get('trailers', False)))
    if kind == 'http.response.body':
        # None would skip the body arm and leave buffered headers unflushed.
        return NativeResponse(body=event.get('body') or b'',
                              more_body=bool(event.get('more_body', False)))
    if kind == 'http.response.trailers':
        return NativeResponse(
            trailers=_owned_response_fields(event.get('headers') or ()),
            more_trailers=bool(event.get('more_trailers', False)))
    if kind == 'http.response.pathsend':
        return NativeResponse(file_path=event['path'])
    if kind == 'http.response.push':
        return NativeResponse(push=event.get('path', '/'),
                              header=_owned_response_fields(event.get('headers') or ()))
    return event


def asgi_send_boundary(inner_send):
    """Expand native HTTP/WebSocket messages to ASGI dictionaries at a boundary.

    Use for external hosts and scope-declared middleware. Pass already-ASGI
    values through unchanged.
    """
    async def _send(event):
        if isinstance(event, (NativeResponse, NativeWSMessage)):
            for ev in event.to_asgi():
                await inner_send(ev)
        else:
            await inner_send(event)

    return _send

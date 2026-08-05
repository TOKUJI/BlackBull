"""Native response message for the H1 send path (native-ization, Sprint 92).

The unified response message BlackBull's own server carries on the native
path.  One class replaces the ASGI start/body/trailers dicts: a response
object may carry any combination of ``header`` (status line + headers),
``body`` (chunks), and ``trailers``, so a complete response is **one object
and one ``send``**, while streaming is header-object then body-chunk objects.

Design invariants (validated in ``scratch/send-model-c.py``):

- **Presence is ``is not None``, never truthiness** — an empty body is a real
  body (204-style).  Middleware must preserve ``None`` when transforming
  (``header or []`` turns ``None`` into ``[]`` and the sender mis-detects a
  header — a real bug caught in the scratch model).
- **DX via properties, not the wire shape** — ``resp.header`` is a zero-copy
  view with ``get``/``append``/``getlist``/``__contains__``/``__len__``/
  ``__iter__`` (BlackBull ``Headers``-like); ``body`` is a plain ``bytes``
  with ``content_length``/``is_empty``/``content_type`` helpers.
- **Server hot path may read the raw slots** (``_header``/``_body``) to skip
  the property+view overhead; the properties exist for middleware and
  handler-facing DX.
- **``to_asgi()`` is the boundary conversion** — 1 object → ASGI event list,
  used only at conversion boundaries: the external ASGI edge (``asgi=True``
  / external hosts) and the middleware native-read arms (symmetric with
  :meth:`Connection.as_scope`).
"""
from __future__ import annotations


class _HeaderView:
    """Zero-copy view over a :class:`NativeResponse` header list.

    Mutations (``append``) are visible to anything reading the response
    afterwards (the sender, ``to_asgi``).  Models the DX of
    :class:`blackbull.headers.Headers` without a copy — lookups are
    case-insensitive (RFC 9110 §5.1), matching ``Headers``.
    """

    __slots__ = ('_items',)

    def __init__(self, items: list[tuple[bytes, bytes]]) -> None:
        self._items = items

    def __iter__(self):
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, name: bytes) -> bool:
        lowered = name.lower()
        return any(k == name or k.lower() == lowered for k, _ in self._items)

    def get(self, name: bytes, default: bytes = b'') -> bytes:
        lowered = name.lower()
        for k, v in self._items:
            if k == name or k.lower() == lowered:
                return v
        return default

    def getlist(self, name: bytes) -> list[tuple[bytes, bytes]]:
        lowered = name.lower()
        return [(k, v) for k, v in self._items
                if k == name or k.lower() == lowered]

    def append(self, name_or_pairs, value: bytes | None = None) -> None:
        if value is None:
            # One-arg form: a *list* of pairs to extend with.  A bare
            # (name, value) 2-tuple is a footgun — ``extend`` would walk its
            # elements (two bytes) as separate entries and corrupt the list —
            # so a 2-tuple of (bytes, bytes) is treated as one pair.
            if (isinstance(name_or_pairs, tuple) and len(name_or_pairs) == 2
                    and isinstance(name_or_pairs[0], (bytes, str))):
                self._items.append(name_or_pairs)
            else:
                self._items.extend(name_or_pairs)
        else:
            self._items.append((name_or_pairs, value))


class NativeWSMessage:
    """One message on the native WebSocket send channel.

    The WS counterpart of :class:`NativeResponse`, and it exists for the same
    reason.  HTTP got a native send message in Sprint 92/93 and the sender a
    native arm; WebSocket was carried along as "conn is native, no scope" while
    its *event channel* stayed ASGI-shaped — so ``websocket.*`` dicts still
    travelled object → middleware → actor → sender on BlackBull's own path.
    The handler never saw them (that is the :class:`~blackbull.websocket.WebSocket`
    object's whole point), but everything under it did.

    Three kinds, discriminated by :attr:`kind` rather than by which of seven
    fields happens to be set — the variants carry disjoint payloads, so a tag
    reads better here than the presence test that suits ``NativeResponse``'s
    combinable arms:

    - ``ACCEPT`` — ``subprotocol`` / ``headers``; completes the handshake.
    - ``SEND`` — exactly one of ``text`` (``str``) or ``data`` (``bytes``).
    - ``CLOSE`` — ``code`` / ``reason``.

    ``data`` rather than ``bytes``: the ASGI key is ``bytes``, but a slot of
    that name shadows the builtin at every use site inside the class.
    :meth:`to_asgi` maps it back for the boundary.
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
        :meth:`NativeResponse.to_asgi` on the HTTP side.
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


class NativeResponse:
    """A response on the native send path: header and/or body and/or trailers.

    ``header`` is ``None`` when absent (never ``[]`` — presence is decided by
    ``is not None``).  ``body`` is ``None`` when absent; ``b''`` is a real
    empty body.  ``more_body`` marks a non-terminal body chunk (streaming).
    ``expects_trailers`` preserves the ASGI ``http.response.start``
    ``trailers: True`` flag so the sender withholds the terminal chunk until
    the trailers event (lossless full-form compat — a terminal body before
    trailers would otherwise corrupt chunked framing).
    """

    __slots__ = (
        '_body',
        '_header',
        'expects_trailers',
        'file_path',
        'more_body',
        'status',
        'trailers',
    )

    def __init__(self, *, status: int = 200,
                 header: list[tuple[bytes, bytes]] | None = None,
                 body: bytes | None = None,
                 more_body: bool = False,
                 trailers: list[tuple[bytes, bytes]] | None = None,
                 expects_trailers: bool = False,
                 file_path: str | None = None) -> None:
        self.status = status
        self.header = header          # setter stores into _header
        self._body = body
        self.more_body = more_body
        self.trailers = trailers
        self.expects_trailers = expects_trailers
        # Sendfile form: the response body *is* this file, and the sender is
        # free to hand it to ``loop.sendfile`` rather than read it into a
        # ``body``.  This is the ``http.response.pathsend`` ASGI extension's
        # function without its dict shape, so a framework-owned producer
        # (``StaticFiles``) can stay native and still get zero-copy.
        # Mutually exclusive with ``body``: the bytes come from the file.
        self.file_path = file_path

    # --- header: DX view, or None when absent -----------------------------
    @property
    def header(self) -> _HeaderView | None:
        if self._header is None:
            return None
        return _HeaderView(self._header)

    @header.setter
    def header(self, value) -> None:
        if value is None:
            self._header = None
        elif isinstance(value, _HeaderView):
            self._header = value._items
        else:
            self._header = value

    # --- body: plain bytes; DX via helper properties -----------------------
    @property
    def body(self) -> bytes | None:
        return self._body

    @body.setter
    def body(self, value: bytes | None) -> None:
        self._body = value

    @property
    def content_length(self) -> int:
        return len(self._body) if self._body is not None else 0

    @property
    def is_empty(self) -> bool:
        return self._body is None or self._body == b''

    @property
    def content_type(self) -> bytes:
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
        events: list[dict] = []
        if self._header is not None:
            start: dict = {'type': 'http.response.start',
                           'status': self.status,
                           # Copy: the cache middleware stores these events,
                           # and an in-place append on the live response
                           # (CORS / header injection) must not leak into a
                           # stored entry via list aliasing.
                           'headers': list(self._header)}
            if self.expects_trailers:
                start['trailers'] = True
            events.append(start)
        if self.file_path is not None:
            events.append({'type': 'http.response.pathsend',
                           'path': self.file_path})
        if self._body is not None:
            events.append({'type': 'http.response.body',
                           'body': self._body,
                           'more_body': self.more_body})
        if self.trailers is not None:
            events.append({'type': 'http.response.trailers',
                           'headers': list(self.trailers)})
        return events

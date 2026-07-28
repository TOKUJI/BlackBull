"""ASGI protocol types: event-type string constants, typed message shapes,
and typed event wrappers.

Three responsibilities, all framework-side:

- ``ASGIEvent``: namespace of ASGI 3.0 event-type strings, used for
  ``match``/``case`` dispatch and equality checks across both the
  framework and (importing from here) the server stack.  Every constant is
  ``Final``, so a type checker infers a ``Literal`` and comparisons against
  them narrow the event unions below.
- The 19 ``TypedDict`` message shapes and the two direction unions
  (``ASGIReceiveEvent`` / ``ASGISendEvent``) plus the two channel callable
  aliases (``ASGIReceiveCallable`` / ``ASGISendCallable``).  These are
  declarations only — erased at runtime, no wire or dispatch change.
- ``ResponseStart`` / ``ResponseBody``: dict-subclass wrappers for the
  two outgoing response event shapes so middleware can dispatch on
  Python type rather than string comparison.  Both subclass ``dict``
  so ``isinstance(e, dict)`` remains True — required for beartype and
  any ASGI send callable annotated ``event: dict``.

**Naming rule**: an ``ASGI`` prefix marks boundary vocabulary and is
quarantined to this module; unprefixed names (``Connection``, ``Headers``,
``Request``, ``Response``) are BlackBull's native layer.  The dicts on the
channel are ASGI 3.0 events verbatim — the spec is free documentation for
every key — so ``rg "from .asgi import"`` stays a literal map of the
remaining ASGI surface.
"""
from collections.abc import Awaitable, Callable
from typing import Final, Literal, NotRequired, TypedDict

from .headers import HeaderList, Headers


class ASGIEvent:
    """Namespace for ASGI protocol event type strings (ASGI 3.0 spec)."""

    # HTTP
    HTTP_REQUEST:           Final = 'http.request'
    HTTP_DISCONNECT:        Final = 'http.disconnect'
    HTTP_RESPONSE_START:    Final = 'http.response.start'
    HTTP_RESPONSE_BODY:     Final = 'http.response.body'
    HTTP_RESPONSE_TRAILERS: Final = 'http.response.trailers'
    HTTP_RESPONSE_PUSH:     Final = 'http.response.push'
    HTTP_RESPONSE_PATHSEND: Final = 'http.response.pathsend'

    # WebSocket
    WS_CONNECT:    Final = 'websocket.connect'
    WS_ACCEPT:     Final = 'websocket.accept'
    WS_RECEIVE:    Final = 'websocket.receive'
    WS_SEND:       Final = 'websocket.send'
    WS_CLOSE:      Final = 'websocket.close'
    WS_DISCONNECT: Final = 'websocket.disconnect'

    # Lifespan
    LIFESPAN_STARTUP:           Final = 'lifespan.startup'
    LIFESPAN_STARTUP_COMPLETE:  Final = 'lifespan.startup.complete'
    LIFESPAN_STARTUP_FAILED:    Final = 'lifespan.startup.failed'
    LIFESPAN_SHUTDOWN:          Final = 'lifespan.shutdown'
    LIFESPAN_SHUTDOWN_COMPLETE: Final = 'lifespan.shutdown.complete'
    LIFESPAN_SHUTDOWN_FAILED:   Final = 'lifespan.shutdown.failed'


# --------------------------------------------------------------------------
# Message shapes (ASGI 3.0 §HTTP, §WebSocket, §Lifespan)
#
# The ``type`` key must be spelled as a literal string, not as
# ``Literal[ASGIEvent.X]`` — ``Literal[...]`` only accepts literal
# expressions, never a name, even a ``Final`` one.  The constants above and
# the tags below are kept in the same order so drift is visible in a diff.
#
# ``headers`` fields use ``HeaderList`` (``Iterable[tuple[bytes, bytes]]``),
# which a ``Headers`` instance satisfies — so the sender's
# place-``Headers``-as-is idiom stays typeable without a conversion.
# --------------------------------------------------------------------------

# --- HTTP: receive direction ----------------------------------------------

class HTTPRequestEvent(TypedDict):
    """Incoming request body chunk."""

    type: Literal['http.request']
    body: NotRequired[bytes]
    more_body: NotRequired[bool]


class HTTPDisconnectEvent(TypedDict):
    """Client went away before the response completed."""

    type: Literal['http.disconnect']


# --- HTTP: send direction -------------------------------------------------

class HTTPResponseStartEvent(TypedDict):
    """Response status line + headers."""

    type: Literal['http.response.start']
    status: int
    headers: NotRequired[HeaderList]
    trailers: NotRequired[bool]


class HTTPResponseBodyEvent(TypedDict):
    """Response body chunk; ``more_body`` keeps the response open."""

    type: Literal['http.response.body']
    body: NotRequired[bytes]
    more_body: NotRequired[bool]


class HTTPResponseTrailersEvent(TypedDict):
    """Trailing headers, sent after the final body chunk."""

    type: Literal['http.response.trailers']
    headers: NotRequired[HeaderList]
    more_trailers: NotRequired[bool]


class HTTPResponsePushEvent(TypedDict):
    """HTTP/2 server push (ASGI *extension*, not baseline ASGI 3.0)."""

    type: Literal['http.response.push']
    path: str
    headers: NotRequired[HeaderList]


class HTTPResponsePathsendEvent(TypedDict):
    """Hand a file path to the server to send (ASGI *extension*)."""

    type: Literal['http.response.pathsend']
    path: str


# --- WebSocket ------------------------------------------------------------

class WebSocketConnectEvent(TypedDict):
    """Handshake offered; the app answers with accept or close."""

    type: Literal['websocket.connect']


class WebSocketAcceptEvent(TypedDict):
    """Complete the handshake."""

    type: Literal['websocket.accept']
    subprotocol: NotRequired[str | None]
    headers: NotRequired[HeaderList]


class WebSocketReceiveEvent(TypedDict):
    """One complete message from the client.

    BlackBull's recipient always sets *both* keys, one of them ``None``
    (``FragmentAssembler`` has already reassembled any fragments), so the
    value types are optional rather than the keys being mutually exclusive.
    """

    type: Literal['websocket.receive']
    bytes: NotRequired[bytes | None]
    text: NotRequired[str | None]


class WebSocketSendEvent(TypedDict):
    """One complete message to the client."""

    type: Literal['websocket.send']
    bytes: NotRequired[bytes | None]
    text: NotRequired[str | None]


class WebSocketCloseEvent(TypedDict):
    """Close the connection (app-initiated)."""

    type: Literal['websocket.close']
    code: NotRequired[int]
    reason: NotRequired[str | None]


class WebSocketDisconnectEvent(TypedDict):
    """Connection closed (peer- or transport-initiated)."""

    type: Literal['websocket.disconnect']
    code: NotRequired[int]
    reason: NotRequired[str | None]


# --- Lifespan -------------------------------------------------------------

class LifespanStartupEvent(TypedDict):
    """Server is starting; run startup hooks."""

    type: Literal['lifespan.startup']


class LifespanStartupCompleteEvent(TypedDict):
    """Startup hooks finished successfully."""

    type: Literal['lifespan.startup.complete']


class LifespanStartupFailedEvent(TypedDict):
    """Startup hooks raised; ``message`` carries the reason."""

    type: Literal['lifespan.startup.failed']
    message: NotRequired[str]


class LifespanShutdownEvent(TypedDict):
    """Server is stopping; run shutdown hooks."""

    type: Literal['lifespan.shutdown']


class LifespanShutdownCompleteEvent(TypedDict):
    """Shutdown hooks finished successfully."""

    type: Literal['lifespan.shutdown.complete']


class LifespanShutdownFailedEvent(TypedDict):
    """Shutdown hooks raised; ``message`` carries the reason."""

    type: Literal['lifespan.shutdown.failed']
    message: NotRequired[str]


# --------------------------------------------------------------------------
# Direction unions and channel callables — 7 + 12 = 19.
# --------------------------------------------------------------------------

ASGIReceiveEvent = (
    HTTPRequestEvent
    | HTTPDisconnectEvent
    | WebSocketConnectEvent
    | WebSocketReceiveEvent
    | WebSocketDisconnectEvent
    | LifespanStartupEvent
    | LifespanShutdownEvent
)

ASGISendEvent = (
    HTTPResponseStartEvent
    | HTTPResponseBodyEvent
    | HTTPResponseTrailersEvent
    | HTTPResponsePushEvent
    | HTTPResponsePathsendEvent
    | WebSocketAcceptEvent
    | WebSocketSendEvent
    | WebSocketCloseEvent
    | LifespanStartupCompleteEvent
    | LifespanStartupFailedEvent
    | LifespanShutdownCompleteEvent
    | LifespanShutdownFailedEvent
)

# Never reference these two aliases from a NamedTuple field — beartype's
# forward-reference resolver rejects a module-level ``Callable`` alias used
# that way.  Inline the ``Callable[...]`` form there instead.
ASGIReceiveCallable = Callable[[], Awaitable[ASGIReceiveEvent]]
ASGISendCallable = Callable[[ASGISendEvent], Awaitable[None]]


class ResponseStart(dict):
    """http.response.start event with typed property access."""

    @property
    def status(self) -> int:
        return self.get('status', 200)          # type: ignore[return-value]

    @property
    def headers(self) -> Headers:
        raw = self.get('headers', [])
        return raw if isinstance(raw, Headers) else Headers(raw)


class ResponseBody(dict):
    """http.response.body event with typed property access."""

    @property
    def body(self) -> bytes:
        return self.get('body', b'')            # type: ignore[return-value]

    @property
    def more_body(self) -> bool:
        return self.get('more_body', False)     # type: ignore[return-value]


def parse_response_event(
    event: ASGISendEvent,
) -> ResponseStart | ResponseBody | ASGISendEvent:
    """Wrap *event* in the appropriate typed subclass for dispatch.

    The returned object IS the event dict (shallow copy) — pass it directly
    to downstream send callables without re-serialisation.
    Trailers and unknown event types are returned unchanged.

    ``ResponseStart``/``ResponseBody`` are dict subclasses, not statically
    members of ``ASGISendEvent``; a caller threading the result back into a
    typed send callable casts at that seam (a runtime no-op) rather than
    widening the public union.
    """
    match event.get('type'):
        case ASGIEvent.HTTP_RESPONSE_START:
            return ResponseStart(event)
        case ASGIEvent.HTTP_RESPONSE_BODY:
            return ResponseBody(event)
    return event

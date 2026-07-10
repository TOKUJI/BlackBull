"""ASGI protocol types: event-type string constants and typed event wrappers.

Two responsibilities, both framework-side:

- ``ASGIEvent``: namespace of ASGI 3.0 event-type strings, used for
  ``match``/``case`` dispatch and equality checks across both the
  framework and (importing from here) the server stack.
- ``ResponseStart`` / ``ResponseBody``: dict-subclass wrappers for the
  two outgoing response event shapes so middleware can dispatch on
  Python type rather than string comparison.  Both subclass ``dict``
  so ``isinstance(e, dict)`` remains True — required for beartype and
  any ASGI send callable annotated ``event: dict``.
"""
from .headers import Headers


class ASGIEvent:
    """Namespace for ASGI protocol event type strings (ASGI 3.0 spec)."""

    # HTTP
    HTTP_REQUEST           = 'http.request'
    HTTP_DISCONNECT        = 'http.disconnect'
    HTTP_RESPONSE_START    = 'http.response.start'
    HTTP_RESPONSE_BODY     = 'http.response.body'
    HTTP_RESPONSE_TRAILERS = 'http.response.trailers'
    HTTP_RESPONSE_PUSH     = 'http.response.push'
    HTTP_RESPONSE_PATHSEND = 'http.response.pathsend'

    # WebSocket
    WS_CONNECT    = 'websocket.connect'
    WS_ACCEPT     = 'websocket.accept'
    WS_RECEIVE    = 'websocket.receive'
    WS_SEND       = 'websocket.send'
    WS_CLOSE      = 'websocket.close'
    WS_DISCONNECT = 'websocket.disconnect'

    # Lifespan
    LIFESPAN_STARTUP           = 'lifespan.startup'
    LIFESPAN_STARTUP_COMPLETE  = 'lifespan.startup.complete'
    LIFESPAN_STARTUP_FAILED    = 'lifespan.startup.failed'
    LIFESPAN_SHUTDOWN          = 'lifespan.shutdown'
    LIFESPAN_SHUTDOWN_COMPLETE = 'lifespan.shutdown.complete'
    LIFESPAN_SHUTDOWN_FAILED   = 'lifespan.shutdown.failed'


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


def parse_response_event(event: dict) -> ResponseStart | ResponseBody | dict:
    """Wrap *event* in the appropriate typed subclass for dispatch.

    The returned object IS the event dict (shallow copy) — pass it directly
    to downstream send callables without re-serialisation.
    Trailers and unknown event types are returned unchanged.
    """
    match event.get('type'):
        case ASGIEvent.HTTP_RESPONSE_START:
            return ResponseStart(event)
        case ASGIEvent.HTTP_RESPONSE_BODY:
            return ResponseBody(event)
    return event

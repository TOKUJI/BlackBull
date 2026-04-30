"""ASGI protocol event wrappers with typed property access.

Provides dict-subclass wrappers for the two response event shapes so
middleware can dispatch on Python type rather than string comparison.
Both subclass ``dict`` so ``isinstance(e, dict)`` remains True — required
for typeguard and any ASGI send callable annotated ``event: dict``.
"""
from .server.headers import Headers


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
        case 'http.response.start':
            return ResponseStart(event)
        case 'http.response.body':
            return ResponseBody(event)
    return event

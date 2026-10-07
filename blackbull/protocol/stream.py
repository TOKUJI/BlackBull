"""HTTP/2 stream state. Stream 0 belongs to the connection; native
request state stays a Connection even on compatibility lanes.
"""
from enum import Enum


class StreamState(Enum):
    """HTTP/2 stream states per RFC 7540 §5.1."""
    IDLE             = 'idle'
    OPEN             = 'open'
    HALF_CLOSED_LOCAL  = 'half-closed (local)'
    HALF_CLOSED_REMOTE = 'half-closed (remote)'
    CLOSED           = 'closed'

class Stream:
    """HTTP/2 stream state with a native Connection dispatch target.

    Stream 0 is connection-level state. Active requests are indexed as root
    children; legacy dependency priority frames do not build a scheduling tree.
    Client stream ids are odd; server push ids are even. Senders own default windows.
    """

    __slots__ = (
        'parent', 'weight', 'stream_id', 'window_size',
        'children', 'conn', 'state', 'priority_hint',
        'expected_content_length', 'received_data_bytes',
    )

    def __init__(self, stream_id: int, parent: 'Stream | None' = None,
                 weight: int = 1, window_size: int | None = None):
        self.parent = parent
        self.weight = weight
        self.stream_id = stream_id
        if window_size:
            self.window_size = window_size

        self.children = {}
        # Native HTTP and WebSocket streams carry Connection objects.
        self.conn = None
        self.state = StreamState.IDLE
        self.priority_hint: dict[str, int | bool] | None = None
        # RFC 9113 §8.1.2.6 — content-length tracking.  ``expected_content_length``
        # is parsed from the request's content-length header (None if absent);
        # ``received_data_bytes`` accumulates DATA-frame payload lengths so the
        # peer's declared length can be checked at END_STREAM.
        self.expected_content_length: int | None = None
        self.received_data_bytes: int = 0

    def on_headers_received(self, end_stream: bool) -> None:
        """Transition state on HEADERS frame (RFC 7540 §5.1)."""
        if end_stream:
            self.state = StreamState.HALF_CLOSED_REMOTE
        else:
            self.state = StreamState.OPEN

    def on_data_received(self, end_stream: bool) -> None:
        """Transition state on DATA frame (RFC 9113 §5.1).

        The peer's END_STREAM closes only *their* half: the stream becomes
        half-closed (remote), from which WINDOW_UPDATE / PRIORITY /
        RST_STREAM remain legal.  Full CLOSED is reached when the server
        side finishes too (stream-task done-callback prunes the node) or
        via RST_STREAM.
        """
        if end_stream:
            self.state = StreamState.HALF_CLOSED_REMOTE

    def add_child(self, stream_id):
        existing = self.find_child(stream_id)
        if existing is not None:
            return existing

        child = Stream(stream_id, self)
        self.children[child.stream_id] = child

        return child

    def drop_child(self, stream_id):
        del self.children[stream_id]

    def get_children(self):
        r = []
        for c in self.children.values():
            r.append(c)
            r += c.get_children()
        return r

    def find_child(self, stream_id):
        """Find a stream id, returning None on a miss.
        """
        if self.stream_id == stream_id:
            return self

        child = self.children.get(stream_id)
        if child is not None:
            return child

        # Slow path: only descend if at least one child has children of its own.
        for v in self.children.values():
            if v.children:
                r = v.find_child(stream_id)
                if r is not None:
                    return r

        return None

    def __repr__(self):
        return f'Stream(ID: {self.stream_id}, conn={self.conn}, state={self.state})'

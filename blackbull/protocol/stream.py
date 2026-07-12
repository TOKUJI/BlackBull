"""HTTP/2 stream state and the priority tree (RFC 7540 §5).

Each ``Stream`` node owns the ASGI ``scope`` for one in-flight request as
well as its position in the dependency tree (parent + weight) used by the
priority machinery.  ``StreamState`` enumerates the lifecycle states
defined in RFC 7540 §5.1; ``on_headers_received`` / ``on_data_received``
perform the corresponding transitions.

Stream identifier ``0`` is reserved for connection-level frames and is the
root of every priority tree.
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
    """One node in the HTTP/2 stream-priority tree (RFC 7540 §5.1, §5.3).

    A ``Stream`` carries the ASGI ``scope`` for one in-flight request,
    plus its position in the priority tree (parent + weight).

    ``identifier == 0`` is the connection-level pseudo-stream
    (RFC 7540 §5.1.1) and acts as the root of the tree; SETTINGS, PING,
    GOAWAY, and connection-level WINDOW_UPDATE frames target it.
    Client-initiated request streams use odd identifiers, server-pushed
    streams use even identifiers (RFC 7540 §5.1.1).

    ``window_size`` defaults to the connection's initial flow-control window
    when omitted (the value lives on the sender, not the stream).
    """

    # Per-stream object — allocated once per HTTP/2 stream and stored in the
    # priority tree.  ``__slots__`` saves the ~56-byte ``__dict__`` overhead
    # at the cost of forbidding dynamic attribute assignment.  Every
    # attribute referenced anywhere on the class must be declared here.
    __slots__ = (
        'parent', 'weight', 'stream_id', 'window_size',
        'children', 'scope', 'state', 'priority_hint',
        'closed_via_rst',
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
        self.scope = None
        self.state = StreamState.IDLE
        self.priority_hint: dict[str, int | bool] | None = None
        # RFC 9113 §5.1 — distinguishes closed-via-END_STREAM (normal close)
        # from closed-via-RST_STREAM.  Influences whether late frames are
        # treated as connection or stream errors.
        self.closed_via_rst: bool = False
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

    def on_rst_received(self) -> None:
        """Transition on incoming RST_STREAM (RFC 9113 §5.1).

        Marks the stream CLOSED and remembers that it was closed via
        RST_STREAM (vs END_STREAM).  The state is retained so later
        frames arriving on the same identifier can be detected and the
        right error type returned.
        """
        self.state = StreamState.CLOSED
        self.closed_via_rst = True

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
        """Locate a node by stream_id.

        BlackBull only nests streams under their priority parent in the rare
        case where PRIORITY arrives for a stream whose dependent_stream is
        another peer stream rather than root.  h2 in the wild — and h2load
        in particular — never exercises this; every peer-initiated stream
        ends up as a direct child of root.  Walking the entire subtree on a
        miss therefore wasted O(N) per lookup once we stopped pruning closed
        streams for §5.1 state validation.  Keep the fast path flat; recurse
        only when the priority tree actually has depth.
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
        return f'Stream(ID: {self.stream_id}, scope={self.scope}, state={self.state})'

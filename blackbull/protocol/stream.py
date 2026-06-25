"""HTTP/2 stream state and the priority tree (RFC 7540 §5).

Each ``Stream`` node owns the ASGI ``scope`` and ``event`` for one in-flight
request as well as its position in the dependency tree (parent + weight) used
by the priority machinery.  ``StreamState`` enumerates the lifecycle states
defined in RFC 7540 §5.1; ``on_headers_received`` / ``on_data_received``
perform the corresponding transitions.

Stream identifier ``0`` is reserved for connection-level frames and is the
root of every priority tree.
"""
import asyncio
from enum import Enum
from urllib.parse import urlparse

from .frame_types import PseudoHeaders, DataFrameFlags, HeaderFrameFlags, FrameTypes
import logging
logger = logging.getLogger(__name__)


class StreamState(Enum):
    """HTTP/2 stream states per RFC 7540 §5.1."""
    IDLE             = 'idle'
    OPEN             = 'open'
    HALF_CLOSED_LOCAL  = 'half-closed (local)'
    HALF_CLOSED_REMOTE = 'half-closed (remote)'
    CLOSED           = 'closed'

class Stream:
    """One node in the HTTP/2 stream-priority tree (RFC 7540 §5.1, §5.3).

    A ``Stream`` carries the ASGI ``scope`` and ``event`` for one in-flight
    request, plus its position in the priority tree (parent + weight).

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
        'children', 'scope', 'event', '_lock',
        'end_stream', 'state', 'priority_hint',
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
        self.event = None
        self._lock = None

        self.end_stream = False
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
        """Transition state on DATA frame (RFC 7540 §5.1)."""
        if end_stream:
            self.state = StreamState.CLOSED

    def on_rst_received(self) -> None:
        """Transition on incoming RST_STREAM (RFC 9113 §5.1).

        Marks the stream CLOSED and remembers that it was closed via
        RST_STREAM (vs END_STREAM).  The state is retained so later
        frames arriving on the same identifier can be detected and the
        right error type returned.
        """
        self.state = StreamState.CLOSED
        self.closed_via_rst = True

    def mark_locally_closed(self) -> None:
        """Mark this stream CLOSED after the server-side response is done.

        Called from the stream task's done-callback so that late frames
        from the peer on this identifier hit the CLOSED branch of the
        state-machine validation.
        """
        self.state = StreamState.CLOSED

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

    def max_stream_id(self):
        if not self.children:
            return self.stream_id
        return max([c.max_stream_id() for c in self.get_children()])

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

    def update_event(self, data=None):
        """ Make or update the event by the data frame. """
        if not self.event:
            self.event = {'type': 'http.request', 'body': ''}

        if not data:
            return self.event
        self.event['body'] = data.payload

        return self.event

    def update_scope(self, headers=None,):
        """ Make or update the scope by the headers. """
        if not self.scope:
            self.scope = {'type': 'http', 'http_version': "2", 'headers': []}

        if not headers:
            return self.scope

        pseudo = headers.pseudo_headers
        self.scope['method'] = pseudo.get(PseudoHeaders.METHOD, self.scope.get('method', 'GET'))
        self.scope['scheme'] = pseudo.get(PseudoHeaders.SCHEME, self.scope.get('scheme', 'https'))

        # ASGI requires scope['path'] to be the *decoded path component*
        # and scope['query_string'] to be the raw query *as bytes*.  The
        # HTTP/2 ``:path`` pseudo-header carries both joined by ``?``
        # (RFC 9113 §8.3.1), so split them before populating the scope.
        # See bench/sprint-logs/sprint-27.md carry-forward.
        raw_path = pseudo.get(PseudoHeaders.PATH, self.scope.get('path', '/'))
        if isinstance(raw_path, bytes):
            raw_path = raw_path.decode('utf-8')
        parsed = urlparse(raw_path)
        self.scope['path']         = parsed.path
        self.scope['raw_path']     = parsed.path.encode('utf-8')
        self.scope['query_string'] = parsed.query.encode('utf-8')
        self.scope['root_path']    = ''
        self.scope['client']       = None

        if PseudoHeaders.AUTHORITY in pseudo:
            self.scope['headers'].append(pseudo[PseudoHeaders.AUTHORITY].split(':'))

        self.scope['headers'].extend(headers.headers)

        return self.scope

    def get_lock(self):
        if not self.is_locked:
            self._lock = asyncio.Condition()
        return self._lock

    def release(self):
        if self._lock is not None:
            self._lock.release()

    @property
    def is_locked(self) -> bool:
        if not self._lock:
            return False
        return self._lock.locked()

    @property
    def is_eos(self) -> bool:
        return self.end_stream

    def flip_eos(self):
        self.end_stream = True
        # self.end_stream = False

    def close(self):
        [child.close() for child in self.children.values()]
        if self.parent is not None:
            self.parent.drop_child(self.stream_id)

    def __repr__(self):
        return f'Stream(ID: {self.stream_id}, scope={self.scope}, end_stream={self.end_stream})'


def eos(frame):  # eos: end of stream
    # Guarded: eos() runs on every DATA/HEADERS frame; the f-string (which
    # formats the frame repr) must not be built when DEBUG is off (Tier 1).
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug('%s, %s, %s', frame, frame.FrameType(), frame.flags)
    if frame.FrameType() == FrameTypes.DATA and frame.flags & DataFrameFlags.END_STREAM > 0 or\
       frame.FrameType() == FrameTypes.HEADERS and frame.flags & HeaderFrameFlags.END_STREAM > 0:
        return True
    return False

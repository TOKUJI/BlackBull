from functools import partial
import traceback

from ..protocol.stream import Stream
from ..protocol.frame import FrameTypes, PseudoHeaders
from ..logger import get_logger_set, log
from .headers import Headers

logger, _ = get_logger_set(__name__)


def _make_scope():
    return {
        'type': 'http',
        'asgi': {
            'version': '3.0',
            'spec_version': '2.2',
        },
        'http_version': '2',
        'method': 'HEAD',
        'scheme': 'https',
        'path': '',
        'raw_path': '',
        'query_string': '',
        'root_path': '',
        'headers': [],
        'client': [],
        'server': [],
    }


class ParserFactory:
    """Dispatches an incoming HTTP/2 frame to the parser registered for its type.

    Each ``HTTP2ParserBase`` subclass registers itself in
    ``HTTP2ParserBase._registry`` keyed on its ``FRAME_TYPE``;
    ``Get(frame, stream)`` looks up that registry and instantiates the matching
    parser bound to the given stream.
    """

    @staticmethod
    def Get(frame, stream) -> 'HTTP2ParserBase':
        return HTTP2ParserBase._registry[frame.FrameType()](frame, stream)


class HTTP2ParserBase:
    """Abstract base for HTTP/2 frame parsers that build an ASGI scope.

    Subclasses set ``FRAME_TYPE`` to the ``FrameTypes`` value they handle and
    implement ``parse()``.  ``__init_subclass__`` auto-registers each concrete
    subclass in ``_registry`` so ``ParserFactory.Get`` can dispatch by frame type.
    Setting ``FRAME_TYPE = None`` keeps a class abstract (skipped at registration).
    """

    FRAME_TYPE = None
    _registry = {}

    def __init__(self, frame, stream):
        self.frame = frame
        self.stream = stream
        self.stream_id = frame.stream_id

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

        # If FRAME_TYPE is None, we won't register it, as it's meant to be an abstract base class
        if cls.FRAME_TYPE is None:
            return

        # Check for duplicate FRAME_TYPE to avoid overwriting existing entries in the registry
        if cls.FRAME_TYPE in cls._registry:
            raise ValueError(f"Duplicate FRAME_TYPE: {cls.FRAME_TYPE}")

        # Register the subclass in the registry based on its FRAME_TYPE
        # This allows us to create instances of the correct subclass based on the frame type
        cls._registry[cls.FRAME_TYPE] = cls

    def parse(self, payload=None):
        raise NotImplementedError()


class HTTP2HEADParser(HTTP2ParserBase):
    """Parses an HTTP/2 HEADERS frame into an ASGI ``http`` scope dict.

    Pulls ``:method`` / ``:path`` / ``:scheme`` from the frame's pseudo-headers,
    encodes the regular headers as ``bytes`` pairs into a ``Headers`` object,
    and resolves ``root_path`` from the ``X-Forwarded-Prefix`` header.
    """

    FRAME_TYPE = FrameTypes.HEADERS
    def __init__(self, frame, stream):
        super().__init__(frame, stream)

    def parse(self, payload=None):
        scope = _make_scope()

        if method := self.frame.pseudo_headers.get(PseudoHeaders.METHOD):
            scope['method'] = method

        if path := self.frame.pseudo_headers.get(PseudoHeaders.PATH):
            scope['path'] = path

        if scheme := self.frame.pseudo_headers.get(PseudoHeaders.SCHEME):
            scope['scheme'] = scheme

        scope['headers'] = Headers(
            [(k.encode(), v.encode()) for k, v in self.frame.headers]
        )

        scope['root_path'] = scope['headers'].get(
            b'x-forwarded-prefix', b''
        ).decode('utf-8')

        return scope


class HTTP2DATAParser(HTTP2ParserBase):
    """Parses an HTTP/2 DATA frame.  Currently returns a fresh empty ASGI scope;
    body bytes are delivered via ``HTTP2Recipient`` rather than this parser.
    """

    FRAME_TYPE = FrameTypes.DATA
    def __init__(self, frame, stream):
        super().__init__(frame, stream)

    def parse(self, payload=None):
        scope = _make_scope()
        return scope

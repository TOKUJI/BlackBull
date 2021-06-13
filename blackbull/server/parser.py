from functools import partial
import traceback

from ..stream import Stream
from ..frame import FrameTypes
from ..logger import get_logger_set, log

logger, _ = get_logger_set(__name__)


def _make_scope():
    return {
        'type': 'http',
        'asgi': {
            'version': '3.0',
            'spec_version': '2.2',
        },
        'http_version': '2.0',
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
    """docstring for ParserFactory"""
    @staticmethod
    def Get(frame, stream):
        parsers = {klass.Type(): klass for klass in HTTP2ParserBase.__subclasses__()}
        return parsers[frame.FrameType()](frame, stream)


class HTTP2ParserBase:
    """docstring for HTTP2ParserBase"""
    def __init__(self, frame, stream):
        self.frame = frame
        self.stream = stream
        self.stream_id = frame.stream_id

    def parse(self):
        raise NotImplementedError()

    @staticmethod
    def Type():
        raise NotImplementedError()


class HTTP2HEADParser(HTTP2ParserBase):
    """docstring for HTTP2HEADParser"""
    def __init__(self, frame, stream):
        super(HTTP2HEADParser, self).__init__(frame, stream)

    @staticmethod
    def Type():
        return FrameTypes.HEADERS

    def parse(self):
        scope = _make_scope()
        scope['method'] = self.frame[':method']
        scope['path'] = self.frame[':path']
        scope['scheme'] = self.frame[':scheme']
        return scope


class HTTP2DATAParser(HTTP2ParserBase):
    """docstring for HTTP2DATAParser"""
    def __init__(self, frame, stream):
        super().__init__(frame, stream)

    @staticmethod
    def Type():
        return FrameTypes.DATA

    def parse(self):
        scope = _make_scope()
        return scope

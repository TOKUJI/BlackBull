import asyncio
from urllib.parse import urlparse

from .frame import PseudoHeaders, DataFrameFlags, HeaderFrameFlags, FrameTypes
from ..logger import get_logger_set
logger, log = get_logger_set(__name__)

class Stream:
    def __init__(self, identifier, parent=None, weight=1, window_size=None):
        from collections import deque
        self.parent = parent
        self.weight = weight
        self.identifier = identifier
        if window_size:
            self.window_size = window_size

        self.children = {}
        self.scope = None
        self.event = None
        self._lock = None

        self.end_stream = False

    def add_child(self, id_):
        if id_ in self.get_children():
            return

        child = Stream(id_, self, )
        self.children[child.identifier] = child

        return child

    def drop_child(self, id_):
        del self.children[id_]

    def get_children(self):
        r = []
        for c in self.children.values():
            r.append(c)
            r += c.get_children()
        return r

    def max_stream_id(self):
        if not self.children:
            return self.identifier
        return max([c.max_stream_id() for c in self.get_children()])

    def find_child(self, identifier):
        if self.identifier == identifier:
            return self

        if identifier in self.children:
            return self.children[identifier]

        for k, v in self.children.items():
            r = v.find_child(identifier)
            if r:
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
        self.scope['path']   = pseudo.get(PseudoHeaders.PATH,   self.scope.get('path', '/'))

        if 'path' in self.scope:
            parsed = urlparse(self.scope['path'])
            self.scope['query_string'] = parsed.query
            self.scope['root_path'] = ''
            self.scope['client'] = None

        if PseudoHeaders.AUTHORITY in pseudo:
            self.scope['headers'].append(pseudo[PseudoHeaders.AUTHORITY].split(':'))

        self.scope['headers'].extend(headers.headers)

        return self.scope

    def get_lock(self):
        if not self.is_locked():
            self._lock = asyncio.Condition()
        return self._lock

    def release(self):
        self._lock.release()

    def is_locked(self):
        if not self._lock:
            return False
        return self._lock.locked()

    def is_eos(self):
        return self.end_stream

    def flip_eos(self):
        self.end_stream = True
        # self.end_stream = False

    def close(self):
        [child.close() for child in self.children.values()]
        self.parent.drop_child(self.identifier)
        del self

    def __repr__(self):
        return f'Stream(ID: {self.identifier}, scope={self.scope}, end_stream={self.end_stream})'


def eos(frame):  # eos: end of stream
    logger.debug(f'{frame}, {frame.FrameType()}, {frame.flags}')
    if frame.FrameType() == FrameTypes.DATA and frame.flags & DataFrameFlags.END_STREAM > 0 or\
       frame.FrameType() == FrameTypes.HEADERS and frame.flags & HeaderFrameFlags.END_STREAM > 0:
        return True
    return False

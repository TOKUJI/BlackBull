from logging import getLogger
from collections import defaultdict
import asyncio
import re
from enum import Enum, auto
import socket
from contextlib import closing

logger = getLogger('utils')


async def do_nothing(*args, **kwargs):
    pass


class HTTPMethods(Enum):
    get = auto()
    put = auto()
    post = auto()
    delete = auto()
    patch = auto()


class Scheme(Enum):
    http = 'http'
    websocket = 'websocket'


def check_port(host='localhost', port=None):
    """
    Returns True: Port is not used.
    Returns False: Port is used.
    """
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        if sock.connect_ex((host, port)) == 0:
            return False
        else:
            return True


def pop_safe(key, source, target, *, new_key=None):

    if key in source:
        if new_key:
            target[new_key] = source.pop(key)
        else:
            target[key] = source.pop(key)
    return target


class serializable(object):
    """ This is ABC of util.serializable classes. Any derived class of this class
    should implement save and load method.
    todo: use abstract base class
    """
    re = r''

    def is_empty(self):
        return NotImplementedError('serializable.is_empty()')

    def save(self):
        return NotImplementedError('serializable.save()')

    @classmethod
    def load(cls, str_):  # must return a pair (serializable, remaining_text)
        return NotImplementedError('serializable.load()')


# RFC 5322 Date and Time specification
IMFFixdate = '%a, %d %b %Y %H:%M:%S %Z'


URI = r'/?[0-9a-zA-Z]*?/?'
HTTP2 = b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'
WebSocket = b'GET /chat HTTP/1.1\r\n'
# 0x505249202a20485454502f322e300d0a0d0a534d0d0a0d0a


def parse_post_data(string):
    matches = re.findall(r'(\w+)=(\w+)&?', string)
    d = {k: v for k, v in matches}
    return d


class EventEmitter:
    """ Asynchronous EventEmitter? """
    def __init__(self):
        self._listeners = defaultdict(list)
        self._listeners_once = defaultdict(list)

    def on(self, event, listener):
        """ Register a lister to this class. This listener will be called unless removed."""
        self._listeners[event].append(listener)

    def once(self, event, listener):
        """ Register a lister to this class. """
        self._listeners_once[event].append(listener)

    def off(self, event, listener):
        l = self._listeners[event]
        index = max(loc for loc, val in enumerate(l) if val == listener)
        l.pop(index)

    def emit(self, event, *args, **kwds):
        for l in self._listeners[event]:
            async def f(*args, **kwds):
                await event.wait()
                l(*args, **kwds)

            asyncio.create_task(f(*args, **kwds))

        for l in self._listeners_once[event]:
            async def f(*args, **kwds):
                await event.wait()
                l(*args, **kwds)

            asyncio.create_task(f(*args, **kwds))
        self._listeners_once.pop(event)

        event.set()


# type definitions
class MessageType(Enum):
    REQUEST = auto()
    RESPONSE = auto()


class HeaderFields(Enum):
    CONTENT_TYPE = 'Content-Type'
    TRANSFER_ENCODING = 'Transfer-Encoding'


class TransferCodings(Enum):
    CHUNKED = 'chunked'
    COMPRESS = 'compress'
    DEFLATE = 'deflate'
    GZIP = 'gzip'

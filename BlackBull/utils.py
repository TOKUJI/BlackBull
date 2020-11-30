from logging import getLogger
from functools import wraps, partial
from collections import defaultdict
import asyncio
import re
from collections import UserDict
from enum import Enum, auto
import socket
from contextlib import closing

logger = getLogger(__name__)


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
# 0x505249202a20485454502f322e300d0a0d0a534d0d0a0d0a


def parse_post_data(string):
    matches = re.findall(r'(\w+)=(\w+)&?', string)
    d = {k: v for k, v in matches}
    return d


# http://taichino.com/programming/1538
class Router(UserDict):
    """
    This class has 2 dictionaries: self.data and self.regex_.
    key: str or re.Pattern
    value: (function, methods)
    """
    f_string = re.compile(r'\{([a-zA-Z_]\w*?)\}', flags=re.ASCII)

    def __init__(self, *args, **kwds):
        super(Router, self).__init__(*args, **kwds)
        self.regex_ = {}

    def __setitem__(self, key, value):
        """
        If 'key' is a str, this class holds it for the key and
        its compiled regular expression objects.
        If key is a regular expression objects, this class keeps it in self.regex_
        """
        if isinstance(key, str):
            self.data[key] = value

            s = self.f_string.sub(r'(?P<\1>[a-zA-Z0-9_\-\.\~]+)', key)
            self.regex_[re.compile(s)] = value

        elif isinstance(key, re.Pattern):
            self.regex_[key] = value

    def __getitem__(self, key):
        if key in self.data:
            return self.data[key]

        # @todo Consider to use List class for self.regex_ to improve performance.
        # Because self.regex_ is merely used as an array like container in this class.
        for p, (fn, methods) in self.regex_.items():
            if m := p.match(key):
                if gdict := m.groupdict():
                    fn = partial(fn, **gdict)
                return (fn, methods)

        raise KeyError(f'{key} is not found')

    def __contains__(self, item):
        if item in self.data:
            return True

        for k in self.regex_.keys():
            if m := k.match(item):
                logger.debug(f'{k} matches {item}? {m}')
                return True

        return False

    def route(self, methods=['GET'], path='/'):
        """ Register a function in the routing table of this server. """
        def register(fn):
            @wraps(fn)
            def wrapper(*args, **kwds):
                return fn(*args, **kwds)

            # Convert method names to uppercase characters.
            if isinstance(methods, str):
                self.__setitem__(path, (wrapper, [methods.upper()]))

            else:  # TODO: should check whether method is iterable or not
                self.__setitem__(path, (wrapper, [method.upper() for method in methods]))

            return wrapper
        return register


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

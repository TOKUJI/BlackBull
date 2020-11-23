from functools import wraps
from collections import defaultdict
import asyncio

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
    def load(cls, str_): # must return a pair (serializable, remaining_text)
        return NotImplementedError('serializable.load()')

import datetime
# RFC 5322 Date and Time specification
IMFFixdate = '%a, %d %b %Y %H:%M:%S %Z'


import re

URI = r'/?[0-9a-zA-Z]*?/?'
HTTP2 = b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'
# 0x505249202a20485454502f322e300d0a0d0a534d0d0a0d0a
def parse_post_data(string):
    matches = re.findall(r'(\w+)=(\w+)&?', string)
    d = {k:v for k, v in matches}
    return d

# http://taichino.com/programming/1538
from collections import UserDict
class RouteRecord(UserDict):
    """docstring for RouteRecord"""
    def __init__(self, *args, **kwds):
        super(RouteRecord, self).__init__(*args, **kwds)
        self.regex_ = {}

    def __setitem__(self, key, value):
        if isinstance(key, re.Pattern):
            self.regex_[key] = value
        else:
            self.data[key] = value
            if key[-1] != '$':
                key = key + '$'
            self.regex_[re.compile(key)] = value

    def __getitem__(self, key):
        try:
            return self.data[key]
        except:
            for k, v in self.regex_.items():
                if k.match(key):
                    return v
            raise KeyError('{} is not found'.format(key))

    def __contains__(self, item):
        if item in self.data:
            return True
        else:
            for k in self.regex_.keys():
                m = k.match(item)
                _logger.debug('{} matches {}? {}'.format(k, item, m))
                if m:
                    return True

        return False

    def find(self, path):
        m = self.__getitem__(path)
        return m[0], m[1]

    def route(self, method='GET', path='/'):
        """ Register a function in the routing table of this server. """
        def register(fn):
            @wraps(fn)
            def wrapper(*args, **kwds):
                return fn(*args, **kwds)

            if isinstance(method, str):
                self.__setitem__(path, (wrapper, [method]))
            else: # TODO: should check whether method is iterable or not
                self.__setitem__(path, (wrapper, method))

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
from enum import Enum, auto

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

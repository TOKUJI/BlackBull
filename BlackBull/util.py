from functools import wraps

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
            else:
                self.__setitem__(path, (wrapper, method))

            return wrapper
        return register

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


from collections import UserDict
from typing import Tuple, Type
from functools import wraps, partial
import re
from logging import getLogger

from .utils import Scheme, HTTPMethods, do_nothing

logger = getLogger(__name__)


class BaseRouter:
    def __setitem__(self, key: Tuple[str, Type[Scheme]], value):
        raise NotImplementedError()

    def __getitem__(self, key: Tuple[str, Type[Scheme]]):
        raise NotImplementedError()

    def __contains__(self, item):
        raise NotImplementedError()

    def route(self, methods=[HTTPMethods.get], path='/', scheme=Scheme.http, functions=[]):
        raise NotImplementedError()

    def route_404(self):
        raise NotImplementedError()


# http://taichino.com/programming/1538
class Router(UserDict, BaseRouter):
    """
    This class has 2 dictionaries: self.data and self.regex_.
    key: str or re.Pattern
    value: (function, methods, scheme)
    """
    f_string = re.compile(r'\{([a-zA-Z_]\w*?)\}', flags=re.ASCII)
    NOT_FOUND_KEY = 'NOT_FOUND'

    def __init__(self, *args, **kwds):
        super(Router, self).__init__(*args, **kwds)
        self.regex_ = {}

    def __setitem__(self, key: Tuple[str, Type[Scheme]], value):
        """
        If 'key' is a str, this class holds it for the key and
        its compiled regular expression objects.
        If key is a regular expression objects, this class keeps it in self.regex_
        """
        path, scheme = key
        logger.debug(key)
        if isinstance(path, str):
            self.data[key] = value

            s = self.f_string.sub(r'(?P<\1>[a-zA-Z0-9_\-\.\~]+)', path)
            self.regex_[(re.compile(f'^{s}$'), scheme)] = value

        elif isinstance(path, re.Pattern):
            self.regex_[key] = value
        else:
            logger.error(f'Unexpected type ({key}.)')

    def __getitem__(self, key: Tuple[str, Type[Scheme]]):
        key_path, key_scheme = key
        logger.debug(key)
        if key in self.data:
            logger.debug(self.data[key])
            return self.data[key]

        # @todo Consider to use List or some iterable class for self.regex_ to improve performance.
        # Because self.regex_ is merely used as an array like container in this class.
        for (p, scheme), (fn, methods) in self.regex_.items():
            logger.debug((p, scheme, fn, methods))

            if (m := p.match(key_path)) and scheme == key_scheme:
                if gdict := m.groupdict():
                    fn = partial(fn, **gdict)
                return (fn, methods)

        logger.debug(f'No Entry: {key}, {self}')
        logger.debug(self.data)
        logger.debug(self.regex_)
        return self[(self.NOT_FOUND_KEY, key[-1])]

    def __contains__(self, item):
        if item in self.data:
            return True

        for k in self.regex_.keys():
            if m := k.match(item):
                logger.debug(f'{k} matches {item}? {m}')
                return True

        return False

    def route_fn(self, methods=[HTTPMethods.get], path='/', scheme=Scheme.http):
        logger.debug('Router.route() is called.')
        if [x for x in methods if not isinstance(x, HTTPMethods)]:
            raise ValueError('methods must be HTTPMethods.')

        def register(fn):
            logger.debug(f'Router.route.register() is called. {fn}')

            @wraps(fn)
            def wrapper(*args, **kwds):
                logger.debug('Router.route.register.wrapper() is called.')
                return fn(*args, **kwds)

            logger.debug((path, scheme))
            self[(path, scheme)] = (wrapper, methods)

            return wrapper

        return register

    def route(self, methods=[HTTPMethods.get], path='/', scheme=Scheme.http, functions=[]):
        """ Register a function or middlewares in the routing table of this server. """
        if not functions:
            return self.route_fn(methods, path, scheme)

        logger.debug(f'Router.route() is called. {functions}')

        if len(functions) == 0:
            logger.warning('There is no function in this routing request.')

        elif len(functions) == 1:
            fns = [partial(functions[0], inner=do_nothing)]

        else:
            fns = []
            inner = partial(functions[-1], inner=do_nothing)

            for fn in functions[-2::-1]:
                temp = partial(fn, **{'inner': inner})
                fns.append(temp)
                inner = temp

        self[(path, scheme)] = (fns[-1], methods)

    def route_404(self):
        """ Register a function for 404. """
        logger.debug('Router.route_404() is called.')
        fn = self.route(methods=[HTTPMethods.get], path=self.NOT_FOUND_KEY)

        logger.debug(self.data)
        logger.debug(self.regex_)
        return fn

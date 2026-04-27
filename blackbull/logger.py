import inspect
import logging
from functools import wraps
from inspect import iscoroutinefunction
from logging import Formatter
from copy import copy
from typing import Literal


def log(fn):
    """Decorator: log call arguments at DEBUG level.

    Automatically uses the logger of the module where @log is applied,
    determined by inspecting the caller's frame at decoration time.
    """
    frame = inspect.stack()[1]
    module_name = frame[0].f_globals.get('__name__', 'blackbull')
    _logger = logging.getLogger(module_name)

    if iscoroutinefunction(fn):
        @wraps(fn)
        async def async_wrapper(*args, **kwds):
            _logger.debug('%s(%s, %s)', fn.__name__, args, kwds)
            return await fn(*args, **kwds)
        return async_wrapper
    else:
        @wraps(fn)
        def wrapper(*args, **kwds):
            _logger.debug('%s(%s, %s)', fn.__name__, args, kwds)
            return fn(*args, **kwds)
        return wrapper


# https://stackoverflow.com/questions/384076/how-can-i-color-python-logging-output
MAPPING = {
    'DEBUG'   : 37,  # white
    'INFO'    : 36,  # cyan
    'WARNING' : 33,  # yellow
    'ERROR'   : 31,  # red
    'CRITICAL': 41,  # white on red bg
}

PREFIX = '\033['
SUFFIX = '\033[0m'


class ColoredFormatter(Formatter):

    def __init__(self, fmt=None, datefmt=None,
                 style: Literal['%', '{', '$'] = '%'):
        Formatter.__init__(self, fmt=fmt, datefmt=datefmt, style=style)

    def format(self, record):
        colored_record = copy(record)
        levelname = colored_record.levelname
        seq = MAPPING.get(levelname, 37)  # default white
        colored_levelname = ('{0}{1}m{2}{3}').format(PREFIX, seq, levelname, SUFFIX)

        colored_record.levelname = colored_levelname
        return Formatter.format(self, colored_record)


if __name__ == '__main__':
    import logging
    logger = logging.getLogger('blackbull.test')
    logger.setLevel(logging.DEBUG)

    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    cf = ColoredFormatter('%(levelname)-17s:%(name)s %(message)s')
    handler.setFormatter(cf)
    logger.addHandler(handler)

    logger.debug(logger.handlers)
    logger.debug('debug')
    logger.info('info')
    logger.warning('warning')
    logger.error('error')
    logger.critical('critical')

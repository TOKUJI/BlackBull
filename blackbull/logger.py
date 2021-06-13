from functools import wraps
from logging import getLogger, NullHandler, Formatter
from copy import copy
from asyncio import iscoroutinefunction


def get_logger_set(name=None):
    """ Returns a pair of logger and its decorator. """
    if name:
        logger = getLogger('blackbull').getChild(name)
    else:
        logger = getLogger('blackbull')
    logger.addHandler(NullHandler())

    def _log(fn):
        if iscoroutinefunction(fn):
            @wraps(fn)
            async def async_wrapper(*args, **kwds):
                logger.debug(f'{fn.__name__}({args}, {kwds})')
                res = await fn(*args, **kwds)
                return res
            return async_wrapper
        else:
            def wrapper(*args, **kwds):
                logger.debug(f'{fn.__name__}({args}, {kwds})')
                res = fn(*args, **kwds)
                return res
            return wrapper

    return logger, _log


def log(logger):
    def _log(fn):
        if iscoroutinefunction(fn):
            @wraps(fn)
            async def async_wrapper(*args, **kwds):
                logger.debug(f'{fn.__name__}({args}, {kwds})')
                res = await fn(*args, **kwds)
                return res
            return async_wrapper
        else:
            def wrapper(*args, **kwds):
                logger.debug(f'{fn.__name__}({args}, {kwds})')
                res = fn(*args, **kwds)
                return res
            return wrapper
    return _log


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

    def __init__(self, fmt=None, datefmt=None, style='%'):
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
    logger, _log = get_logger_set('test')
    # logging.basicConfig(level=logging.DEBUG)

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

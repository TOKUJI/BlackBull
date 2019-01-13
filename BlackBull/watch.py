from ctypes import cdll, Structure, c_int, c_uint, sizeof
from ctypes.util import find_library
from struct import unpack
from weakref import WeakValueDictionary
import asyncio
import os

# todo: move watch.py to another package.
from logging import getLogger
from functools import wraps
_logger = getLogger('watch')
def _log(fn):
    @wraps(fn)
    def wrapper(*args, **kwds):
        _logger.debug('{}({}, {})'.format(fn.__name__, args, kwds))
        res = fn(*args, **kwds)
        return res
    return wrapper



if find_library('c'):
    _LIB = cdll.LoadLibrary(find_library('c'))
else:
    _LIB = cdll.LoadLibrary('libc.so.6')


INOTIFY_EVENT = {
    # the following are legal, implemented events that user-space can watch for
    0x00000001:"IN_ACCESS", # File was accessed
    0x00000002:"IN_MODIFY", # File was modified
    0x00000004:"IN_ATTRIB", # Metadata changed
    0x00000008:"IN_CLOSE_WRITE", # Writtable file was closed
    0x00000010:"IN_CLOSE_NOWRITE", # Unwrittable file closed
    0x00000020:"IN_OPEN", # File was opened
    0x00000040:"IN_MOVED_FROM", # File was moved from X
    0x00000080:"IN_MOVED_TO", # File was moved to Y
    0x00000100:"IN_CREATE", # Subfile was created
    0x00000200:"IN_DELETE", # Subfile was deleted
    0x00000400:"IN_DELETE_SELF", # Self was deleted
    0x00000800:"IN_MOVE_SELF", # Self was moved

    # the following are legal events.  they are sent as needed to any watch 
    0x00002000:"IN_UNMOUNT", # Backing fs was unmounted 
    0x00004000:"IN_Q_OVERFLOW", # Event queued overflowed 
    0x00008000:"IN_IGNORED", #File was ignored 

    # special flags 
    0x01000000:"IN_ONLYDIR", # only watch the path if it is a directory 
    0x02000000:"IN_DONT_FOLLOW", # don't follow a sym link 
    0x04000000:"IN_EXCL_UNLINK", # exclude events on unlinked objects 
    0x10000000:"IN_MASK_CREATE", # only create watches 
    0x20000000:"IN_MASK_ADD", # add to the mask of an already existing watch 
    0x40000000:"IN_ISDIR", # event occurred against dir 
    0x80000000:"IN_ONESHOT", # only send event once 
    # helper events 
    # IN_CLOSE        (IN_CLOSE_WRITE | IN_CLOSE_NOWRITE)  close 
    # IN_MOVE         (IN_MOVED_FROM | IN_MOVED_TO)  moves 
}

INOTIFY_EVENT_NAME = {
    "IN_ACCESS":        0x00000001,
    "IN_MODIFY":        0x00000002,
    "IN_ATTRIB":        0x00000004,
    "IN_CLOSE_WRITE":   0x00000008,
    "IN_CLOSE_NOWRITE": 0x00000010,
    "IN_OPEN":          0x00000020,
    "IN_MOVED_FROM":    0x00000040,
    "IN_MOVED_TO":      0x00000080,
    "IN_CREATE":        0x00000100,
    "IN_DELETE":        0x00000200,
    "IN_DELETE_SELF":   0x00000400,
    "IN_MOVE_SELF":     0x00000800,
    "IN_UNMOUNT":       0x00002000,
    "IN_Q_OVERFLOW":    0x00004000,
    "IN_IGNORED":       0x00008000,
    "IN_ONLYDIR":       0x01000000,
    "IN_DONT_FOLLOW":   0x02000000,
    "IN_EXCL_UNLINK":   0x04000000,
    "IN_MASK_CREATE":   0x10000000,
    "IN_MASK_ADD":      0x20000000,
    "IN_ISDIR":         0x40000000,
    "IN_ONESHOT":       0x80000000,
}


class _InotifyEvent(Structure) :
    _fields_ = [
            ("wd", c_int),
            ("mask", c_uint),
            ("cookie", c_uint),
            ("len", c_uint),
            ]

    def __repr__(self):
        return 'wd:{}, mask:{}, cookie:{}, len:{}'.format(self.wd, self.get_event_names(self.mask), self.cookie, self.len)

    def get_event_names(self, event_type):
        names = []
        for bit, name in INOTIFY_EVENT.items():
            if event_type & bit:
                names.append(name)
                event_type -= bit

                if event_type == 0:
                    break

        assert event_type == 0, \
               "We could not resolve all event-types: (%d)" % (event_type,)
        return names

    @classmethod
    def size(cls):
        return sizeof(cls)


class Watcher(object):
    IN_CHANGED = INOTIFY_EVENT_NAME["IN_MODIFY"] \
                |INOTIFY_EVENT_NAME["IN_CLOSE_WRITE"]  \
                |INOTIFY_EVENT_NAME["IN_MOVED_FROM"] \
                |INOTIFY_EVENT_NAME["IN_MOVED_TO"] \
                |INOTIFY_EVENT_NAME["IN_CREATE"] \
                |INOTIFY_EVENT_NAME["IN_DELETE"] \
                |INOTIFY_EVENT_NAME["IN_DELETE_SELF"] \
                |INOTIFY_EVENT_NAME["IN_MOVE_SELF"] \

    _instances = WeakValueDictionary()
    _fd = None

    def __init__(self):
        self._instances[id(self)] = self

        self._fd =  _LIB.inotify_init()
        if self._fd < 0:
            raise OSError("could not initialize inotify")
        _logger.debug(self._fd)

        self._watch = {}
        self.max_path_length = 0

        _base  = os.execl
        @_log
        def _execl(*args, **kwds):
            self.__del__()
            _base(*args, **kwds)

        os.execl = _execl

    @_log
    def add_watch(self, path, callback=None, except_=[]):
        wd = _LIB.inotify_add_watch(self._fd, path.encode(), self.IN_CHANGED)

        if wd < 0:
            _logger.error('fd for inotify: {}, return value for inotify_add_watch: {}'.format(self._fd, wd))
            raise OSError("Could not add this file / directory in the watch list")

        self.max_path_length = max(self.max_path_length, os.statvfs(path).f_namemax)

        if except_ and isinstance(except_, str):
            except_ = [except_]

        self._watch[wd] = {'name':path, 'callback':callback, 'except': except_}
        _logger.debug('{} {}'.format(wd, self._watch[wd]))

        return wd

    @_log
    def handle_event(self):
        buf = os.read(self._fd, _InotifyEvent.size() + self.max_path_length + 1)
        header = _InotifyEvent(*unpack("@iIII", buf[:_InotifyEvent.size()]))
        _logger.debug('inotify header: {} '.format(header))

        pathname = buf[_InotifyEvent.size() : _InotifyEvent.size() + header.len].decode('utf-8').rstrip('\x00')
        _logger.debug('{} is not in {}'.format(pathname, self._watch[header.wd]['except']))

        if header.wd in self._watch \
            and self._watch[header.wd]['callback'] \
            and pathname not in self._watch[header.wd]['except']:
            _logger.debug(self._watch[header.wd])
            self._watch[header.wd]['callback']()
        self._event.set()

    @_log 
    async def watch(self):
        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(self._fd, self.handle_event)
        self._event = asyncio.Event()

        while True:
            await self._event.wait()
            self._event.clear()

    def __del__(self):
        os.close(self._fd)




def force_reload(path): # todo: should change to use functools.partial
    def reload(*args, **kwds):
        import sys
        _logger.debug('run {} {}'.format(sys.executable, path))
        os.execl(sys.executable, 'python', path)
    return reload


if __name__ == '__main__':
    import os.path
    from logging import StreamHandler, DEBUG

    _logger.setLevel(DEBUG)
    handler = StreamHandler()
    handler.setLevel(DEBUG)
    _logger.addHandler(handler)
    _logger.debug('logger is setted up')

    watcher = Watcher()
    watcher.add_watch('./', callback=force_reload(__file__), except_=__file__)
    _logger.debug('watch is added')

    try:
        asyncio.run(watcher.watch())
        print("could it be possible?")
    except KeyboardInterrupt:
        os._exit(0)

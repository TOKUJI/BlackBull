import socket
from operator import itemgetter, attrgetter

# private programs
from .logger import get_logger_set
logger, log = get_logger_set('socket')

def create_socket(address):
    host, port = address
    rsock = None
    info = socket.getaddrinfo(host, port, socket.AF_UNSPEC,
                              socket.SOCK_STREAM, 0, socket.AI_PASSIVE)

    info.sort(key=itemgetter(0), reverse=True) # sort descending by the address family

    for res in info:
        af, socktype, proto, canonname, sa = res
        try:
            rsock = socket.socket(af, socktype, proto)
        except OSError as msg:
            logger.error(msg)
            rsock = None
            continue
        try:
            rsock.bind(sa)
            rsock.listen(1)
        except OSError as msg:
            logger.error(msg)
            rsock.close()
            rsock = None
            continue
        break
    return rsock


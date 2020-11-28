import socket
from operator import itemgetter

# private programs
from .logger import get_logger_set
logger, log = get_logger_set('socket')


def create_socket(address):
    host, port = address
    rsock = None
    info = socket.getaddrinfo(host, port, socket.AF_UNSPEC,
                              socket.SOCK_STREAM, 0, socket.AI_PASSIVE)

    info.sort(key=itemgetter(0), reverse=True)  # sort descending by the address family

    """
    family: Address Family
    sockaddr: Socket Address. If protocol is 6, it is four tuple (host, port, flowinfo, scope_id).
    """
    for family, socktype, proto, canonname, sockaddr in info:
        logger.info((family, socktype, proto, canonname, sockaddr))

        try:
            rsock = socket.socket(family, socktype, proto)
        except OSError as msg:
            logger.error(msg)
            rsock = None
            continue

        try:
            rsock.bind(sockaddr)
            rsock.listen()
        except OSError as msg:
            logger.error(msg)
            rsock.close()
            rsock = None
            continue

        break  # Succeeded to open.

    return rsock

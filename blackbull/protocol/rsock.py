import socket

import logging
logger = logging.getLogger(__name__)


def _bind_socket(family, host, port):
    """
    Create, configure, bind and listen on a single socket for the given
    address *family* (``socket.AF_INET`` or ``socket.AF_INET6``).

    Returns the bound socket on success, or ``None`` if the address family is
    not supported on this platform or the port is already in use.
    """
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
    except OSError as msg:
        logger.error('Could not create socket (family=%s): %s', family, msg)
        return None

    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        if family == socket.AF_INET6:
            # Disable the IPv4-mapped address feature so that the IPv6 socket
            # handles *only* IPv6 traffic.  This lets both sockets coexist on
            # the same port without conflicts.
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)

        sock.bind((host, port))
        sock.listen()
        logger.info('Bound %s socket on %s:%s', family.name, host, port)
        return sock

    except OSError as msg:
        logger.error('Could not bind %s socket on %s:%s – %s', family.name, host, port, msg)
        sock.close()
        return None


def create_socket(address):
    """
    Create a **single** socket (legacy helper).

    The *host* in *address* determines the address family:
    an IPv6 literal (e.g. ``'::'``) opens an ``AF_INET6`` socket;
    anything else opens an ``AF_INET`` socket.

    Prefer :func:`create_dual_stack_sockets` for new code.
    """
    host, port = address

    # Choose address family based on the supplied host string.
    try:
        socket.inet_pton(socket.AF_INET6, host)
        family = socket.AF_INET6
    except OSError:
        family = socket.AF_INET

    return _bind_socket(family, host, port)


def create_dual_stack_sockets(port):
    """
    Create one IPv4 socket (``0.0.0.0``) **and** one IPv6 socket (``::``),
    both listening on *port*.

    Using two explicit sockets — each with ``IPV6_V6ONLY`` set on the IPv6
    one — is the most portable way to accept both IPv4 and IPv6 connections
    on all major platforms (Linux, macOS, Windows).

    When *port* is 0 (let the OS pick a free port), the IPv4 socket is bound
    first to obtain the assigned port number, then the IPv6 socket is bound to
    that **same** port.  This guarantees both sockets share a single port,
    which is what callers expect.

    Returns a list that contains whichever sockets were successfully bound
    (typically two, but may be one if the platform lacks IPv6 support).
    """
    sockets = []

    ipv4_sock = _bind_socket(socket.AF_INET, '0.0.0.0', port)
    if ipv4_sock is not None:
        sockets.append(ipv4_sock)
        if port == 0:
            # Learn the port the OS assigned so IPv6 uses the same one.
            port = ipv4_sock.getsockname()[1]

    ipv6_sock = _bind_socket(socket.AF_INET6, '::', port)
    if ipv6_sock is not None:
        sockets.append(ipv6_sock)

    if not sockets:
        logger.error('Failed to bind any socket on port %s.', port)

    return sockets

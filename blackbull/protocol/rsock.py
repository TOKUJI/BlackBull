import os
import socket

import logging
logger = logging.getLogger(__name__)

_DEFAULT_BACKLOG = 1024

#: True when the OS supports SO_REUSEPORT (Linux ≥ 3.9, macOS ≥ 10.6).
REUSEPORT_SUPPORTED = hasattr(socket, 'SO_REUSEPORT')

#: Env var holding a comma-separated list of fds the master has handed
#: to itself across ``os.execvp`` — see :func:`adopt_inherited_sockets`.
_INHERIT_FDS_ENV = 'BB_INHERIT_FDS'


def adopt_inherited_sockets() -> list[socket.socket] | None:
    """Build :class:`socket.socket` objects from fds inherited across exec.

    Returns ``None`` when no inherited fds are advertised (the normal
    cold-start path).  Returns a list of bound, listening sockets when
    the master process has re-exec'd itself for an auto-reload — the
    fds were marked inheritable, the env var ``BB_INHERIT_FDS`` was set
    to a comma-separated fd list, and they survived the exec.

    Callers MUST NOT bind/listen on the returned sockets — they are
    already in the listening state from before exec.

    The env var is cleared after adoption so child workers forked from
    this process do not also try to adopt the same fds.
    """
    spec = os.environ.get(_INHERIT_FDS_ENV)
    if not spec:
        return None
    try:
        fds = [int(s) for s in spec.split(',') if s]
    except ValueError:
        logger.error('Malformed %s=%r — ignoring', _INHERIT_FDS_ENV, spec)
        return None
    if not fds:
        return None

    sockets: list[socket.socket] = []
    for fd in fds:
        try:
            sock = socket.socket(fileno=fd)
        except OSError as exc:
            logger.error('Failed to adopt inherited fd %d: %s', fd, exc)
            continue
        # Mark the inherited socket non-inheritable for any further
        # fork+exec — only this generation of the master needs it.
        try:
            os.set_inheritable(sock.fileno(), False)
        except OSError:
            pass
        sockets.append(sock)
        logger.info('Adopted inherited listening socket fd=%d %s',
                    fd, sock.getsockname())

    # Clear the env var so workers forked from us don't try to re-adopt.
    del os.environ[_INHERIT_FDS_ENV]
    return sockets or None


def _bind_socket(family, host, port,
                 backlog: int = _DEFAULT_BACKLOG,
                 reuseport: bool = False,
                 sndbuf: int = 0, rcvbuf: int = 0,
                 keepalive: bool = True,
                 user_timeout_ms: int = 0):
    """
    Create, configure, bind and listen on a single socket for the given
    address *family* (``socket.AF_INET`` or ``socket.AF_INET6``).

    Returns the bound socket on success, or ``None`` if the address family is
    not supported on this platform or the port is already in use.

    *sndbuf* / *rcvbuf* (when non-zero) and TCP-keepalive options are set on
    the **listening** socket; on Linux these are inherited by accepted
    sockets, so per-connection setsockopt syscalls on the hot accept path
    can be avoided.
    """
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
    except OSError as msg:
        logger.error('Could not create socket (family=%s): %s', family, msg)
        return None

    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        if reuseport and REUSEPORT_SUPPORTED:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)

        if family == socket.AF_INET6:
            # Disable the IPv4-mapped address feature so that the IPv6 socket
            # handles *only* IPv6 traffic.  This lets both sockets coexist on
            # the same port without conflicts.
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)

        # SO_SNDBUF / SO_RCVBUF and TCP_USER_TIMEOUT are inherited by
        # accepted sockets on Linux, so set them once on the listening
        # socket and skip per-accept.  SO_KEEPALIVE is NOT inherited
        # (verified) — and we replaced it with an application-level
        # idle timer in HTTP1Actor (see ``keep_alive_timeout``).
        if sndbuf:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, sndbuf)
        if rcvbuf:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        # TCP_USER_TIMEOUT (Linux ≥ 2.6.37) — value in ms.  Catches the
        # case where a peer is silently dead during active transmission
        # (an ack never arrives); SO_KEEPALIVE only catches idle peers.
        if user_timeout_ms and hasattr(socket, 'TCP_USER_TIMEOUT'):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, user_timeout_ms)
        # The *keepalive* parameter is accepted for forward-compatibility
        # but no longer applied here; see callers.

        sock.bind((host, port))
        sock.listen(backlog)
        logger.info('Bound %s socket on %s:%s (backlog=%d reuseport=%s)',
                    family.name, host, port, backlog, reuseport and REUSEPORT_SUPPORTED)
        return sock

    except OSError as msg:
        logger.error('Could not bind %s socket on %s:%s – %s', family.name, host, port, msg)
        sock.close()
        return None


def create_socket(address, backlog: int = _DEFAULT_BACKLOG):
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

    return _bind_socket(family, host, port, backlog=backlog)


def create_dual_stack_sockets(port, backlog: int = _DEFAULT_BACKLOG,
                               reuseport: bool = False,
                               sndbuf: int = 0, rcvbuf: int = 0,
                               keepalive: bool = True,
                               user_timeout_ms: int = 0):
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

    ipv4_sock = _bind_socket(socket.AF_INET, '0.0.0.0', port,
                              backlog=backlog, reuseport=reuseport,
                              sndbuf=sndbuf, rcvbuf=rcvbuf, keepalive=keepalive,
                              user_timeout_ms=user_timeout_ms)
    if ipv4_sock is not None:
        sockets.append(ipv4_sock)
        if port == 0:
            # Learn the port the OS assigned so IPv6 uses the same one.
            port = ipv4_sock.getsockname()[1]

    ipv6_sock = _bind_socket(socket.AF_INET6, '::', port,
                              backlog=backlog, reuseport=reuseport,
                              sndbuf=sndbuf, rcvbuf=rcvbuf, keepalive=keepalive,
                              user_timeout_ms=user_timeout_ms)
    if ipv6_sock is not None:
        sockets.append(ipv6_sock)

    if not sockets:
        logger.error('Failed to bind any socket on port %s.', port)

    return sockets

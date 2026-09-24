"""Bound, listening sockets for the server to accept on.

Everything here hands back sockets already through ``bind()`` and ``listen()``
— the caller passes them to the event loop and never binds again.  A bind that
fails is reported as ``None`` (or an absence from the returned list) rather
than raised, so check what you got back.  Which function to call depends on
where the socket comes from:

- [`create_dual_stack_sockets`][blackbull.protocol.rsock.create_dual_stack_sockets]
  for a TCP port, one socket per family so both stacks are reached portably.
- [`create_unix_socket`][blackbull.protocol.rsock.create_unix_socket] for an
  ``AF_UNIX`` path.
- [`adopt_listening_fd`][blackbull.protocol.rsock.adopt_listening_fd] when a
  supervisor bound it — systemd socket activation, or ``--bind fd://N``.
- [`adopt_inherited_sockets`][blackbull.protocol.rsock.adopt_inherited_sockets]
  when the master re-exec'd itself and passed its own listeners across.

The last two return sockets that are *already* listening; binding them again
is an error.  ``SO_REUSEPORT`` is how several workers share one port, and the
module constant ``REUSEPORT_SUPPORTED`` says whether this host offers it.

See ``docs/deployment/unix-and-fd.md`` for the deployment shapes these serve.
"""
import os
import socket
import struct
import sys
import weakref

from .._cleanup import combine_cleanup_errors

import logging
logger = logging.getLogger(__name__)

_DEFAULT_BACKLOG = 1024

#: True when the OS supports SO_REUSEPORT (Linux ≥ 3.9, macOS ≥ 10.6).
REUSEPORT_SUPPORTED = hasattr(socket, 'SO_REUSEPORT')

#: Env var holding a comma-separated list of fds the master has handed
#: to itself across ``os.execvp`` — see [`adopt_inherited_sockets`][].
_INHERIT_FDS_ENV = 'BB_INHERIT_FDS'


#: The socket files this process bound: listener -> (path, inode, pid).  A
#: forked worker inherits the entry but not the pid, and an adopted socket
#: never has one, so neither removes a file another process listens on.
_BOUND_SOCKET_FILES: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _take_bound_socket_file(sock):
    try:
        return _BOUND_SOCKET_FILES.pop(sock, None)
    except TypeError:
        return None


def _unlink_bound_socket_file(record) -> None:
    """Remove the file unless another process bound it or something else has
    been bound at that path since."""
    if record is None:
        return
    path, inode, pid = record
    if pid != os.getpid():
        return
    try:
        if os.stat(path).st_ino == inode:
            os.unlink(path)
    except OSError:
        # Best effort: a file that cannot be removed must not fail shutdown.
        logger.debug('Socket file %s left in place', path, exc_info=True)


def close_sockets(sockets) -> Exception | None:
    """Close each live descriptor once and return any close failures."""
    unique = []
    aliases = []
    owners = {}
    for sock in sockets:
        try:
            fd = sock.fileno()
        except Exception:
            fd = -1
        key = ('fd', fd) if isinstance(fd, int) and fd >= 0 else ('object', id(sock))
        if key in owners:
            if owners[key] is not sock:
                aliases.append(sock)
            continue
        owners[key] = sock
        unique.append(sock)

    errors = []
    for sock in aliases:
        detach = getattr(sock, 'detach', None)
        if detach is None:
            continue
        try:
            detach()
        except Exception as exc:
            errors.append(exc)
            logger.exception('Failed to disarm aliased listening socket')
    for sock in unique:
        socket_file = _take_bound_socket_file(sock)
        try:
            sock.close()
        except Exception as exc:
            errors.append(exc)
            logger.exception('Failed to close listening socket')
        _unlink_bound_socket_file(socket_file)
    return combine_cleanup_errors(*errors)


def adopt_inherited_sockets() -> list[socket.socket] | None:
    """Build ``socket.socket`` objects from fds inherited across exec.

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
    sockets: list[socket.socket] = []
    try:
        try:
            tokens = spec.split(',')
            if any(not token for token in tokens):
                raise ValueError('empty fd token')
            fds = [int(token) for token in tokens]
            if any(fd < 0 for fd in fds):
                raise ValueError('negative fd')
        except ValueError as exc:
            raise RuntimeError(
                f'Malformed {_INHERIT_FDS_ENV}={spec!r}'
            ) from exc

        seen_fds = set()
        for fd in fds:
            if fd in seen_fds:
                raise RuntimeError(
                    f'Duplicate inherited fd {fd} cannot have multiple owners')
            seen_fds.add(fd)
            try:
                sock = socket.socket(fileno=fd)
            except (OSError, OverflowError, ValueError) as exc:
                raise RuntimeError(
                    f'Failed to adopt inherited fd {fd}: {exc}'
                ) from exc
            sockets.append(sock)
            try:
                os.set_inheritable(sock.fileno(), False)
                address = sock.getsockname()
            except OSError as exc:
                raise RuntimeError(
                    f'Failed to prepare inherited fd {fd}: {exc}'
                ) from exc
            logger.info('Adopted inherited listening socket fd=%d %s', fd, address)
        return sockets
    except BaseException:
        close_sockets(sockets)
        raise
    finally:
        # Workers forked from this process must never re-adopt this generation.
        os.environ.pop(_INHERIT_FDS_ENV, None)


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
        # (verified); the application-level idle timer in HTTP1Actor
        # (``keep_alive_timeout``) covers that ground instead.
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
        # but is not applied here; see callers.

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
    Create a **single** socket.

    The *host* in *address* determines the address family:
    an IPv6 literal (e.g. ``'::'``) opens an ``AF_INET6`` socket;
    anything else opens an ``AF_INET`` socket.

    Prefer [`create_dual_stack_sockets`][] for new code.
    """
    host, port = address

    # Choose address family based on the supplied host string.
    try:
        socket.inet_pton(socket.AF_INET6, host)
        family = socket.AF_INET6
    except OSError:
        family = socket.AF_INET

    return _bind_socket(family, host, port, backlog=backlog)


#: Systemd LISTEN_FDS protocol — first inherited fd starts at SD_LISTEN_FDS_START.
_SD_LISTEN_FDS_START = 3


def adopt_listening_fd(fd: int) -> socket.socket:
    """Build a ``socket.socket`` from an already-bound listening fd.

    Used for systemd socket-activation (``--bind fd://N``) and any other
    out-of-process socket hand-off where the supervisor binds and listens
    on the user's behalf.

    Validates the sd_listen_fds(3) contract when ``$LISTEN_PID`` and
    ``$LISTEN_FDS`` are present:

    * ``LISTEN_PID`` MUST equal our pid — if it points at someone else we
      raise rather than steal another process's fds.
    * ``LISTEN_FDS`` defines the inclusive window
      ``[SD_LISTEN_FDS_START, SD_LISTEN_FDS_START + LISTEN_FDS)`` — we
      reject fds outside that window.

    When neither env var is set we trust the caller and build the socket
    object anyway (useful for non-systemd handoff and for tests).

    The fd is *not* unset CLOEXEC — workers inherit it via fork, the
    multiworker path already handles set_inheritable as needed.
    """
    pid_env = os.environ.get('LISTEN_PID')
    if pid_env is not None:
        try:
            expected_pid = int(pid_env)
        except ValueError:
            raise RuntimeError(
                f"LISTEN_PID={pid_env!r} is not an integer"
            ) from None
        if expected_pid != os.getpid():
            raise RuntimeError(
                f'LISTEN_PID={expected_pid} does not match this process '
                f'({os.getpid()}) — refusing to adopt fd {fd}'
            )

    n_env = os.environ.get('LISTEN_FDS')
    if n_env is not None:
        try:
            n = int(n_env)
        except ValueError:
            raise RuntimeError(
                f"LISTEN_FDS={n_env!r} is not an integer"
            ) from None
        if not (_SD_LISTEN_FDS_START <= fd < _SD_LISTEN_FDS_START + n):
            raise RuntimeError(
                f'fd {fd} is outside the systemd LISTEN_FDS window '
                f'[{_SD_LISTEN_FDS_START}, {_SD_LISTEN_FDS_START + n})'
            )

    try:
        sock = socket.socket(fileno=fd)
    except OSError as exc:
        raise RuntimeError(f'Could not adopt fd {fd}: {exc}') from exc
    logger.info('Adopted listening fd %d %s (family=%s)',
                fd, sock.getsockname(), sock.family.name)
    return sock


def create_unix_socket(path: str, backlog: int = _DEFAULT_BACKLOG,
                       sndbuf: int = 0, rcvbuf: int = 0,
                       mode: int | None = 0o660) -> "socket.socket | None":
    """Bind and listen on an ``AF_UNIX`` socket at *path*.

    Returns the bound socket on success, ``None`` on any bind/listen failure.

    *path* is taken verbatim — no expanduser, no normalisation; the caller
    is responsible for placing the socket where their reverse proxy
    expects it.

    *mode* sets the socket-file permissions after bind (``chmod`` on the
    inode).  Defaults to ``0o660`` so a reverse proxy (nginx) running in
    the same group can connect; pass ``None`` to skip the chmod and
    inherit umask behaviour.

    A stale leftover socket file at *path* is unlinked before bind
    (matching the systemd / hypercorn / nginx behaviour).  We refuse to
    unlink if *path* is a regular file or directory — the user almost
    certainly didn't mean to point us at one.

    TCP-only socket options (``SO_REUSEPORT``, ``TCP_USER_TIMEOUT``,
    ``IPV6_V6ONLY``) are deliberately skipped — ``AF_UNIX`` doesn't carry
    them.  ``SO_SNDBUF`` / ``SO_RCVBUF`` *are* honoured when supplied.
    """
    import errno  # noqa: PLC0415
    import stat   # noqa: PLC0415

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError as msg:
        logger.error('Could not create AF_UNIX socket: %s', msg)
        return None

    # Unlink stale socket file — but only when it actually is a socket,
    # so we don't silently destroy unrelated user files.
    try:
        st_mode = os.stat(path).st_mode
    except FileNotFoundError:
        pass
    except OSError as msg:
        logger.error('Could not stat %s: %s', path, msg)
        sock.close()
        return None
    else:
        if stat.S_ISSOCK(st_mode):
            try:
                os.unlink(path)
            except OSError as msg:
                logger.error('Could not unlink stale UDS at %s: %s', path, msg)
                sock.close()
                return None
        else:
            logger.error('Refusing to bind UDS at %s: path exists and is not a socket', path)
            sock.close()
            return None

    try:
        if sndbuf:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, sndbuf)
        if rcvbuf:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        sock.bind(path)
        if mode is not None:
            try:
                os.chmod(path, mode)
            except OSError as msg:
                # chmod failure isn't fatal for binding; warn and keep going.
                logger.warning('Could not chmod %s to %o: %s', path, mode, msg)
        sock.listen(backlog)
        try:
            _BOUND_SOCKET_FILES[sock] = (path, os.stat(path).st_ino, os.getpid())
        except OSError:
            logger.debug('Socket file %s not tracked for removal', path,
                         exc_info=True)
        logger.info('Bound AF_UNIX socket on %s (backlog=%d mode=%s)',
                    path, backlog,
                    'unchanged' if mode is None else f'0o{mode:o}')
        return sock
    except OSError as msg:
        logger.error('Could not bind AF_UNIX socket on %s: %s', path, msg)
        sock.close()
        return None


# linux/sock_diag.h, linux/unix_diag.h, linux/netlink.h
_NETLINK_SOCK_DIAG = 4
_SOCK_DIAG_BY_FAMILY = 20
_NLM_F_REQUEST = 1
_UDIAG_SHOW_RQLEN = 0x10
_UNIX_DIAG_RQLEN = 4
_TCP_LISTEN = 10
_NLMSGHDR = struct.Struct('=IHHII')
_UNIX_DIAG_REQ = struct.Struct('=BBHIIIII')
_UNIX_DIAG_MSG = struct.Struct('=BBBBIII')
_RTATTR = struct.Struct('=HH')
_RQLEN = struct.Struct('=II')


def _sock_diag_exchange(request: bytes) -> bytes:
    with socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM,
                       _NETLINK_SOCK_DIAG) as nl:
        nl.send(request)
        # The kernel answers inside send(); nothing to wait for.
        return nl.recv(8192, socket.MSG_DONTWAIT)


def somaxconn() -> int | None:
    """``net.core.somaxconn`` in this process's network namespace, or ``None``."""
    try:
        with open('/proc/sys/net/core/somaxconn') as f:
            return int(f.read())
    except (OSError, ValueError):
        return None


def unix_accept_queue(sock) -> tuple[int, int] | None:
    """``(waiting, backlog)`` of a listening ``AF_UNIX`` socket, from the kernel.

    Read by inode, so an adopted or duplicated fd reports its creator's
    backlog.  The queue is full when ``waiting > backlog``.  ``None`` off
    Linux, for a socket that is not listening or is in another network
    namespace, or on any failure.
    """
    if not sys.platform.startswith('linux'):
        return None
    try:
        inode = os.fstat(sock.fileno()).st_ino
        req = _UNIX_DIAG_REQ.pack(socket.AF_UNIX, 0, 0, 1 << _TCP_LISTEN,
                                  inode, _UDIAG_SHOW_RQLEN,
                                  0xFFFFFFFF, 0xFFFFFFFF)
        reply = _sock_diag_exchange(
            _NLMSGHDR.pack(_NLMSGHDR.size + len(req), _SOCK_DIAG_BY_FAMILY,
                           _NLM_F_REQUEST, 1, 0) + req)
        length, kind, _flags, _seq, _pid = _NLMSGHDR.unpack_from(reply)
        if kind != _SOCK_DIAG_BY_FAMILY or length > len(reply):
            return None
        offset = _NLMSGHDR.size
        _family, _type, state, _pad, found, *_cookie = (
            _UNIX_DIAG_MSG.unpack_from(reply, offset))
        if found != inode or state != _TCP_LISTEN:
            return None
        offset += _UNIX_DIAG_MSG.size
        while offset + _RTATTR.size <= length:
            attr_len, attr_type = _RTATTR.unpack_from(reply, offset)
            if attr_len < _RTATTR.size:
                return None
            if (attr_type == _UNIX_DIAG_RQLEN
                    and attr_len >= _RTATTR.size + _RQLEN.size):
                return _RQLEN.unpack_from(reply, offset + _RTATTR.size)
            offset += (attr_len + 3) & ~3
    except Exception:
        logger.debug('sock_diag query failed', exc_info=True)
    return None


def create_dual_stack_sockets(port, backlog: int = _DEFAULT_BACKLOG,
                               reuseport: bool = False,
                               sndbuf: int = 0, rcvbuf: int = 0,
                               keepalive: bool = True,
                               user_timeout_ms: int = 0,
                               host: str | None = None):
    """
    Create one IPv4 socket (``0.0.0.0``) **and** one IPv6 socket (``::``),
    both listening on *port*.

    Naming a *host* asks for that interface instead, which is one socket in
    one family — the point of naming it is to reach nothing else.

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

    if host is not None:
        family = socket.AF_INET6 if ':' in host else socket.AF_INET
        sock = _bind_socket(family, host, port,
                            backlog=backlog, reuseport=reuseport,
                            sndbuf=sndbuf, rcvbuf=rcvbuf, keepalive=keepalive,
                            user_timeout_ms=user_timeout_ms)
        if sock is None:
            logger.error('Failed to bind %s port %s.', host, port)
            return []
        return [sock]

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


def create_configured_sockets(port, cfg, *, reuseport: bool,
                              host: str | None = None):
    """[`create_dual_stack_sockets`][] with every option *cfg* carries."""
    return create_dual_stack_sockets(
        port,
        backlog=cfg.socket_backlog,
        sndbuf=cfg.socket_sndbuf,
        rcvbuf=cfg.socket_rcvbuf,
        user_timeout_ms=cfg.tcp_user_timeout_ms,
        reuseport=reuseport,
        host=host,
    )

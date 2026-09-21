"""Multi-worker master process for BlackBull.

Uses a pre-fork model:
  1. The master binds sockets once (via ASGIServer.open_socket).
  2. N worker processes are forked; each inherits the socket file descriptors.
  3. The master monitors workers in a synchronous loop and respawns any that crash.
  4. On SIGTERM or SIGINT the master sends SIGTERM to all workers, waits up to
     *shutdown_timeout* seconds, then SIGKILLs any that are still alive.

When ``reload=True`` the master additionally runs a file watcher; on any
matching change it SIGTERMs the workers, marks the listening sockets
inheritable, and ``os.execvp``\\ s itself with the original argv — the
fresh process adopts the inherited fds and re-forks workers from the
new code.  See [`blackbull.server.reload`][blackbull.server.reload].

The worker entry point is [`blackbull.server.worker.run_worker`][blackbull.server.worker.run_worker].
Each worker runs its own asyncio event loop and its own ASGI lifespan cycle.

Usage::

    from blackbull.server.multiworker import MultiWorkerServer

    server = MultiWorkerServer(app, raw_sockets, ssl_context, workers=4)
    server.run()          # blocks until SIGTERM / SIGINT
"""
import logging
import multiprocessing
import os
import signal
import socket
import sys
import time
from dataclasses import dataclass

from .._cleanup import combine_cleanup_errors
from .listener import InheritedFd, Listener
from .recipient import _WS_READ_INLINE
from .worker import run_worker
from ..protocol.rsock import (close_sockets, create_configured_sockets,
                              REUSEPORT_SUPPORTED)

logger = logging.getLogger(__name__)

_MONITOR_INTERVAL = 1.0   # seconds between worker health checks
_SHUTDOWN_TIMEOUT = 10.0  # seconds to wait for graceful shutdown before SIGKILL
_RELOAD_TICK = 0.1        # finer poll when reload mode is active

_STACK_NAMES = {'v4': 'IPv4', 'v6': 'IPv6'}

#: Per-family kernel list of LISTENing TCP sockets (``st_nlink`` counts no
#: descriptors, so this list is what separates a handed-over socket from a
#: kept one).
_PROC_NET = {socket.AF_INET: '/proc/net/tcp',
             socket.AF_INET6: '/proc/net/tcp6'}

#: ``SO_NETNS_COOKIE`` (Linux 5.14+): the network namespace a socket is in.
#: ``None`` off Linux, where the number is not this option.
_SO_NETNS_COOKIE = 71 if sys.platform.startswith('linux') else None


@dataclass(frozen=True, slots=True)
class _PlannedListener:
    """A shared listener, recorded before the master releases its sockets.

    *addresses* is the host each of the re-bind's sockets must ask for,
    [`_rebind_address`][]'s answer — ``None`` for the framework's dual-stack
    pair.  *adopted* pairs each socket's family with its kernel inode; *flagged*
    marks a socket a supervisor already carries ``SO_REUSEPORT`` on;
    *foreign_netns* one bound outside this process's namespace.
    """
    listener: Listener
    port: int
    where: str
    reached: frozenset
    addresses: tuple
    adopted: tuple
    flagged: bool
    foreign_netns: bool


#: The families a per-worker re-bind can repeat: ``SO_REUSEPORT`` is an
#: IP-socket option, so a listener of any other family has no address to ask
#: for.
_IP_FAMILIES = (socket.AF_INET, socket.AF_INET6)

#: ``_stacks``'s four answers, shared so that a call allocates nothing.
_NO_IP = frozenset()
_V4 = frozenset(('v4',))
_V6 = frozenset(('v6',))
_V4_V6 = frozenset(('v4', 'v6'))


def _stacks(sock) -> frozenset:
    """The IP stacks one listening socket answers on; none when it is not IP."""
    family = sock.family
    if family not in _IP_FAMILIES:
        return _NO_IP
    if family == socket.AF_INET:
        return _V4
    try:
        return _V6 if sock.getsockopt(socket.IPPROTO_IPV6,
                                      socket.IPV6_V6ONLY) else _V4_V6
    except OSError:
        return _V6


def _reaches(socks) -> frozenset:
    """Which stacks a set of listening sockets answers on.

    A dual-stack IPv6 socket claims the IPv4 port too, and a host without IPv6
    legitimately comes back with one stack, so coverage — not a socket count —
    is what a re-bound set is held to.
    """
    return frozenset().union(*(_stacks(sock) for sock in socks))


def _rebind_address(sock):
    """The host the per-worker re-bind must ask for to repeat *sock*.

    Callers pass an ``AF_INET``/``AF_INET6`` socket — the plan drops anything
    else, because a socket with no IP address has nothing to repeat.  ``None``
    asks for the framework's own dual-stack pair.  The socket is the fact here
    — ``Listener.where`` has already dropped a named host — and the wildcard
    ``::`` is the one address a single bind cannot repeat: asking for ``::`` by
    name comes back with ``IPV6_V6ONLY`` set, which drops the IPv4 reach
    ``_reaches`` reads off the same option.
    """
    host = sock.getsockname()[0]
    if (sock.family == socket.AF_INET6 and host == '::'
            and 'v4' in _reaches([sock])):
        return None
    return host


def _describe(socks) -> str:
    """Where these sockets are, in the form an operator can act on."""
    names = []
    for sock in socks:
        name = sock.getsockname()
        if not isinstance(name, tuple):
            names.append(str(name))
            continue
        host, port = name[0], name[1]
        names.append(f'[{host}]:{port}' if ':' in host else f'{host}:{port}')
    return ', '.join(names)


def _refuse_non_ip_rebind(shared, workers: int) -> None:
    """Refuse the per-worker re-bind for a listener that is not an IP socket.

    ``SO_REUSEPORT`` is an IP-socket option: an ``AF_UNIX`` listener has no
    address to ask for, and its ``getsockname`` is the path, whose second
    character a plan read as the port before this refusal existed.
    """
    for _listener, socks in shared:
        non_ip = [sock for sock in socks if sock.family not in _IP_FAMILIES]
        if non_ip:
            raise RuntimeError(_refusal(
                workers, _describe(non_ip),
                f'SO_REUSEPORT is an IP-socket option, and '
                f'{non_ip[0].family.name} has no address to re-bind per worker'))


def _reuseport_is_set(sock) -> bool:
    """Whether this socket carries ``SO_REUSEPORT`` itself (a dup shares it)."""
    try:
        return bool(sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT))
    except OSError:
        return False


def _elsewhere_netns(sock) -> bool:
    """Whether *sock* was created in a network namespace this process is not in.

    A socket keeps the namespace it was created in.  A host that does not
    answer the question reads as the same namespace.
    """
    if _SO_NETNS_COOKIE is None:
        return False
    try:
        with socket.socket() as probe:
            return (sock.getsockopt(socket.SOL_SOCKET, _SO_NETNS_COOKIE, 8)
                    != probe.getsockopt(socket.SOL_SOCKET,
                                        _SO_NETNS_COOKIE, 8))
    except OSError:
        return False


def _kernel_listening(port: int, families) -> frozenset | None:
    """Inodes the kernel lists as LISTENing on *port*, or ``None`` if unaskable.

    An empty table is unaskable, not an answer: a masked procfs reports
    nothing at all, and reading that silence as "released" is how a kept socket
    goes unnoticed.
    """
    inodes = set()
    for family in families:
        path = _PROC_NET.get(family)
        if path is None:
            return None
        try:
            with open(path) as handle:
                text = handle.read()
        except OSError:
            return None
        if not text.strip():
            return None
        suffix = f':{port:04X}'
        for line in text.splitlines()[1:]:
            fields = line.split()
            if len(fields) > 9 and fields[3] == '0A' and fields[1].endswith(suffix):
                inodes.add(int(fields[9]))
    return frozenset(inodes)


def _held_elsewhere(plan: _PlannedListener) -> bool | None:
    """Whether the kernel still lists one of this listener's sockets as open.

    Only this process's copies were closed, so a socket of ours that the kernel
    still has on the port is held by somebody else.
    """
    families = {family for family, _inode in plan.adopted}
    listening = _kernel_listening(plan.port, families)
    if listening is None:
        return None
    return any(inode in listening for _family, inode in plan.adopted)


def _refusal(workers: int, where: str, reason: str) -> str:
    """The one refusal an unserved port gets, naming every way out of it."""
    return (f'BB_SOCKET_REUSEPORT=1 with {workers} worker(s) cannot give each '
            f'worker its own listener on {where}: {reason}. Set '
            f'BB_SOCKET_REUSEPORT=0 to share the adopted socket across the '
            f'workers, or run with BB_WORKERS=1.')


def _unserved_reason(plan: _PlannedListener, worker_listeners,
                     index: int) -> str | None:
    """Why this listener would come up unserved, or ``None`` when it is served."""
    if plan.foreign_netns:
        return ('the socket it adopted is in another network namespace, so the '
                'sockets its workers bind here cannot serve its port')
    held = _held_elsewhere(plan)
    if held:
        return ('the socket it adopted is still listening in another process, '
                'so the kernel would hand part of the port to a socket this '
                'server does not accept on')
    if held is None and plan.flagged:
        return ('the socket it adopted already carries SO_REUSEPORT, so the '
                'per-worker sets can join it while its creator still holds it, '
                'and this process cannot say whether the socket was released')
    for group in worker_listeners:
        covered = _reaches(group[index][1])
        if plan.reached <= covered:
            continue
        missing = ', '.join(_STACK_NAMES[stack]
                            for stack in sorted(plan.reached - covered))
        return (f'the per-worker re-bind came back without {missing}, so '
                f'another process may still hold the port (Linux shares a '
                f'port only with sockets that set SO_REUSEPORT themselves)')
    return None


def _refuse_when_unserved(planned, worker_listeners, workers: int) -> None:
    """Stop before the fork when a listener would come up without the port.

    The per-worker sets are no proof on their own: a socket that already
    carries ``SO_REUSEPORT`` (systemd's ``ReusePort=yes``) is joined by the
    re-bind rather than replaced by it, and its creator's copy keeps taking
    connections nothing here accepts on.  A short set is judged by coverage, a
    released one by the kernel's socket list.
    """
    for index, plan in enumerate(planned):
        reason = _unserved_reason(plan, worker_listeners, index)
        if reason is None:
            continue
        raise RuntimeError(_refusal(workers, plan.where, reason))


def _settle_stateful_bindings(app, workers: int) -> None:
    """Decide how a stateful raw protocol is reachable once workers multiply.

    Why a stateful binding stops claiming the shared listener is
    [`RawBinding.claims`][RawBinding.claims].  What is decided *here* is the case it cannot
    cover: a binding whose only route is the shared port has no owner to fall
    back to, so it is refused — before the fork, making the failure one
    message rather than one per worker.
    """
    registry = getattr(app, '_protocol_registry', None)
    if registry is None:
        return
    for binding in registry.raw_bindings.values():
        if not (binding.stateful and binding.detector is not None):
            continue
        if binding.port is None:
            raise RuntimeError(
                f'Protocol {binding.name!r} keeps state and is reachable only '
                f'on the shared port, which {workers} workers serve — a later '
                f'exchange would be answered by a worker that never saw the '
                f'earlier one.  Give it a dedicated port, run with workers=1, '
                f'or register it with stateful=False if it keeps nothing '
                f'between exchanges.')
        binding.disable_shared_dispatch()
        logger.info(
            'Protocol %r keeps state: reachable on port %d only, not the '
            'shared listener, because %d workers serve it.',
            binding.name, binding.port, workers)


class MultiWorkerServer:
    """Spawns and supervises N worker processes.

    Parameters
    ----------
    app:
        ASGI application callable.
    raw_sockets:
        Pre-bound sockets that every worker will inherit.
    ssl_context:
        TLS context, or None for plain HTTP.
    workers:
        Number of worker processes to maintain.
    max_connections:
        Per-worker connection limit forwarded to ``ASGIServer``.
    shutdown_timeout:
        Seconds to wait for graceful shutdown before sending SIGKILL.
    """

    def __init__(self, app, bound_listeners, ssl_context, *,
                 workers: int,
                 max_connections: int = 500,
                 stream_queue_depth: int = 64,
                 ws_queue_depth: int = _WS_READ_INLINE,
                 shutdown_timeout: float = _SHUTDOWN_TIMEOUT,
                 reload: bool = False,
                 reload_paths=None):
        bound_listeners = list(bound_listeners)
        self._worker_listeners = []
        self._pending_processes: list = []
        try:
            if workers < 1:
                raise ValueError(f'workers must be >= 1, got {workers}')
            self._app = app
            self._ssl_context = ssl_context
            self._num_workers = workers
            if workers > 1:
                _settle_stateful_bindings(app, workers)
            self._shared = [(l, socks) for l, socks in bound_listeners
                            if l.workers == 'all']
            self._single_owner = [(l, socks) for l, socks in bound_listeners
                                  if l.workers == 'one']
            self._max_connections = max_connections
            self._stream_queue_depth = stream_queue_depth
            self._ws_queue_depth = ws_queue_depth
            self._shutdown_timeout = shutdown_timeout
            self._reload = reload
            self._reload_paths = reload_paths
            self._reload_pending = False
            self._stopped = False
            self._watcher = None
            self._processes: list = []
            self._cleanup_deadline: float | None = None
            self._listening_sockets = [
                s for _l, socks in self._shared for s in socks]
            self._mp_ctx = multiprocessing.get_context('fork')

            from ..env import get_settings as _get_settings  # noqa: PLC0415
            cfg = _get_settings()
            if (workers > 1 and REUSEPORT_SUPPORTED
                    and cfg.socket_reuseport and not reload):
                _refuse_non_ip_rebind(self._shared, workers)
                planned = [_PlannedListener(
                    listener=listener,
                    port=socks[0].getsockname()[1],
                    where=_describe(socks),
                    reached=_reaches(socks),
                    addresses=tuple(_rebind_address(sock) for sock in socks
                                    if sock.family in _IP_FAMILIES),
                    adopted=tuple((sock.family, os.fstat(sock.fileno()).st_ino)
                                  for sock in socks),
                    flagged=isinstance(listener.where, InheritedFd)
                            and any(_reuseport_is_set(sock) for sock in socks),
                    foreign_netns=any(_elsewhere_netns(sock) for sock in socks),
                ) for listener, socks in self._shared]
                close_error = close_sockets(self._listening_sockets)
                self._listening_sockets = []
                if close_error is not None:
                    raise close_error
                for _worker in range(workers):
                    group = []
                    self._worker_listeners.append(group)
                    for plan in planned:
                        bound = []
                        group.append((plan.listener, bound))
                        for host in plan.addresses:
                            created = create_configured_sockets(
                                plan.port, cfg, reuseport=True, host=host)
                            if not created:
                                raise RuntimeError(
                                    f'Failed to re-bind worker listener on {plan.where}')
                            bound.extend(created)
                _refuse_when_unserved(
                    planned, self._worker_listeners, workers)
                logger.info(
                    'SO_REUSEPORT: created %d per-worker socket set(s) for %d listener(s)',
                    workers, len(planned))
            else:
                self._worker_listeners = [self._shared] * workers
        except BaseException:
            owned_sockets = [
                sock for _listener, socks in bound_listeners for sock in socks]
            owned_sockets.extend(
                sock for group in self._worker_listeners
                for _listener, sockets in group for sock in sockets)
            close_sockets(owned_sockets)
            raise

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Spawn workers and block until a shutdown signal is received.

        When ``reload`` is enabled the master also runs a file watcher and
        re-execs itself on any matching change — see
        [`blackbull.server.reload`][blackbull.server.reload].
        """
        primary = None
        try:
            self._install_signal_handlers()
            self._spawn_all()

            logger.info(
                'Master (PID %d) running %d worker(s)%s',
                os.getpid(), self._num_workers,
                ' [auto-reload]' if self._reload else '',
            )

            if self._reload:
                self._start_watcher()

            tick = _RELOAD_TICK if self._reload else _MONITOR_INTERVAL
            elapsed_since_reap = 0.0
            while not self._stopped:
                if self._reload and self._reload_pending:
                    logger.info('reload: change detected — recycling workers')
                    self._reload_pending = False
                    self._reload_now()  # does not return — execvp's
                # Only health-check on _MONITOR_INTERVAL cadence even when
                # the loop ticks fast for reload responsiveness.
                if elapsed_since_reap >= _MONITOR_INTERVAL:
                    self._reap_and_respawn()
                    elapsed_since_reap = 0.0
                time.sleep(tick)
                elapsed_since_reap += tick
        except BaseException as exc:
            primary = exc
            raise
        finally:
            cleanup_error = self._shutdown_all()
            socket_error = close_sockets(self._listening_socket_references())
            self._listening_sockets = []
            self._worker_listeners = []
            self._single_owner = []
            self._shared = []
            cleanup_error = combine_cleanup_errors(cleanup_error, socket_error)
            if cleanup_error is not None:
                if primary is None:
                    raise cleanup_error
                logger.error('Multi-worker cleanup failed: %s', cleanup_error)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _all_listening_sockets(self) -> list:
        """Every live listening descriptor the master owns, exactly once."""
        unique = []
        seen = set()
        for sock in self._listening_socket_references():
            try:
                fd = sock.fileno()
            except Exception:
                continue
            if not isinstance(fd, int) or fd < 0:
                continue
            key = ('fd', fd)
            if key not in seen:
                seen.add(key)
                unique.append(sock)
        return unique

    def _listening_socket_references(self):
        """Every socket wrapper retained by the supervisor, including aliases."""
        yield from self._listening_sockets
        for group in (*self._worker_listeners, self._single_owner, self._shared):
            for _listener, bound in group:
                yield from bound

    def _spawn_worker(self, worker_id: int):
        # A single-owner listener goes to worker 0 and no one else — worker 0
        # inherits the master's still-open fds via fork, so a respawn after a
        # crash re-adopts them and the broker resumes on the same port.
        listeners = list(self._worker_listeners[worker_id])
        if worker_id == 0:
            listeners += self._single_owner
        # fork copies the whole descriptor table, so hand the child the list to
        # let go of: a process without the descriptor cannot accept on it,
        # which makes single ownership structural rather than a consequence of
        # what nobody happens to call.
        mine = {id(sock) for _listener, socks in listeners for sock in socks}
        disowned = [sock for sock in self._all_listening_sockets()
                    if id(sock) not in mine]
        p = self._mp_ctx.Process(
            target=run_worker,
            args=(self._app, listeners, self._ssl_context,
                  worker_id, self._max_connections,
                  self._stream_queue_depth, self._ws_queue_depth,
                  disowned),
            daemon=False,  # workers must be reaped explicitly on shutdown
            name=f'bb-worker-{worker_id}',
        )
        self._pending_processes.append(p)
        try:
            p.start()
            logger.info('Spawned worker %d (PID %d)', worker_id, p.pid)
        except BaseException:
            unreclaimed, cleanup_error = self._reclaim_processes(
                [p], self._cleanup_deadline_value(),
                terminate=True)
            if not unreclaimed:
                self._remove_pending_process(p)
            if cleanup_error is not None:
                logger.error('Failed to reclaim worker after start error: %s',
                             cleanup_error)
            raise
        return p

    def _track_process(self, process) -> None:
        self._processes.append(process)
        self._remove_pending_process(process)

    def _remove_pending_process(self, process) -> None:
        self._pending_processes = [
            pending for pending in self._pending_processes
            if pending is not process]

    def _spawn_all(self) -> None:
        self._processes = []
        try:
            for worker_id in range(self._num_workers):
                self._track_process(self._spawn_worker(worker_id))
        except BaseException:
            cleanup_error = self._shutdown_all()
            if cleanup_error is not None:
                logger.error('Failed to roll back partially spawned workers: %s',
                             cleanup_error)
            raise

    def _reap_and_respawn(self) -> None:
        for i, p in enumerate(self._processes):
            if not p.is_alive():
                logger.warning(
                    'Worker %d (PID %d) exited with code %s — respawning',
                    i, p.pid, p.exitcode,
                )
                try:
                    replacement = self._spawn_worker(i)
                except BaseException:
                    _unreclaimed, cleanup_error = self._reclaim_processes(
                        [p], self._cleanup_deadline_value(),
                        terminate=False)
                    if cleanup_error is not None:
                        logger.error('Failed to close exited worker: %s',
                                     cleanup_error)
                    raise
                self._processes[i] = replacement
                self._remove_pending_process(replacement)
                unreclaimed, cleanup_error = self._reclaim_processes(
                    [p], time.monotonic() + self._shutdown_timeout,
                    terminate=False)
                self._pending_processes.extend(
                    process for process in unreclaimed
                    if all(process is not pending
                           for pending in self._pending_processes))
                if cleanup_error is not None:
                    raise cleanup_error

    @staticmethod
    def _process_is_alive(process):
        try:
            return process.is_alive(), None
        except (AssertionError, ValueError):
            return False, None
        except Exception as exc:
            return None, exc

    @staticmethod
    def _process_started(process):
        try:
            return process.pid is not None, None
        except ValueError:
            return False, None
        except Exception as exc:
            return None, exc

    def _owned_processes(self):
        owned = []
        seen = set()
        for process in (*self._processes, *self._pending_processes):
            if id(process) in seen:
                continue
            seen.add(id(process))
            owned.append(process)
        return owned

    def _cleanup_deadline_value(self) -> float:
        if self._cleanup_deadline is None:
            self._cleanup_deadline = time.monotonic() + self._shutdown_timeout
        return self._cleanup_deadline

    def _terminate_all(self) -> Exception | None:
        """Ask every running child to stop, continuing after any error."""
        errors = []
        processes = self._owned_processes()
        logger.info('Sending SIGTERM to %d worker(s)', len(processes))
        for p in processes:
            alive, state_error = self._process_is_alive(p)
            errors.append(state_error)
            if alive is False:
                continue
            try:
                p.terminate()
            except Exception as exc:
                errors.append(exc)
                logger.exception('Failed to terminate worker')
        return combine_cleanup_errors(*errors)

    def _reclaim_processes(self, processes, deadline: float, *, terminate: bool):
        """Reap and close process objects without exceeding *deadline*."""
        errors = []
        unreclaimed = []
        for p in processes:
            alive, state_error = self._process_is_alive(p)
            errors.append(state_error)
            if terminate and alive is not False:
                try:
                    p.terminate()
                except Exception as exc:
                    errors.append(exc)
                    logger.exception('Failed to terminate worker')
            started, state_error = self._process_started(p)
            errors.append(state_error)
            if started is not False:
                try:
                    p.join(timeout=max(0.0, deadline - time.monotonic()))
                except Exception as exc:
                    errors.append(exc)
                    logger.exception('Failed to join worker')
            alive, state_error = self._process_is_alive(p)
            errors.append(state_error)
            if alive is not False:
                try:
                    logger.warning('Worker did not stop — sending SIGKILL')
                    p.kill()
                    p.join(timeout=max(0.0, deadline - time.monotonic()))
                except Exception as exc:
                    errors.append(exc)
                    logger.exception('Failed to kill or join worker')
            alive, state_error = self._process_is_alive(p)
            errors.append(state_error)
            if alive is not False:
                unreclaimed.append(p)
                errors.append(TimeoutError(
                    'worker did not stop before the deadline'))
                continue
            try:
                p.close()
            except ValueError:
                # A closed Process has no resources left to reclaim.
                pass
            except Exception as exc:
                errors.append(exc)
                logger.exception('Failed to close worker process')
                unreclaimed.append(p)
        return unreclaimed, combine_cleanup_errors(*errors)

    def _shutdown_all(self) -> Exception | None:
        deadline = self._cleanup_deadline_value()
        termination_error = self._terminate_all()
        watcher_error = self._stop_watcher()
        processes = self._owned_processes()
        unreclaimed, reclaim_error = self._reclaim_processes(
            processes, deadline, terminate=False)
        self._processes = []
        self._pending_processes = unreclaimed
        if not unreclaimed:
            logger.info('All workers stopped')
        return combine_cleanup_errors(
            termination_error, watcher_error, reclaim_error)

    def _stop_watcher(self) -> Exception | None:
        if self._watcher is None:
            return None
        deadline = self._cleanup_deadline_value()
        try:
            self._watcher.stop(timeout=max(0.0, deadline - time.monotonic()))
        except Exception as exc:
            logger.exception('Failed to stop file watcher')
            return exc
        self._watcher = None
        return None

    def _install_signal_handlers(self) -> None:
        import threading  # noqa: PLC0415
        if threading.current_thread() is not threading.main_thread():
            logger.debug('Not in main thread — skipping signal handler installation')
            return

        def _handle_stop(signo, frame):  # noqa: ANN001
            logger.info('Master received signal %d — initiating shutdown', signo)
            self._stopped = True

        signal.signal(signal.SIGTERM, _handle_stop)
        signal.signal(signal.SIGINT, _handle_stop)

    # ------------------------------------------------------------------
    # Reload
    # ------------------------------------------------------------------

    def _start_watcher(self) -> None:
        from .reload import FileChangeWatcher  # noqa: PLC0415

        paths = self._reload_paths or [os.getcwd()]

        def _on_change() -> None:
            # Called on the watcher thread.  Setting a bool is atomic in
            # CPython; the main loop polls it on _RELOAD_TICK cadence.
            self._reload_pending = True

        self._watcher = FileChangeWatcher(paths, _on_change)
        self._watcher.start()

    def _reload_now(self) -> None:
        """SIGTERM workers, then re-exec the master.  Does not return."""
        from .reload import exec_self_with_sockets  # noqa: PLC0415

        cleanup_error = self._shutdown_all()
        if cleanup_error is not None:
            raise cleanup_error

        if not self._listening_sockets:
            # SO_REUSEPORT path closed the master sockets — reload was
            # supposed to be incompatible with that branch, so we should
            # never get here.  If we do, fall back to normal shutdown.
            logger.error('reload: master holds no listening sockets — cannot exec')
            self._stopped = True
            return

        # execvp replaces the process — control does not return.
        exec_self_with_sockets(self._listening_sockets)

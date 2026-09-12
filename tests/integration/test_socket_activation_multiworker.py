"""A supervisor that keeps the listening socket, with several reuseport workers.

The socket is held for real (no ``EADDRINUSE`` injection, no monkeypatched
bind) and the listeners are read from ``/proc``, never from the server's log.
"Answered or refused" is the contract, and a refusal must name the setting.
"""
from __future__ import annotations

import http.client
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.name != 'posix', reason='needs fd passing (fd://)'),
]

#: Repo root, for the child's ``import blackbull``: this checkout has no
#: installed package, so the child needs the same sys.path the tests run with.
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: A connect that succeeds and then says nothing is the defect, not a slow
#: server: the read timeout makes "hung" distinguishable from "refused".
_READ_TIMEOUT = 1.0

#: A1's window: inside it the port must answer on every family the
#: configuration asked for, or the process must have exited non-zero.
_UP_BUDGET = 10.0

#: The creator's backlog, deliberately not the server's default (1024) so a
#: listener that is still the adopted socket is recognisable by its inode.
_CREATOR_BACKLOG = 4096

_POLL = 0.05
_HARD_TIMEOUT = 120

_CHILD = """\
import logging, sys

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format='%(levelname)s %(name)s: %(message)s')

from blackbull import BlackBull
from blackbull.app import serve

app = BlackBull()


@app.route(path='/ping')
async def ping():
    return 'pong'


# What the console script does with a refused startup: name it and exit 1.
try:
    serve(app, {kwargs})
except RuntimeError as exc:
    print(f'blackbull: {{exc}}', file=sys.stderr)
    raise SystemExit(1)
"""


# ---------------------------------------------------------------------------
# Observing the kernel, not the server
# ---------------------------------------------------------------------------

def _listen_rows(port: int, *, required: bool = True) -> list[dict]:
    """Every LISTEN socket on *port*, straight out of ``/proc/net/tcp[6]``.

    *required* separates a test that cannot make its claim without procfs from
    a failure report that would rather print no rows than raise.
    """
    want = f'{port:04X}'
    rows = []
    for path, family in (('/proc/net/tcp', 4), ('/proc/net/tcp6', 6)):
        try:
            text = Path(path).read_text()
        except OSError:
            if required:
                pytest.skip(f'{path} is unavailable on this platform')
            continue
        for line in text.splitlines()[1:]:
            fields = line.split()
            local, state = fields[1], fields[3]
            if state == '0A' and local.endswith(f':{want}'):
                rows.append({
                    'family': family,
                    'address': local.rsplit(':', 1)[0],
                    'inode': int(fields[9]),
                })
    return rows


def _holder_pids(inode: int, pids: list[int]) -> set[int]:
    """Which of *pids* have an fd on *inode*."""
    needle = f'socket:[{inode}]'
    holders = set()
    for pid in pids:
        try:
            fds = os.listdir(f'/proc/{pid}/fd')
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(f'/proc/{pid}/fd/{fd}') == needle:
                    holders.add(pid)
                    break
            except OSError:
                continue
    return holders


def _worker_pids(master_pid: int) -> list[int] | None:
    """The master's children — the workers it forked."""
    try:
        text = Path(f'/proc/{master_pid}/task/{master_pid}/children').read_text()
    except OSError:
        return None
    return [int(tok) for tok in text.split()]


def _can_unshare_net() -> bool:
    """Whether an unprivileged process here can make a network namespace."""
    try:
        return subprocess.run(['unshare', '-rn', 'true'],
                              capture_output=True).returncode == 0
    except OSError:
        return False


# ---------------------------------------------------------------------------
# The shape being tested
# ---------------------------------------------------------------------------

def _dual_stack_listener(host_family=socket.AF_INET6, host='::', v6only=0,
                         reuseport=False):
    """Bind, listen, and return the creator's socket on a free port.

    *reuseport* is the supervisor's own ``SO_REUSEPORT`` (systemd's
    ``ReusePort=yes``), set before the bind.
    """
    creator = socket.socket(host_family, socket.SOCK_STREAM)
    creator.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if reuseport:
        creator.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    if host_family == socket.AF_INET6 and v6only is not None:
        creator.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, v6only)
    creator.bind((host, 0))
    creator.listen(_CREATOR_BACKLOG)
    return creator


class Server:
    """A real server child, plus the log it writes."""

    def __init__(self, tmp_path: Path, *, workers: int, reuseport: int,
                 inherited_fd: int | None = None, port: int | None = None,
                 unshare_net: bool = False):
        kwargs = [f'workers={workers}']
        if inherited_fd is not None:
            kwargs.append(f'inherited_fd={inherited_fd}')
        if port is not None:
            kwargs.append(f'port={port}')
        script = tmp_path / f'server-{time.monotonic_ns()}.py'
        script.write_text(_CHILD.format(kwargs=', '.join(kwargs)))
        self.log_path = tmp_path / f'server-{time.monotonic_ns()}.log'
        self._log = open(self.log_path, 'w', buffering=1)
        env = dict(os.environ)
        env['PYTHONPATH'] = os.pathsep.join(
            [str(_REPO_ROOT)] + [p for p in [env.get('PYTHONPATH')] if p])
        env['PYTHONUNBUFFERED'] = '1'
        env['BB_ACCESS_LOG'] = '0'
        env['BB_SOCKET_REUSEPORT'] = str(reuseport)
        # The worker count is an argument here; an inherited BB_WORKERS would
        # only be read when it is not.
        env.pop('BB_WORKERS', None)
        argv = [sys.executable, str(script)]
        if unshare_net:
            argv = ['unshare', '-rn', *argv]
        self.proc = subprocess.Popen(
            argv,
            cwd=str(_REPO_ROOT), env=env,
            stdout=self._log, stderr=subprocess.STDOUT,
            text=True, pass_fds=() if inherited_fd is None else (inherited_fd,),
            start_new_session=True, close_fds=True,
        )

    @property
    def log(self) -> str:
        try:
            return self.log_path.read_text(errors='replace')
        except OSError:
            return ''

    def stop(self) -> None:
        if self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except OSError:
                self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except OSError:
                self.proc.kill()
            self.proc.wait(timeout=5)
        self._log.close()


class Outcome:
    """What the port did inside the budget, and how."""

    def __init__(self, answers: dict[str, str], exitcode: int | None):
        self.answers = answers          # '127.0.0.1' / '::1' -> probe verdict
        self.exitcode = exitcode

    @property
    def served(self) -> bool:
        return all(v.startswith('answered') for v in self.answers.values())

    def __str__(self) -> str:
        shape = ', '.join(f'{host}: {verdict}' for host, verdict in self.answers.items())
        return f'{shape} (exitcode={self.exitcode})'


def _probe(host: str, port: int) -> str:
    """'answered: <status>', 'refused', or 'connect OK, NO RESPONSE'."""
    try:
        conn = http.client.HTTPConnection(host, port, timeout=_READ_TIMEOUT)
    except OSError as exc:
        return f'refused: {exc}'
    try:
        conn.request('GET', '/ping')
        response = conn.getresponse()
        response.read()
        return f'answered: {response.status}'
    except TimeoutError:
        return f'connect OK, NO RESPONSE within {_READ_TIMEOUT}s'
    except OSError as exc:
        return f'refused: {exc}'
    finally:
        conn.close()


def _settle(server: Server, port: int, budget: float = _UP_BUDGET,
            hosts: tuple = ('127.0.0.1', '::1')) -> Outcome:
    """Poll each host until it answers or the child exits."""
    answers = {host: 'not probed' for host in hosts}
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if server.proc.poll() is not None:
            break
        for host in answers:
            if not answers[host].startswith('answered'):
                answers[host] = _probe(host, port)
        if all(v.startswith('answered') for v in answers.values()):
            break
        time.sleep(_POLL)
    return Outcome(answers, server.proc.poll())


def _diagnostics(server: Server, port: int) -> str:
    rows = [f'  family={row["family"]} local={row["address"]}:{port:04X} '
            f'inode={row["inode"]}'
            for row in _listen_rows(port, required=False)]
    return ('listeners the kernel reports on the port:\n'
            + ('\n'.join(rows) or '  (none)')
            + f'\n--- child log ({len(server.log)} bytes) ---\n'
            + server.log[-2000:])


# ---------------------------------------------------------------------------
# The defect: the creator holds the socket
# ---------------------------------------------------------------------------

@pytest.mark.timeout(_HARD_TIMEOUT)
def test_a_creator_held_listener_is_served_or_refused(tmp_path: Path):
    """The creator keeps its plain socket open for the whole run."""
    creator = _dual_stack_listener()
    port = creator.getsockname()[1]
    adopted = os.dup(creator.fileno())
    server = Server(tmp_path, workers=2, reuseport=1, inherited_fd=adopted)
    os.close(adopted)
    try:
        outcome = _settle(server, port)
        log = server.log
        # The creator stays open for the whole run — that is the shape.
        assert creator.fileno() >= 0
        if outcome.exitcode is None:
            assert outcome.served, (
                f'the server is still running but the port is not served: '
                f'{outcome}\n{_diagnostics(server, port)}')
            assert 'BB_SOCKET_REUSEPORT' in log and str(port) in log, (
                'served, but the log never says the port was taken over '
                f'rather than given its own listener:\n{log[-2000:]}')
        else:
            assert outcome.exitcode != 0, (
                f'the server exited 0 without serving: {outcome}')
            assert 'BB_SOCKET_REUSEPORT' in log, (
                f'the refusal does not name the setting:\n{log[-2000:]}')
            assert str(port) in log, (
                f'the refusal does not name the listener:\n{log[-2000:]}')
            assert '2 worker' in log, (
                f'the refusal does not name the worker count:\n{log[-2000:]}')
    finally:
        server.stop()
        creator.close()


@pytest.mark.timeout(_HARD_TIMEOUT)
def test_a_creator_held_reuseport_listener_is_served_or_refused(tmp_path: Path):
    """The same, with the creator's ``SO_REUSEPORT`` set before the bind
    (systemd's ``ReusePort=yes``), so the port is shared rather than blocked."""
    creator = _dual_stack_listener(reuseport=True)
    port = creator.getsockname()[1]
    adopted = os.dup(creator.fileno())
    server = Server(tmp_path, workers=2, reuseport=1, inherited_fd=adopted)
    os.close(adopted)
    try:
        outcome = _settle(server, port)
        log = server.log
        assert creator.fileno() >= 0
        if outcome.exitcode is None:
            assert outcome.served, (
                f'the server is still running but part of the port is dead: '
                f'{outcome}\n{_diagnostics(server, port)}')
            assert 'BB_SOCKET_REUSEPORT' in log and str(port) in log, (
                'served, but the log never says the port was taken over '
                f'rather than given its own listener:\n{log[-2000:]}')
        else:
            assert outcome.exitcode != 0, (
                f'the server exited 0 without serving: {outcome}')
            assert 'BB_SOCKET_REUSEPORT' in log, (
                f'the refusal does not name the setting:\n{log[-2000:]}')
            assert str(port) in log, (
                f'the refusal does not name the listener:\n{log[-2000:]}')
            assert '2 worker' in log, (
                f'the refusal does not name the worker count:\n{log[-2000:]}')
    finally:
        server.stop()
        creator.close()


@pytest.mark.timeout(_HARD_TIMEOUT)
def test_a_creator_held_v4_listener_does_not_half_die(tmp_path: Path):
    """IPv4 held while IPv6 is free, so the re-bind succeeds per family."""
    creator = _dual_stack_listener(socket.AF_INET, '0.0.0.0', v6only=None)
    port = creator.getsockname()[1]
    adopted = os.dup(creator.fileno())
    server = Server(tmp_path, workers=2, reuseport=1, inherited_fd=adopted)
    os.close(adopted)
    try:
        outcome = _settle(server, port)
        log = server.log
        if outcome.exitcode is None:
            assert outcome.served, (
                f'the server is still running with a half-dead port: '
                f'{outcome}\n{_diagnostics(server, port)}')
            assert 'BB_SOCKET_REUSEPORT' in log and str(port) in log, (
                f'served, but the log does not name the setting and listener:\n'
                f'{log[-2000:]}')
        else:
            assert outcome.exitcode != 0, (
                f'the server exited 0 without serving: {outcome}')
            assert 'BB_SOCKET_REUSEPORT' in log and str(port) in log, (
                f'the refusal does not name the setting and listener:\n{log[-2000:]}')
    finally:
        server.stop()
        creator.close()


@pytest.mark.timeout(_HARD_TIMEOUT)
def test_a_socket_from_another_network_namespace_is_refused(tmp_path: Path):
    """The adopted socket keeps the network namespace it was bound in."""
    if not _can_unshare_net():
        pytest.skip('needs a user and network namespace (unshare -rn)')
    creator = _dual_stack_listener(reuseport=True)
    port = creator.getsockname()[1]
    adopted = os.dup(creator.fileno())
    server = Server(tmp_path, workers=2, reuseport=1, inherited_fd=adopted,
                    unshare_net=True)
    os.close(adopted)
    try:
        outcome = _settle(server, port)
        assert outcome.exitcode not in (None, 0), (
            f'the server reports success with its listener in another '
            f'namespace: {outcome}\n{_diagnostics(server, port)}')
        log = server.log
        assert 'BB_SOCKET_REUSEPORT' in log and 'namespace' in log, (
            f'the refusal does not name the setting and the reason:\n'
            f'{log[-2000:]}')
    finally:
        server.stop()
        creator.close()


# ---------------------------------------------------------------------------
# Controls: what must not move
# ---------------------------------------------------------------------------

@pytest.mark.timeout(_HARD_TIMEOUT)
def test_a_released_listener_keeps_its_per_worker_sockets(tmp_path: Path):
    """The creator drops its own copy before the server binds, as
    ``systemd-socket-activate`` does."""
    creator = _dual_stack_listener()
    port = creator.getsockname()[1]
    creator_inode = os.fstat(creator.fileno()).st_ino
    adopted = os.dup(creator.fileno())
    creator.close()
    server = Server(tmp_path, workers=2, reuseport=1, inherited_fd=adopted)
    os.close(adopted)
    try:
        outcome = _settle(server, port)
        assert outcome.served, (
            f'the released listener is not served: {outcome}\n'
            f'{_diagnostics(server, port)}')

        rows = _listen_rows(port)
        assert rows, 'the kernel reports no listener on the port'
        assert all(row['inode'] != creator_inode for row in rows), (
            'the adopted socket is still the listener — the workers never '
            f're-bound:\n{_diagnostics(server, port)}')

        workers = _worker_pids(server.proc.pid)
        assert workers is not None and len(workers) == 2, (
            f'expected two workers under pid {server.proc.pid}, got {workers}')
        for family in (4, 6):
            inodes = {row['inode'] for row in rows if row['family'] == family}
            assert len(inodes) == 2, (
                f'family {family}: expected one listening socket per worker, '
                f'found {len(inodes)}:\n{_diagnostics(server, port)}')
            holders = {inode: _holder_pids(inode, [server.proc.pid, *workers])
                       for inode in inodes}
            # One socket per worker, and each worker on its own: two sockets
            # can co-bind the same wildcard address and port only because
            # every one of them set SO_REUSEPORT.
            assert sorted(len(pids - {server.proc.pid}) for pids in holders.values()) == [1, 1], (
                f'family {family}: a per-worker socket is not held by exactly '
                f'one worker: {holders}')
            owned = {next(iter(pids - {server.proc.pid})) for pids in holders.values()}
            assert len(owned) == 2, (
                f'family {family}: both sockets are held by the same worker: '
                f'{holders}')
    finally:
        server.stop()


@pytest.mark.timeout(_HARD_TIMEOUT)
def test_a_released_named_host_listener_keeps_its_address(tmp_path: Path):
    """The fd carries the interface its creator chose; the re-bind must not
    widen it to every interface."""
    creator = _dual_stack_listener(socket.AF_INET, '127.0.0.1', v6only=None)
    port = creator.getsockname()[1]
    bound = {row['address'] for row in _listen_rows(port)}
    adopted = os.dup(creator.fileno())
    creator.close()
    server = Server(tmp_path, workers=2, reuseport=1, inherited_fd=adopted)
    os.close(adopted)
    try:
        outcome = _settle(server, port, hosts=('127.0.0.1',))
        assert outcome.served, (
            f'the released named-host listener is not served: {outcome}\n'
            f'{_diagnostics(server, port)}')

        rows = _listen_rows(port)
        assert {row['address'] for row in rows} == bound, (
            f'the workers listen somewhere else: '
            f'{sorted({row["address"] for row in rows})} != {sorted(bound)}\n'
            f'{_diagnostics(server, port)}')
        assert len(rows) == 2, (
            f'expected one listening socket per worker:\n'
            f'{_diagnostics(server, port)}')
    finally:
        server.stop()


@pytest.mark.timeout(_HARD_TIMEOUT)
def test_a_released_reuseport_listener_still_gets_per_worker_sockets(tmp_path: Path):
    """Released with ``SO_REUSEPORT`` set: membership alone must not read as
    "still held"."""
    creator = _dual_stack_listener(reuseport=True)
    port = creator.getsockname()[1]
    creator_inode = os.fstat(creator.fileno()).st_ino
    adopted = os.dup(creator.fileno())
    creator.close()
    server = Server(tmp_path, workers=2, reuseport=1, inherited_fd=adopted)
    os.close(adopted)
    try:
        outcome = _settle(server, port)
        assert outcome.served, (
            f'a released reuseport listener was not served: {outcome}\n'
            f'{_diagnostics(server, port)}')

        rows = _listen_rows(port)
        assert rows, 'the kernel reports no listener on the port'
        assert all(row['inode'] != creator_inode for row in rows), (
            'the adopted socket is still the listener — the workers never '
            f're-bound:\n{_diagnostics(server, port)}')
        for family in (4, 6):
            inodes = {row['inode'] for row in rows if row['family'] == family}
            assert len(inodes) == 2, (
                f'family {family}: expected one listening socket per worker, '
                f'found {len(inodes)}:\n{_diagnostics(server, port)}')
    finally:
        server.stop()


@pytest.mark.timeout(_HARD_TIMEOUT)
def test_reuseport_still_makes_per_worker_sockets_on_a_free_port(tmp_path: Path):
    """No adoption at all: the per-worker listeners are the feature."""
    server = Server(tmp_path, workers=2, reuseport=1, port=0)
    try:
        port = None
        deadline = time.monotonic() + _UP_BUDGET
        while port is None and time.monotonic() < deadline:
            if server.proc.poll() is not None:
                break
            match = re.search(r'on port (\d+)', server.log)
            if match:
                port = int(match.group(1))
            else:
                time.sleep(_POLL)
        assert port is not None, (
            f'the server never reported its port:\n{server.log[-2000:]}')
        outcome = _settle(server, port)
        assert outcome.served, (
            f'the free-port server is not served: {outcome}\n'
            f'{_diagnostics(server, port)}')

        rows = _listen_rows(port)
        workers = _worker_pids(server.proc.pid)
        assert workers is not None and len(workers) == 2, (
            f'expected two workers under pid {server.proc.pid}, got {workers}')
        for family in (4, 6):
            inodes = {row['inode'] for row in rows if row['family'] == family}
            assert len(inodes) == 2, (
                f'family {family}: expected one listening socket per worker, '
                f'found {len(inodes)}:\n{_diagnostics(server, port)}')
    finally:
        server.stop()


@pytest.mark.timeout(_HARD_TIMEOUT)
def test_a_creator_held_listener_still_serves_without_reuseport(tmp_path: Path):
    """``BB_SOCKET_REUSEPORT=0`` shares the held socket instead of re-binding
    it — the way out the refusal names."""
    creator = _dual_stack_listener()
    port = creator.getsockname()[1]
    adopted = os.dup(creator.fileno())
    server = Server(tmp_path, workers=2, reuseport=0, inherited_fd=adopted)
    os.close(adopted)
    try:
        outcome = _settle(server, port)
        assert outcome.served, (
            f'the shared adopted socket is not served: {outcome}\n'
            f'{_diagnostics(server, port)}')
    finally:
        server.stop()
        creator.close()


@pytest.mark.timeout(_HARD_TIMEOUT)
def test_one_worker_with_a_creator_held_listener_still_serves(tmp_path: Path):
    """``workers=1`` adopts the fd as-is, with no re-bind to attempt."""
    creator = _dual_stack_listener()
    port = creator.getsockname()[1]
    adopted = os.dup(creator.fileno())
    server = Server(tmp_path, workers=1, reuseport=1, inherited_fd=adopted)
    os.close(adopted)
    try:
        outcome = _settle(server, port)
        assert outcome.served, (
            f'the single worker does not serve the adopted socket: {outcome}\n'
            f'{_diagnostics(server, port)}')
    finally:
        server.stop()
        creator.close()

"""Control a replacement supervisor and its initial worker group."""
import asyncio
import json
import os
import select
import signal
import socket
import subprocess
import sys
import time

from ..protocol.rsock import _INHERIT_FDS_ENV
from .listener import HTTP

CONTROL_ENV = '_BB_RELOAD_CONTROL_FD'
LAYOUT_ENV = '_BB_RELOAD_LISTENER_LAYOUT'
EXCLUSIVE_HTTP_ENV = '_BB_RELOAD_EXCLUSIVE_HTTP'
_READY = b'R'
_ARMED = b'A'
_START = b'S'
_START_EXCLUSIVE = b'E'
_STOPPED = b'X'
_INTERVAL = 0.05
STARTUP_TIMEOUT = 30.0


def listener_layout(listener):
    return [repr(listener.where), listener.speaks, listener.workers,
            listener.tls is not None]


class ReloadGeneration:
    def __init__(self, listeners, *, exclusive_http):
        argv = sys.orig_argv[1:]
        if not argv:
            raise RuntimeError('cannot reload without an original Python invocation')
        fds = tuple(sock.fileno() for _, sockets in listeners for sock in sockets)
        if not fds or any(fd < 0 for fd in fds):
            raise RuntimeError('cannot reload with missing or closed listener descriptors')
        owner, child = socket.socketpair()
        try:
            layout = [[len(sockets), listener_layout(listener)]
                      for listener, sockets in listeners]
            env = dict(os.environ, **{
                _INHERIT_FDS_ENV: ','.join(map(str, fds)),
                CONTROL_ENV: str(child.fileno()),
                LAYOUT_ENV: json.dumps(layout),
                EXCLUSIVE_HTTP_ENV: '1' if exclusive_http else '0',
            })
            self.process = subprocess.Popen(
                [sys.executable, *argv], env=env,
                pass_fds=(*fds, child.fileno()), start_new_session=True)
        except BaseException:
            owner.close()
            raise
        finally:
            child.close()
        self.control = owner
        self.control.settimeout(_INTERVAL)
        self.pending_exclusive = exclusive_http or any(
            listener.workers == 'one' for listener, _ in listeners)

    def activate(self, stopped):
        deadline = time.monotonic() + STARTUP_TIMEOUT
        self._expect(_READY, deadline, stopped)
        self.control.sendall(_START)
        self._expect(_ARMED, deadline, stopped)

    def activate_exclusive(self, stopped):
        if self.pending_exclusive:
            self.control.sendall(_START_EXCLUSIVE)
            self._expect(_ARMED, time.monotonic() + STARTUP_TIMEOUT, stopped)
            self.pending_exclusive = False

    def _expect(self, expected, deadline, stopped):
        while not stopped() and time.monotonic() < deadline:
            try:
                message = self.control.recv(1)
            except TimeoutError:
                # A child it forked may still hold the control socket open.
                if self.process.poll() is not None:
                    raise RuntimeError('replacement exited before completing reload') from None
                continue
            if message != expected:
                raise RuntimeError('replacement exited before completing reload')
            return
        raise TimeoutError('replacement generation did not become ready before the deadline')

    def exited(self):
        return (self.process.returncode is not None
                or self.control.fileno() < 0
                or channel_readable(self.control))

    def stop(self, timeout):
        if self.control.fileno() < 0:
            return      # already stopped: the group id may since have been reused
        deadline = time.monotonic() + timeout
        clean = False
        try:
            if self.process.poll() is None:
                try:
                    os.kill(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    # EOF below still determines whether children were reclaimed.
                    pass
            # A leader that already exited leaves its workers in the group.
            while (self.process.returncode is None and self.control.fileno() >= 0
                   and time.monotonic() < deadline):
                try:
                    message = self.control.recv(1)
                except TimeoutError:
                    continue
                if message in (b'', _STOPPED):
                    clean = message == _STOPPED
                    break
        finally:
            self.control.close()
            if not clean:
                # The workers outlive a leader that exited: signal its whole group.
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    # The process group has already disappeared.
                    pass
            try:
                self.process.wait(timeout=1.0)
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError('reload generation could not be reclaimed') from exc


def channel_readable(channel):
    poller = select.poll()
    poller.register(channel, select.POLLIN)
    return bool(poller.poll(0))


def close_generation(channel, clean):
    try:
        if clean:
            channel.sendall(_STOPPED)
    except OSError:
        # The owner can disappear during shutdown.
        pass
    finally:
        channel.close()


class WorkerReadiness:
    def __init__(self, context, workers, *, deferred, exclusive_http):
        # Every byte has one writer. A killed worker must not strand a lock.
        self.states = context.RawArray('b', workers)
        self.phase = context.RawValue('b', 0)
        self.has_exclusive = deferred
        self.exclusive_http = exclusive_http

    def defers(self, listener):
        return listener.workers == 'one' or self.exclusive_http and listener.speaks == HTTP

    async def wait(self, worker_id, stopping):
        self.states[worker_id] = 1
        await self._wait_start(1, stopping)

    async def _wait_start(self, phase, stopping):
        while self.phase.value < phase and not stopping.is_set():
            await asyncio.sleep(_INTERVAL)

    async def wait_exclusive(self, stopping):
        await self._wait_start(2, stopping)

    def armed(self, worker_id, *, exclusive=False):
        self.states[worker_id] = 3 if exclusive else 2

    def publish(self, channel, processes, stopped):
        deadline = time.monotonic() + STARTUP_TIMEOUT
        channel.settimeout(_INTERVAL)
        self._wait_for(1, deadline, processes, stopped)
        channel.sendall(_READY)
        self._await_start(channel, _START, deadline, processes, stopped)
        self.phase.value = 1
        self._wait_for(2, deadline, processes, stopped)
        channel.sendall(_ARMED)
        if self.has_exclusive:
            deadline = time.monotonic() + STARTUP_TIMEOUT
            self._await_start(channel, _START_EXCLUSIVE, deadline, processes, stopped)
            self.phase.value = 2
            self._wait_for(3, deadline, processes, stopped)
            channel.sendall(_ARMED)

    def _await_start(self, channel, expected, deadline, processes, stopped):
        while True:
            self._check(deadline, processes, stopped)
            try:
                message = channel.recv(1)
            except TimeoutError:
                continue
            if message != expected:
                raise RuntimeError('reload activation was abandoned')
            return

    @staticmethod
    def _check(deadline, processes, stopped):
        if stopped() or any(not p.is_alive() for p in processes):
            raise RuntimeError('replacement worker exited during startup')
        if time.monotonic() >= deadline:
            raise TimeoutError('replacement workers did not start before the deadline')

    def _wait_for(self, state, deadline, processes, stopped):
        while True:
            self._check(deadline, processes, stopped)
            if all(value >= state for value in self.states[:]):
                return
            time.sleep(_INTERVAL)

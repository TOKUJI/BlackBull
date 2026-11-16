"""Third-party HTTP/2 servers in containers: nginx and nghttpd.

Driven through the ``docker`` CLI, not the Python SDK: the SDK talks to the
daemon socket directly, which some setups cannot reach even when the CLI works
(Docker Desktop's Windows named pipe from inside WSL).

Each image is built from a context directory and tagged by that context's
digest, so an edited config never runs a stale image.  Only a preparation test
(in ``test_reference_peers.py``) builds; a peer waits for that build and skips
if it never lands, so a cell run on its own pays for no build and races no
other worker.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest

HERE = Path(__file__).parent

#: Building is not unit-test speed: on the box that filed #309 even a cached
#: nginx build took 112 s, against a suite default of 30 s.
BUILD_BUDGET = 900

#: A peer test may wait for another worker's build, so it gets the build's budget.
PEER_BUDGET = BUILD_BUDGET + 60

#: A peer outlives no run: it stops itself even when its worker is killed.
PEER_LIFETIME = 1800

#: A healthy peer answers within about a second, even under ``-n auto``.
ANSWER_TIMEOUT = 10.0
ATTEMPTS = 3

#: xdist gives every worker the same uid; a run of its own needs one that
#: cannot repeat, so the pid carries a random token.
RUN_ID = (os.environ.get('PYTEST_XDIST_TESTRUNUID')
          or f'{os.getpid()}-{uuid.uuid4().hex[:8]}')

_OWNER = os.environ.get('USER') or os.environ.get('USERNAME') or 'user'

#: IANA's dynamic range, where Windows allocates and reserves its own ports.
DYNAMIC_PORTS_START = 49152

_PREFACE = b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n' + b'\x00\x00\x00\x04\x00\x00\x00\x00\x00'


def unavailable(reason: str):
    """Skip without a peer, or fail where the run exists to compare against one.

    ``BB_REQUIRE_REFERENCE_PEERS=1`` (the differential's CI job) turns every
    skip here into a failure, so a run that compared nothing is not green.
    """
    if os.environ.get('BB_REQUIRE_REFERENCE_PEERS') == '1':
        pytest.fail(reason)
    pytest.skip(reason)


class PeerStartError(Exception):
    """``docker run`` refused to start the peer on the requested port."""


def context_digest(ctx: Path) -> str:
    """A tag that changes when, and only when, the build context changes.

    Length-prefixed so a rename cannot forge another context's stream; mode
    is in because ``COPY`` carries it.
    """
    digest = hashlib.sha256()
    for path in sorted(ctx.rglob('*')):
        if path.is_file():
            fields = (path.relative_to(ctx).as_posix().encode(),
                      f'{path.stat().st_mode & 0o777:o}'.encode(),
                      path.read_bytes())
            digest.update(b''.join(f'{len(f)}:'.encode() + f for f in fields))
    return digest.hexdigest()[:12]


def require_docker() -> None:
    """Without a CLI or a daemon there is no third-party server to reach."""
    if shutil.which('docker') is None:
        unavailable('docker CLI not installed')
    if subprocess.run(['docker', 'info'], capture_output=True,
                      timeout=60).returncode != 0:
        unavailable('docker daemon unreachable')


def bind_probe(port: int) -> socket.socket:
    """Bind *port* on both families, so a holder of either one conflicts.

    No SO_REUSEADDR: it would loosen the very conflict check this relies on.
    """
    dual = socket.has_dualstack_ipv6()
    sock = socket.socket(socket.AF_INET6 if dual else socket.AF_INET)
    try:
        if dual:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.bind(('::' if dual else '0.0.0.0', port))
    except BaseException:
        sock.close()
        raise
    return sock


def free_wsl_port() -> int:
    """A registered-range port nothing in WSL holds now.

    Docker Desktop relays a published port into WSL only if WSL does not hold
    it when the container starts, and WSL's ephemeral range is 1024-65535, so
    the port is chosen here rather than left to ``-P``.
    """
    for _ in range(64):
        with bind_probe(0) as sock:
            port = sock.getsockname()[1]
        if port < DYNAMIC_PORTS_START:
            return port
    unavailable(f'no free port below {DYNAMIC_PORTS_START}: check '
                f'net.ipv4.ip_local_port_range')


def wait_for_h2c(host: str, port: int, timeout: float = 30.0) -> None:
    """Wait until the server speaks HTTP/2, not merely until something accepts.

    ``docker run -p`` publishes the port as soon as the container exists, so
    docker-proxy completes the TCP handshake while the server inside is still
    starting, and the first real connection is reset before the server
    speaks.  A server sends its SETTINGS the moment an h2c connection opens,
    so a SETTINGS frame is the proof.
    """
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0) as sock:
                sock.sendall(_PREFACE)
                header = sock.recv(9)
                if len(header) == 9 and header[3] == 0x4:
                    return
                last = RuntimeError(f'accepted, but answered {header!r}')
        except OSError as exc:
            last = exc
        time.sleep(0.2)
    raise RuntimeError(f'no HTTP/2 answer on {host}:{port} ({last!r})')


class ReferencePeer:
    """One third-party server image: its build, and its containers.

    *argv* runs the server in the foreground on port 80; ``{lifetime}``
    in it is replaced by the container's lifetime in seconds.
    """

    def __init__(self, name: str, argv: tuple[str, ...], preparer: str) -> None:
        self.name = name
        self.context = HERE / name
        self.image = f'bb-fault-{name}:{context_digest(self.context)}'
        self.argv = argv
        #: The test that builds this image, named for the skip messages.
        self.preparer = preparer
        #: This run's record of a failed build: per user because the temp dir
        #: is shared, per run because a record only means something to its run.
        self.build_failed = (Path(tempfile.gettempdir())
                             / f'{self.image.replace(":", "-")}-{_OWNER}-{RUN_ID}.failed')

    def present(self) -> bool:
        return subprocess.run(['docker', 'image', 'inspect', self.image],
                              capture_output=True, timeout=60).returncode == 0

    def recorded_failure(self) -> str | None:
        try:
            return self.build_failed.read_text()
        except FileNotFoundError:
            return None

    def prepare(self) -> None:
        """Build this context's image unless the daemon already has it.

        A build that fails or overruns fails the caller: the peer cannot be
        produced, and a skip would hide that behind a green run.  The reason
        is recorded first, so the peers skip with it.
        """
        if self.present():
            return
        try:
            build = subprocess.run(
                ['docker', 'build', '-q', '-t', self.image, str(self.context)],
                capture_output=True, timeout=BUILD_BUDGET - 60)
        except subprocess.TimeoutExpired as exc:
            # str(TimeoutExpired) does not carry the captured output.
            detail = (exc.stderr or b'').decode(errors='replace').strip()[:200]
            reason = f'the build did not finish within {BUILD_BUDGET - 60} s'
            if detail:
                reason = f'{reason}: {detail}'
            self.build_failed.write_text(reason)
            pytest.fail(reason)
        if build.returncode != 0:
            reason = build.stderr.decode(errors='replace')[:200].strip()
            self.build_failed.write_text(reason)
            pytest.fail(f'could not build the reference image: {reason}')

    def require(self, selected: set[str]) -> None:
        """Wait for the preparation test's build, then skip without it.

        A failure it recorded, or a run that did not select it, ends the wait
        at once.
        """
        if self.present():
            return
        if self.preparer not in selected:
            unavailable(f'{self.image} is absent and this run did not select '
                        f'{self.preparer}, which builds it')
        deadline = time.monotonic() + BUILD_BUDGET
        while not self.present():
            recorded = self.recorded_failure()
            if recorded is not None:
                unavailable(f'could not build the reference image: {recorded}; '
                            f'see {self.preparer}')
            if time.monotonic() >= deadline:
                unavailable(f'{self.image} was not built within '
                            f'{BUILD_BUDGET} s; see {self.preparer}')
            time.sleep(2)

    def start(self, port: int, lifetime: int = PEER_LIFETIME) -> str:
        """Start a container on *port* and return its id."""
        argv = [a.replace('{lifetime}', str(lifetime)) for a in self.argv]
        run = subprocess.run(
            # --init: busybox timeout execs the server as PID 1, which no signal stops.
            ['docker', 'run', '-d', '--rm', '--init', '-p', f'{port}:80',
             '--label', f'bb-fault-run={RUN_ID}', self.image, *argv],
            capture_output=True, timeout=120)
        if run.returncode != 0:
            raise PeerStartError(run.stderr.decode(errors='replace')[:200])
        return run.stdout.decode().strip()

    def start_answering(self, lifetime: int = PEER_LIFETIME) -> tuple[str, int]:
        """Start containers on fresh free ports until one answers.

        A port can still be taken in WSL between the check and the relay, or
        be refused by Windows; either way the next attempt uses another port.
        """
        for attempt in range(ATTEMPTS):
            last = attempt == ATTEMPTS - 1
            port = free_wsl_port()
            try:
                container = self.start(port, lifetime)
            except PeerStartError as exc:
                if last:
                    unavailable(f'could not start the reference server: {exc}')
                continue
            try:
                wait_for_h2c('127.0.0.1', port, timeout=ANSWER_TIMEOUT)
                return container, port
            except BaseException as exc:
                subprocess.run(['docker', 'rm', '-f', container],
                               capture_output=True, timeout=120)
                if not isinstance(exc, RuntimeError) or last:
                    raise

    def serve(self, request):
        """A fixture body: one answering container for the requesting module."""
        require_docker()
        self.require({item.name for item in request.session.items})
        container, port = self.start_answering()
        try:
            yield '127.0.0.1', port
        finally:
            subprocess.run(['docker', 'rm', '-f', container],
                           capture_output=True, timeout=120)


#: One listener for HTTP/1.1 and h2c (prior knowledge), answering 200 "ok".
NGINX = ReferencePeer('nginx_h2c', ('timeout', '{lifetime}', 'nginx', '-g', 'daemon off;'),
                      preparer='test_the_nginx_image_is_prepared')
#: nghttp2's server, h2c only, answering 200 "ok" for /.
NGHTTPD = ReferencePeer('nghttpd', ('timeout', '{lifetime}', 'nghttpd', '--no-tls', '-d', '/srv', '80'),
                        preparer='test_the_nghttpd_image_is_prepared')
PEERS = (NGINX, NGHTTPD)

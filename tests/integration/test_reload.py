"""Exercise watcher, generation handoff and connection drain over real sockets."""
from __future__ import annotations

import os
import signal
import socket
import ssl
import subprocess
import sys
import textwrap
import threading
import time
import warnings
import http.client
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import pytest
from hpack import Encoder
from websockets.exceptions import ConnectionClosedOK
from websockets.sync.client import connect as connect_websocket

from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import FrameTypes, HeaderFrameFlags
from blackbull.mqtt.messages import MQTTConnack, MQTTConnect, decode_packet, encode_packet


# The handoff spawns real subprocesses.  The
# directory says integration; the marker makes the default suite and the
# pre-commit hook agree with it.  CI still covers it: the integration lane
# in .github/workflows/test.yml runs `pytest tests/integration
# tests/conformance --run-integration`.
pytestmark = pytest.mark.integration


#: Per-stage budgets.  Sized from the healthy path (~12 s end to end) with
#: headroom for CPU/IO contention.  A budget is a ceiling, not an
#: expectation — the point is that exceeding one names *which* stage
#: stalled, not that the number is tuned.
_BUDGET: dict[str, float] = {
    'server up': 30.0,       # cold interpreter + import + bind
    'watcher armed': 5.0,    # watchfiles thread start
    'change seen': 10.0,     # poll interval is 100 ms; this is generous
    'recycle begun': 5.0,    # master polls the flag every _RELOAD_TICK
    'new code live': 15.0,   # staged startup plus handoff
}

#: Hard timeout.  Must exceed the sum of the budgets (plus one 'change
#: seen' retry and teardown), or the blunt timeout fires first and takes
#: the staged diagnosis with it.
_HARD_TIMEOUT_SEC = 120

_REQ_TIMEOUT_SEC = 2.0
_POLL_SEC = 0.1
_H2_PING = b'\x00\x00\x08\x06\x00\x00\x00\x00\x0012345678'
_CERT_DIR = Path(__file__).resolve().parents[1]

#: Log signals each stage waits for.  Substrings, deliberately short: the
#: test should break when a line is removed, not when it is re-worded.
_WATCHER_ARMED = 'auto-reload: watching'
_CHANGE_SEEN = 'auto-reload: change detected'
_RECYCLE_BEGUN = 'recycling workers'


class DroppedReloadEvent(UserWarning):
    """The first write was not observed; a second one was.

    Not a failure — the reload machinery works — but it means event
    delivery dropped an event, which is the half of the flake that the
    machinery cannot fix.  Raised as a warning so it stays visible in CI
    output without blocking a commit.
    """


def _free_port() -> int:
    """Reserve an ephemeral port via a transient bind and immediately release it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _get_version(port: int, path='/version', *, tls=False) -> str | None:
    """Plain HTTP GET, returns body or None on connect failure."""
    try:
        if tls:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            conn = http.client.HTTPSConnection(
                '127.0.0.1', port, timeout=_REQ_TIMEOUT_SEC,
                context=context)
        else:
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=_REQ_TIMEOUT_SEC)
        conn.request('GET', path)
        resp = conn.getresponse()
        body = resp.read().decode('ascii', errors='replace')
        conn.close()
        return body
    except (OSError, http.client.HTTPException):
        return None


def _open_h2_request(port: int):
    sock = socket.create_connection(('127.0.0.1', port), timeout=5)
    factory = FrameFactory()
    head = factory.create(
        FrameTypes.HEADERS, HeaderFrameFlags.END_HEADERS | HeaderFrameFlags.END_STREAM,
        1, data=Encoder().encode([
            (':method', 'GET'), (':scheme', 'http'),
            (':authority', 'localhost'), (':path', '/held'),
        ]),
    )
    sock.sendall(b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n' + factory.settings().save()
                 + factory.settings(ack=True).save() + head.save())
    return sock


def _read_h2_frame(reader):
    header = reader.read(9)
    if not header:
        return None
    assert len(header) == 9
    return header[3], int.from_bytes(header[5:], 'big'), reader.read(int.from_bytes(header[:3], 'big'))


def _open_mqtt(port: int):
    sock = socket.create_connection(('127.0.0.1', port), timeout=4)
    try:
        sock.sendall(encode_packet(MQTTConnect(client_id='reload', clean_start=True, keep_alive=60)))
        with sock.makefile('rb') as reader:
            head = reader.read(2)
            reply = decode_packet(head + reader.read(head[1]))
        assert isinstance(reply, MQTTConnack) and reply.reason_code == 0
    except BaseException:
        sock.close()
        raise
    return sock


class ListenerProbe:
    """Background TCP-connect sampler for the listening socket.

    The reload path passes the same listening socket to the replacement, so a
    connect must succeed at every point in the transition.  Sampling on
    its own thread keeps that invariant independent of how long the staged
    waits take — otherwise the outage window is only observed while the
    main thread happens to be polling HTTP.

    Connect-only on purpose: an HTTP round-trip also fails when workers
    are mid-recycle, which is expected and would conflate "no listener"
    with "no worker yet".
    """

    def __init__(self, port: int, interval: float = _POLL_SEC):
        self._port = port
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.samples = 0
        self.failures = 0

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with socket.create_connection(('127.0.0.1', self._port),
                                              timeout=_REQ_TIMEOUT_SEC):
                    self.samples += 1
            except OSError:
                self.failures += 1
            self._stop.wait(self._interval)

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name='reload-listener-probe', daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None


def _log_text(log_path: Path) -> str:
    try:
        return log_path.read_text(errors='replace')
    except OSError:
        return ''


def _occurrences(log_path: Path, needle: str) -> int:
    return _log_text(log_path).count(needle)


def _await(stage: str, probe: Callable[[], bool]) -> bool:
    """Poll *probe* until true within the stage's budget."""
    deadline = time.monotonic() + _BUDGET[stage]
    while time.monotonic() < deadline:
        if probe():
            return True
        time.sleep(_POLL_SEC)
    return False


def _fail_stage(stage: str, waiting_for: str) -> NoReturn:
    pytest.fail(
        f'reload stalled at stage {stage!r} after {_BUDGET[stage]}s '
        f'waiting for {waiting_for}. '
        'Stage timeline and subprocess log follow under "Captured stdout call".'
    )


class StageTimeline:
    """Runs the staged waits and records how long each one took.

    A stall is much easier to read against the stages that already
    succeeded — "watcher armed in 0.01 s, change seen never" is a
    diagnosis; "did not observe v2" is not.
    """

    def __init__(self) -> None:
        self._rows: list[tuple[str, float, bool]] = []

    def run(self, stage: str, probe: Callable[[], bool]) -> bool:
        start = time.monotonic()
        ok = _await(stage, probe)
        self._rows.append((stage, time.monotonic() - start, ok))
        return ok

    def record(self, label: str, elapsed: float) -> None:
        """Note a phase that is timed rather than waited on (spawn, teardown)."""
        self._rows.append((label, elapsed, True))

    def require(self, stage: str, probe: Callable[[], bool],
                waiting_for: str) -> None:
        if not self.run(stage, probe):
            _fail_stage(stage, waiting_for)

    def render(self) -> str:
        return '\n'.join(
            f'  {"ok   " if ok else "STALL"} {stage:<14} {elapsed:6.2f}s'
            + (f' / {_BUDGET[stage]:.0f}s' if stage in _BUDGET else '')
            for stage, elapsed, ok in self._rows
        ) or '  (no stage ran)'


def _write_app(script: Path, port: int, version: str, *, startup_fails=False,
               startup_delay=0, extra_port=None, host=None, raw_port=None, mqtt_port=None) -> None:
    """(Re)write the scratch ASGI script with the given version string baked in."""
    previous_mtime = script.stat().st_mtime_ns if script.exists() else 0
    script.write_text(textwrap.dedent(f'''
        import asyncio, logging, os, ssl
        from pathlib import Path
        logging.basicConfig(
            level=logging.INFO,
            format='[%(asctime)s pid=%(process)d %(name)s %(levelname)s] %(message)s',
        )
        logging.info('reload_app starting pid=%d cwd=%s', os.getpid(), os.getcwd())
        from blackbull import BlackBull, Listener, Tcp, WebSocket
        from blackbull.utils import Scheme

        if os.environ.get('TEST_RELOAD_STARTUP_TIMEOUT'):
            from blackbull.server import reload_generation
            reload_generation.STARTUP_TIMEOUT = 4

        app = BlackBull()

        if {mqtt_port!r} is not None:
            from blackbull.mqtt import MQTTExtension
            app.add_extension(MQTTExtension(port={mqtt_port!r}))

        if {raw_port!r} is not None:
            async def raw(reader, writer, ctx):
                if await reader.readexactly(1) == b'H':
                    await writer.write(b'waiting')
                    while not Path('release').exists():
                        await asyncio.sleep(0.05)
                await writer.write(b'{version}')

            app.register_protocol_handler('raw', raw, port={raw_port!r})

        @app.on_startup
        async def startup():
            if {startup_fails!r}:
                raise RuntimeError('broken application startup')
            if {startup_delay}:
                logging.info('startup delayed')
                await asyncio.sleep({startup_delay})

        @app.route(path='/version')
        async def version():
            return b'{version}'

        @app.route(path='/ws', scheme=Scheme.websocket)
        async def websocket(ws: WebSocket):
            await ws.accept()
            async for message in ws:
                await ws.send_text('{version}')

        @app.route(path='/supervisor')
        async def supervisor():
            return str(os.getppid())

        @app.route(path='/held')
        async def held():
            logging.info('held request entered')
            while not Path('release').exists():
                await asyncio.sleep(0.05)
            return b'{version}'

        if __name__ == '__main__':
            other = {extra_port!r}
            if other is None:
                app.run(port={port}, workers=2, reload=True)
            else:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain({str(_CERT_DIR / 'cert.pem')!r},
                                        {str(_CERT_DIR / 'key.pem')!r})
                listeners = [Listener(Tcp({port}, {host!r})),
                             Listener(Tcp(other, '127.0.0.1'), tls=context)]
                app.run(listeners=listeners, workers=2, reload=True)
    ''').lstrip())
    stamp = max(time.time_ns(), previous_mtime + 1_000_000_000)
    os.utime(script, ns=(stamp, stamp))


@pytest.mark.timeout(_HARD_TIMEOUT_SEC)
@pytest.mark.parametrize('entry', [
    'script', 'listeners', 'raw', 'mqtt', 'module',
    pytest.param('module-options', marks=pytest.mark.no_audit(
        reason='The child runs with -O, which the execution audit cannot trace.')),
    'console'])
def test_auto_reload_picks_up_new_code(tmp_path: Path, child_env, entry):
    port = _free_port()
    script = tmp_path / 'reload_app.py'
    extra_port = _free_port() if entry == 'listeners' else None
    while extra_port == port:
        extra_port = _free_port()
    raw_port = _free_port() if entry == 'raw' else None
    while raw_port == port:
        raw_port = _free_port()
    mqtt_port = _free_port() if entry == 'mqtt' else None
    while mqtt_port == port:
        mqtt_port = _free_port()
    layout = dict(extra_port=extra_port, raw_port=raw_port, mqtt_port=mqtt_port)
    _write_app(script, port, 'v1', **layout)

    # PYTHONUNBUFFERED makes any diagnostic output appear promptly when
    # the test fails.
    env = child_env({'PYTHONUNBUFFERED': '1'}, cwd=str(tmp_path))
    env['BB_ACCESS_LOG'] = '0'   # quieter test output
    env['BB_KEEP_ALIVE_TIMEOUT'] = '40'
    env['BB_HEADER_TIMEOUT'] = '40'
    if entry == 'script':
        env['TEST_RELOAD_STARTUP_TIMEOUT'] = '1'
    if mqtt_port is not None:
        env['BB_WORKER_DRAIN_TIMEOUT'] = '3'
    # Force watchfiles into polling mode for the subprocess's watcher.
    # watchfiles' default inotify backend silently drops the rewrite event on
    # WSL2's drvfs/9p filesystem, and polling is backend-agnostic; the reload
    # handoff logic under test is identical either way.  Polling has its own
    # blind spot — it reports a file as modified only when the mtime moves
    # *forward*, so a backwards clock step drops the save outright — which is
    # what stage 3's retry exists to survive.
    env['WATCHFILES_FORCE_POLLING'] = '1'
    env['WATCHFILES_POLL_DELAY_MS'] = '100'

    # Capture to a file rather than a pipe: an unread PIPE eventually
    # blocks the subprocess on writes once the kernel buffer (~64 KiB)
    # fills, which silently stalls reload progression mid-test.
    log_path = tmp_path / 'subprocess.log'
    log_fh = open(log_path, 'w', buffering=1)
    cli_args = ['reload_app:app', '--bind', f'127.0.0.1:{port}',
                '--reload', '--reload-path', str(tmp_path)]
    if entry in ('script', 'listeners', 'raw', 'mqtt'):
        command = [sys.executable, str(script)]
    elif entry == 'console':
        command = ['blackbull', *cli_args]
    else:
        options = ['-O', '-W', 'default', '-X', 'dev'] if entry == 'module-options' else []
        command = [sys.executable, *options, '-m', 'blackbull.cli', *cli_args]
    spawn_start = time.monotonic()
    proc = subprocess.Popen(
        command,
        env=env,
        cwd=str(tmp_path),
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        text=True,
        # Detach from pytest's process group / session so signals
        # pytest delivers don't fall through, and so watchfiles' inotify
        # doesn't see any artefacts of the parent's MP fork churn.
        start_new_session=True,
        close_fds=True,
    )

    probe = ListenerProbe(port)
    stages = StageTimeline()
    stages.record('spawn', time.monotonic() - spawn_start)
    retried = False
    held = None
    idle = None
    untouched = None
    partial = None
    held_h2 = None
    websocket = None
    raw_held = None
    mqtt_held = None
    try:
        # ---- stage 1: the v1 server answers -------------------------------
        stages.require('server up',
                       lambda: _get_version(port) == 'v1',
                       'GET /version == v1')

        # ---- stage 2: the watcher is armed --------------------------------
        stages.require('watcher armed',
                       lambda: _occurrences(log_path, _WATCHER_ARMED) > 0,
                       f'{_WATCHER_ARMED!r} in the subprocess log')

        # Even an armed polling watcher races its own first scan: the
        # baseline snapshot has to complete before a write can be seen as a
        # change.  The wait matches the real dev-time path (save happens
        # seconds after start), and stage 3's retry covers the rest.
        time.sleep(1.5)

        # Baselines taken *after* arming: only occurrences caused by our
        # rewrite count, so a stray earlier event cannot satisfy a stage.
        seen_before = _occurrences(log_path, _CHANGE_SEEN)
        recycle_before = _occurrences(log_path, _RECYCLE_BEGUN)

        # The listener invariant spans everything from here to v2.
        probe.start()

        if entry in ('script', 'listeners', 'raw', 'mqtt'):
            _write_app(script, port, 'broken', startup_fails=entry != 'listeners',
                       host='127.0.0.1', **layout)
            assert _await('new code live', lambda: 'reload: candidate failed' in _log_text(log_path))
            assert _get_version(port) == 'v1'
            assert proc.poll() is None
            seen_before = _occurrences(log_path, _CHANGE_SEEN)
            recycle_before = _occurrences(log_path, _RECYCLE_BEGUN)
            if entry == 'script':
                _write_app(script, port, 'slow', startup_delay=60)
                assert _await('recycle begun', lambda: 'startup delayed' in _log_text(log_path))
                assert _get_version(port) == 'v1'
                assert _await('new code live', lambda: 'before the deadline' in _log_text(log_path))
                assert _get_version(port) == 'v1'
                seen_before = _occurrences(log_path, _CHANGE_SEEN)
                recycle_before = _occurrences(log_path, _RECYCLE_BEGUN)
                untouched = socket.create_connection(('127.0.0.1', port), timeout=2)
                websocket = connect_websocket(f'ws://127.0.0.1:{port}/ws', open_timeout=2, close_timeout=2)
                websocket.send('version')
                assert websocket.recv(timeout=2) == 'v1'
                idle = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
                partial = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
                for connection in (idle, partial):
                    connection.request('GET', '/version')
                    with connection.getresponse() as reply:
                        assert reply.read() == b'v1'
                partial.sock.sendall(b'GET /version HTTP/1.1\r\nHost:')
                held = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                held.request('GET', '/held', headers={'Connection': 'close'})
                held_h2 = _open_h2_request(port)
                assert _await('recycle begun', lambda: _occurrences(log_path, 'held request entered') == 2)
                held_h2.sendall(_H2_PING[:11])
            elif extra_port is not None:
                assert _get_version(extra_port, tls=True) == 'v1'
            elif raw_port is not None:
                raw_held = socket.create_connection(('127.0.0.1', raw_port), timeout=5)
                raw_held.sendall(b'H')
                assert raw_held.recv(7, socket.MSG_WAITALL) == b'waiting'
            else:
                mqtt_held = _open_mqtt(mqtt_port)

        # ---- stage 3: the watcher observes the rewrite --------------------
        _write_app(script, port, 'v2', **layout)

        def change_seen() -> bool:
            return _occurrences(log_path, _CHANGE_SEEN) > seen_before

        if not stages.run('change seen', change_seen):
            # A user who saves and sees nothing saves again.  If the second
            # write lands, the defect is event delivery, not the reload
            # machinery — a distinction the old single deadline could not
            # make.  (It is: the poller drops a save whose mtime predates its
            # last snapshot, which a backwards clock step produces.)
            retried = True
            _write_app(script, port, 'v2', **layout)
            if not stages.run('change seen', change_seen):
                _fail_stage('change seen',
                            f'{_CHANGE_SEEN!r} after two rewrites '
                            '(watcher is not delivering events at all)')

        # ---- stage 4: the master acts on it -------------------------------
        stages.require('recycle begun',
                       lambda: _occurrences(log_path, _RECYCLE_BEGUN) > recycle_before,
                       f'{_RECYCLE_BEGUN!r} — the watcher fired but the master '
                       'did not act on the flag')

        # ---- stage 5: the new code serves ---------------------------------
        stages.require('new code live',
                       lambda: _get_version(port) == 'v2',
                       'GET /version == v2 after reload')
        if extra_port is not None:
            assert _get_version(extra_port, tls=True) == 'v2'
        if raw_held is not None:
            assert _await('recycle begun', lambda: 'Draining ' in _log_text(log_path))
            with socket.create_connection(('127.0.0.1', raw_port), timeout=0.2) as raw:
                raw.sendall(b'V')
                with pytest.raises(TimeoutError):
                    raw.recv(2)
                (tmp_path / 'release').touch()
                assert raw_held.recv(2, socket.MSG_WAITALL) == b'v1'
                raw.settimeout(5)
                assert raw.recv(2, socket.MSG_WAITALL) == b'v2'
        if mqtt_held is not None:
            mqtt_held.sendall(b'\xc0\x00')  # PINGREQ on an old session
            assert mqtt_held.recv(2, socket.MSG_WAITALL) == b'\xd0\x00'
            assert mqtt_held.recv(1) == b''
            with _open_mqtt(mqtt_port):
                pass
        if held is not None:
            assert untouched.recv(1) == b''
            assert idle.sock.recv(1) == b''
            with pytest.raises(ConnectionClosedOK) as exc:
                websocket.recv(timeout=2)
            assert exc.value.rcvd.code == 1001
            partial.sock.sendall(b' localhost\r\nConnection: close\r\n\r\n')
            with http.client.HTTPResponse(partial.sock) as reply:
                reply.begin()
                assert reply.status == 200
                assert reply.read() == b'v1'
            with held_h2.makefile('rb') as reader:
                while (frame := _read_h2_frame(reader)) is not None and frame[0] != 7:
                    pass
                assert frame == (7, 0, b'\x00\x00\x00\x01\x00\x00\x00\x00')
                held_h2.sendall(_H2_PING[11:])
                assert _read_h2_frame(reader) == (6, 0, b'12345678')
                _write_app(script, port, 'v3')
                (tmp_path / 'release').touch()
                body = bytearray()
                while (frame := _read_h2_frame(reader)) is not None:
                    if frame[:2] == (0, 1):
                        body.extend(frame[2])
                assert body == b'v1'
            with held.getresponse() as reply:
                assert reply.status == 200
                assert reply.read() == b'v1'
        if entry == 'script':
            if not _await('new code live', lambda: _get_version(port) == 'v3'):
                _write_app(script, port, 'v3')
                assert _await('new code live', lambda: _get_version(port) == 'v3')
            supervisor = int(_get_version(port, '/supervisor'))
            assert supervisor != proc.pid and os.getpgid(supervisor) == supervisor
            os.kill(supervisor, signal.SIGKILL)

            def recovered():
                reply = _get_version(port, '/supervisor')
                return reply is not None and int(reply) not in (1, proc.pid, supervisor)

            assert _await('new code live', recovered)
            assert _get_version(port) == 'v3'
    finally:
        if websocket is not None:
            websocket.close()
        if raw_held is not None:
            (tmp_path / 'release').touch()
            raw_held.close()
        if mqtt_held is not None:
            mqtt_held.close()
        if untouched is not None:
            untouched.close()
        for connection in (idle, partial):
            if connection is not None:
                connection.close()
        if held is not None:
            (tmp_path / 'release').touch()
            held.close()
        if held_h2 is not None:
            held_h2.close()
        probe.stop()
        teardown_start = time.monotonic()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        stages.record('teardown', time.monotonic() - teardown_start)
        log_fh.close()
        # Surface the timeline and the subprocess log when the test failed
        # so the post-mortem doesn't require re-running with -s.
        print('--- reload stage timeline ---')
        print(stages.render())
        log_text = _log_text(log_path)
        if log_text:
            # Pytest captures this; appears under "Captured stdout call".
            print(f'--- subprocess log ({len(log_text)} bytes) ---')
            print(log_text[-4000:])  # tail in case it's huge

    if retried:
        warnings.warn(
            'the first rewrite was not observed; the second one was. The '
            'reload pipeline is healthy, but an event was dropped between '
            'the filesystem and the watcher.',
            DroppedReloadEvent,
            stacklevel=1,
        )

    # A small handful of failed connects during the worker-recycle window is
    # tolerable; a sustained outage means the listening socket was closed
    # during reload, which the supervisor must prevent.
    assert probe.samples > 0, 'listener probe never sampled — probe thread died'
    assert probe.failures < 10, (
        f'listening socket refused {probe.failures} of '
        f'{probe.samples + probe.failures} connects during reload '
        '(it should stay bound across the handoff)'
    )
    with pytest.raises(OSError):
        socket.create_connection(('127.0.0.1', port), timeout=_REQ_TIMEOUT_SEC)

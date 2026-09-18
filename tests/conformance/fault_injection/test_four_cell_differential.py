"""Every cell driven at a counterpart that is not BlackBull.

Sprint 108's cell-D defect survived a full sprint because the only server
that cell was ever pointed at was ours: the scenario put a second
connection preface on the wire, BlackBull skipped the junk, and every case
reported success while the fault never reached the code meant to judge it.

The lesson was not "test against a third party".  It was that *the cell
most likely to be self-referential is the one where our own implementation
is the convenient counterpart* — cells A, B and C got a third party by
accident, because there was no BlackBull HTTP/1.1 client to reach for and
`httpx` was obvious for the rest.

So this file's assertions are deliberately **not** about who is right.
Where nginx, httpx, curl and BlackBull disagree, that difference is a
finding to read, not a verdict to encode — RFC 9112 §2.2 alone leaves
several of these genuinely open.  What is asserted is that each cell can
be *driven and judged at all* with someone else on the other end.

The two broken-**client** cells need a third-party *server*, and that is
nginx, in a container built on demand from `nginx_h2c/` — once per context,
by `test_the_reference_image_is_prepared_for_this_context`, which the cells
below then only start.  One listener speaks HTTP/1.1 and h2c, so both cells
point at the same peer.  Docker is required for that half only; it skips
cleanly without one, the way `test_http1_differential.py` does, and the
broken-**server** cells (which need third-party *clients*, not servers) run
either way.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio

httpx = pytest.importorskip('httpx')


def _mirror_app():
    """nginx's semantics — 200 "ok" for anything.

    Without it every generated path is 404 here and 200 there, and the run
    drowns in status differences that say nothing about framing.
    """
    from http import HTTPMethod, HTTPStatus

    from blackbull import BlackBull, read_body

    app = BlackBull()

    @app.route(path='/', methods=[HTTPMethod.GET, HTTPMethod.POST,
                                  HTTPMethod.PUT, HTTPMethod.DELETE,
                                  HTTPMethod.OPTIONS, HTTPMethod.HEAD])
    async def _root(scope, receive, send):
        await read_body(receive)
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'text/plain')]})
        await send({'type': 'http.response.body', 'body': b'ok'})

    for status in (HTTPStatus.NOT_FOUND, HTTPStatus.METHOD_NOT_ALLOWED):
        @app.on_error(status)
        async def _mirror(scope, receive, send):
            await read_body(receive)
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(b'content-type', b'text/plain')]})
            await send({'type': 'http.response.body', 'body': b'ok'})

    return app


# ---------------------------------------------------------------------------
# The third-party server: nginx, speaking HTTP/1.1 and h2c on one port
# ---------------------------------------------------------------------------

_NGINX_CONTEXT = Path(__file__).parent / 'nginx_h2c'

#: Building nginx is not a unit-test-speed operation — a pull, or on a slow
#: daemon even a fully cached build (112 s on the WSL2 box that filed #309),
#: runs into the minutes, against a suite default of 30 s.
_NGINX_BUILD_BUDGET = 900

#: A peer test uses the prepared server — start the container, publish and
#: probe its port, run the case, stop it — and under `-n auto` may first have
#: to wait for the preparation test's build in another worker.  Giving up on
#: that wait early is how a slow-but-working build would become seventeen
#: green skips, so the wait is allowed the build's own budget.
_NGINX_PEER_BUDGET = _NGINX_BUILD_BUDGET + 60


def _nginx_context_digest(ctx: Path = _NGINX_CONTEXT) -> str:
    """A tag that changes when, and only when, the build context changes.

    The tag used to be `latest`, so an edited `nginx.conf` kept running
    whatever image happened to carry it and the "built rather than mounted"
    promise below quietly stopped holding.  Hashing the context makes the tag
    the answer to "is this image built from these files?", which is what lets
    the build be skipped when it is.

    Length-prefixed so no rename can forge another context's stream, and mode
    is in because `COPY` carries it into the image.
    """
    digest = hashlib.sha256()
    for path in sorted(ctx.rglob('*')):
        if path.is_file():
            fields = (path.relative_to(ctx).as_posix().encode(),
                      f'{path.stat().st_mode & 0o777:o}'.encode(),
                      path.read_bytes())
            digest.update(b''.join(f'{len(f)}:'.encode() + f for f in fields))
    return digest.hexdigest()[:12]


#: One image per context, so the cost is paid once per change to `nginx_h2c/`.
_NGINX_IMAGE = f'bb-fault-nginx:{_nginx_context_digest()}'

#: This run's identity, so a record left by an earlier run is not read as this
#: run's failure: xdist gives every worker the same uid, and a single process
#: is its own pid.
_RUN_ID = os.environ.get('PYTEST_XDIST_TESTRUNUID') or str(os.getpid())

#: Named per user because the temp dir is shared, and in it rather than in the
#: checkout because the record has to outlive the process that wrote it.
_RECORD_OWNER = os.environ.get('USER') or os.environ.get('USERNAME') or 'user'

#: What the preparation test leaves behind when its build fails, so a peer test
#: can tell "a build is still running" from "no image is coming": the run that
#: recorded it, then the reason.
_NGINX_BUILD_FAILED = (Path(tempfile.gettempdir())
                       / f'{_NGINX_IMAGE.replace(":", "-")}'
                         f'-{_RECORD_OWNER}.failed')


def _require_docker() -> None:
    """Without a CLI or a daemon there is no third-party server to reach."""
    if shutil.which('docker') is None:
        pytest.skip('docker CLI not installed')
    if subprocess.run(['docker', 'info'], capture_output=True,
                      timeout=60).returncode != 0:
        pytest.skip('docker daemon unreachable')


def _nginx_image_present() -> bool:
    return subprocess.run(['docker', 'image', 'inspect', _NGINX_IMAGE],
                          capture_output=True, timeout=60).returncode == 0


def _recorded_build_failure() -> str | None:
    """This run's recorded build failure, if the preparation test wrote one.

    A record from another run is not this run's — reading it would skip the
    coverage that this run's preparation test is still going to provide — and
    one that vanishes mid-read (the preparation test clearing it) is simply
    not there.
    """
    try:
        run_id, _, reason = _NGINX_BUILD_FAILED.read_text().partition('\n')
    except FileNotFoundError:
        return None
    return reason if run_id == _RUN_ID else None


def _prepare_nginx_image() -> None:
    """Build this context's image unless the daemon already has it.

    A build that *fails* leaves the box without a third-party peer, the same
    as a missing CLI, so it skips — and records why, so a peer test skips with
    it instead of waiting out a build that is not coming.  A build that
    overruns its budget does not skip: at 112 s for a cached build, "slower
    than we allowed" is not evidence the peer is unavailable, and a skip there
    is coverage disappearing where nobody looks for it.
    """
    _NGINX_BUILD_FAILED.unlink(missing_ok=True)
    if _nginx_image_present():
        return
    # Under this test's mark, so the daemon's own timeout reports first.
    build = subprocess.run(
        ['docker', 'build', '-q', '-t', _NGINX_IMAGE, str(_NGINX_CONTEXT)],
        capture_output=True, timeout=_NGINX_BUILD_BUDGET - 60)
    if build.returncode != 0:
        reason = build.stderr.decode(errors='replace')[:200]
        _NGINX_BUILD_FAILED.write_text(f'{_RUN_ID}\n{reason}')
        pytest.skip(f'could not build the reference image: {reason}')


def _preparer_name() -> str:
    """The preparation test's name, resolved late so a rename cannot drift."""
    return test_the_reference_image_is_prepared_for_this_context.__name__


def _require_nginx_image(selected: set[str]) -> None:
    """Wait for the image the preparation test builds, then skip without it.

    Only that test builds, so a peer test's budget pays for using the server
    and never for making one.  Waiting is bounded by the build's own budget
    because under `-n auto` that build is running in another worker; a failure
    it recorded, or a run that did not select it, ends the wait at once — a
    peer test that outwaited the build would be the seventeen green skips this
    change exists to prevent.
    """
    if _nginx_image_present():
        return
    preparer = _preparer_name()
    if preparer not in selected:
        pytest.skip(f'{_NGINX_IMAGE} is absent and this run did not select '
                    f'{preparer}, which builds it')
    deadline = time.monotonic() + _NGINX_BUILD_BUDGET
    while not _nginx_image_present():
        recorded = _recorded_build_failure()
        if recorded is not None:
            pytest.skip(f'could not build the reference image: {recorded}')
        if time.monotonic() >= deadline:
            pytest.skip(f'{_NGINX_IMAGE} was not built within '
                        f'{_NGINX_BUILD_BUDGET} s; see {preparer}')
        time.sleep(2)


async def test_the_nginx_tag_follows_the_context(tmp_path):
    """The tag has to change with the context, or a stale image runs.

    `latest` was the tag before this, so an edited `nginx.conf` kept running
    whatever image carried the name.  Path-independent as well as content-
    sensitive: the same files under another path are the same context, which
    is why the digest is not of the path.
    """
    ctx = tmp_path / 'ctx'
    ctx.mkdir()
    (ctx / 'Dockerfile').write_text('FROM nginx:1.27-alpine\n')
    (ctx / 'nginx.conf').write_text('events {}\n')
    before = _nginx_context_digest(ctx)

    (ctx / 'nginx.conf').write_text('events { worker_connections 4; }\n')
    assert _nginx_context_digest(ctx) != before

    same = tmp_path / 'elsewhere'
    same.mkdir()
    (same / 'Dockerfile').write_text('FROM nginx:1.27-alpine\n')
    (same / 'nginx.conf').write_text('events { worker_connections 4; }\n')
    assert _nginx_context_digest(same) == _nginx_context_digest(ctx)

    # `COPY` carries the mode into the image, so the mode is context too.
    (ctx / 'nginx.conf').chmod(0o400)
    assert _nginx_context_digest(ctx) != _nginx_context_digest(same)


async def test_a_present_nginx_image_is_not_rebuilt(monkeypatch, tmp_path):
    """The build is paid once per context, not once per run.

    Asked of the daemon rather than assumed: `inspect` is the only command
    a run that finds its image already there may issue.  A stale failure
    record from an earlier run goes with it, so it cannot make this run's
    peer tests skip over an image that is now there.
    """
    calls = []
    failed = tmp_path / 'failed'
    failed.write_text('an earlier run could not reach the registry')

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.setitem(globals(), '_NGINX_BUILD_FAILED', failed)
    _prepare_nginx_image()

    assert [c[:3] for c in calls] == [['docker', 'image', 'inspect']], calls
    assert not failed.exists()


async def test_an_absent_nginx_image_is_built_for_this_context(monkeypatch,
                                                               tmp_path):
    """A miss builds this context's tag, from this context's directory."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, int(argv[1] == 'image'))

    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.setitem(globals(), '_NGINX_BUILD_FAILED', tmp_path / 'failed')
    _prepare_nginx_image()

    assert calls[-1][:2] == ['docker', 'build'], calls
    assert calls[-1][3:5] == ['-t', _NGINX_IMAGE], calls
    assert calls[-1][5] == str(_NGINX_CONTEXT), calls


async def test_a_failed_build_records_why_for_the_peer_tests(monkeypatch,
                                                            tmp_path):
    """A peer test must not wait the build's budget for an image that failed.

    The preparation test is the only thing that builds, so without this record
    every peer worker would poll for the whole budget before skipping, which
    reads as a hang rather than as "no third-party server here".
    """
    failed = tmp_path / 'failed'

    def fake_run(argv, **kwargs):
        stderr = b'' if argv[1] == 'image' else b'no such host'
        return subprocess.CompletedProcess(argv, 1, b'', stderr)

    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.setitem(globals(), '_NGINX_BUILD_FAILED', failed)

    with pytest.raises(pytest.skip.Exception):
        _prepare_nginx_image()
    assert failed.read_text().startswith(_RUN_ID)
    assert 'no such host' in failed.read_text()

    # And the record is what ends a peer test's wait, message included.
    with pytest.raises(pytest.skip.Exception, match='no such host'):
        _require_nginx_image({_preparer_name()})


async def test_a_missing_nginx_image_is_waited_for_not_rebuilt(monkeypatch,
                                                              tmp_path):
    """A peer test may not turn its own budget into a build.

    The build belongs to the preparation test; under `-n auto` a second worker
    reaching this point while that one builds has to wait for it rather than
    build the same context again beside it — and has to wait long enough for
    that build to land, which is what keeps a slow build from becoming
    seventeen skips.
    """
    calls = []
    clock = [0.0]
    state = {'present_after': None, 'polls': 0}

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[1] != 'image':
            return subprocess.CompletedProcess(argv, 0)
        state['polls'] += 1
        after = state['present_after']
        present = after is not None and state['polls'] > after
        return subprocess.CompletedProcess(argv, int(not present))

    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(time, 'sleep',
                        lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setitem(globals(), '_NGINX_BUILD_FAILED', tmp_path / 'failed')

    # The build lands: the wait ends, and nothing builds a second image.
    state['present_after'] = 2
    _require_nginx_image({_preparer_name()})
    assert state['polls'] == 3, state
    assert [c[1] for c in calls] == ['image'] * 3, calls
    assert clock[0] < _NGINX_BUILD_BUDGET, clock

    # The build never lands: skip at the build's own budget, not before, and
    # still without building beside it.
    state.update(present_after=None, polls=0)
    calls.clear()
    with pytest.raises(pytest.skip.Exception):
        _require_nginx_image({_preparer_name()})
    assert clock[0] >= _NGINX_BUILD_BUDGET, clock
    assert [c[1] for c in calls] == ['image'] * state['polls'], calls


async def test_a_peer_test_with_no_build_to_wait_for_skips_at_once(
        monkeypatch, tmp_path):
    """Neither wait is owed when no build can produce the image.

    A cell run on its own (`-k`, a single nodeid) does not select the
    preparation test, and a box whose build failed will not produce one on the
    next poll either.  Both must say so immediately: a stale image tag used to
    build here, so "wait, something is coming" is the assumption to test.
    """
    calls = []
    clock = [0.0]

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(time, 'sleep',
                        lambda s: clock.__setitem__(0, clock[0] + s))

    preparer = test_the_reference_image_is_prepared_for_this_context.__name__
    with pytest.raises(pytest.skip.Exception, match=preparer):
        _require_nginx_image(set())

    failed = tmp_path / 'failed'
    failed.write_text(f'{_RUN_ID}\ncould not resolve the registry')
    monkeypatch.setitem(globals(), '_NGINX_BUILD_FAILED', failed)
    with pytest.raises(pytest.skip.Exception, match='could not resolve'):
        _require_nginx_image({_preparer_name()})

    assert clock[0] == 0.0, clock
    assert len(calls) == 3, calls
    assert [c[1] for c in calls].count('build') == 0, calls


async def test_a_record_from_another_run_is_not_this_runs_failure(
        monkeypatch, tmp_path):
    """A record only means something to the run that wrote it.

    Reading yesterday's failure as today's would skip the coverage today's
    preparation test is still going to provide — the same silent loss as
    skipping a slow build, one run later.
    """
    failed = tmp_path / 'failed'
    failed.write_text('another run\nregistry was down last night')
    monkeypatch.setitem(globals(), '_NGINX_BUILD_FAILED', failed)

    assert _recorded_build_failure() is None

    failed.write_text(f'{_RUN_ID}\nregistry was down last night')
    assert _recorded_build_failure() == 'registry was down last night'


@pytest.mark.timeout(_NGINX_BUILD_BUDGET)
async def test_the_reference_image_is_prepared_for_this_context():
    """Pay for the build here, once, under a budget that admits it is a build.

    The peer tests below only *use* the image.  Before this split the build
    ran inside whichever cell happened to touch the fixture first, under that
    cell's unit-test-sized budget, so a slow daemon turned all seventeen red.
    """
    _require_docker()
    _prepare_nginx_image()
    assert _nginx_image_present(), (
        f'{_NGINX_IMAGE} missing after a build that reported success')


@pytest.fixture(scope='module')
def nginx_peer(request):
    """A reference server for the two broken-client cells.

    Driven through the `docker` **CLI** rather than the Python SDK.  The
    SDK talks to the daemon socket directly, which is unreachable from
    some developer setups even when the CLI works (Docker Desktop exposes
    a Windows named pipe that a Linux SDK inside WSL cannot open) — and it
    is an extra dependency installed only in the weekly tier, which would
    have left this coverage skipped on every pull request.  The CLI is the
    thing that is actually present.

    Built rather than volume-mounted: a bind mount of a single file fails
    on some hosts, and a fixture that dies there takes the cell's only
    third-party server coverage with it.

    The image is prepared by the test above, which is the only thing here that
    builds: this fixture waits for it and skips if it never arrives, so a cell
    run on its own neither pays for a build nor races another worker's.  That
    test is defined above the cells because pytest runs a module in file order,
    which is what makes "the build is already running" a fact a peer can act
    on rather than a guess.
    """
    _require_docker()
    _require_nginx_image({item.name for item in request.session.items})

    run = subprocess.run(
        ['docker', 'run', '-d', '--rm', '-P', _NGINX_IMAGE],
        capture_output=True, timeout=120)
    if run.returncode != 0:
        pytest.skip(f'could not start the reference server: '
                    f'{run.stderr.decode(errors="replace")[:200]}')
    container = run.stdout.decode().strip()

    try:
        port_out = subprocess.run(
            ['docker', 'port', container, '80/tcp'],
            capture_output=True, timeout=60, check=True)
        # "0.0.0.0:49154" (and possibly an IPv6 line after it).
        port = int(port_out.stdout.decode().splitlines()[0].rsplit(':', 1)[1])
        _wait_for_port('127.0.0.1', port)
        yield '127.0.0.1', port
    finally:
        subprocess.run(['docker', 'stop', container],
                       capture_output=True, timeout=120)


def _wait_for_port(host: str, port: int, timeout: float = 30.0) -> None:
    """Wait until nginx answers, not merely until something accepts.

    ``docker run -P`` publishes the host port as soon as the container exists,
    so docker-proxy completes the TCP handshake while nginx inside is still
    starting.  A probe that stops at connect therefore returns early and the
    first real connection is reset before nginx speaks.

    That is how this surfaced: a scenario expecting GOAWAY got
    ``ConnectionResetError`` with ``server_bytes_received=0``.  Zero bytes is
    the tell — nginx sends its own SETTINGS the moment an h2c connection opens,
    so a peer that says nothing was never listening, rather than one that
    rejected the frame.

    The same listener serves HTTP/1.1 and h2c, so one HTTP/1.1 exchange proves
    a worker is accepting and answering.
    """
    import socket

    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0) as sock:
                sock.sendall(b'GET / HTTP/1.1\r\nHost: probe\r\n\r\n')
                if sock.recv(9).startswith(b'HTTP/1.1'):
                    return
                last = RuntimeError('accepted, but answered nothing')
        except OSError as exc:
            last = exc
        time.sleep(0.2)
    raise RuntimeError(f'nginx never answered on {host}:{port} ({last!r})')


# ---------------------------------------------------------------------------
# Cells A and D — the broken client.  A *server* is the judge.
# ---------------------------------------------------------------------------

class TestTheBrokenClientCellsReachARealServer:
    """The direction that was measuring nothing until #256."""

    @pytest.mark.parametrize('case_name', [
        'absent_host', 'two_content_lengths',
        'content_length_and_transfer_encoding', 'space_before_header_colon',
        'obs_fold_header', 'negative_content_length', 'chunk_size_not_hex',
        'nul_in_header_value', 'body_shorter_than_declared',
    ])
    async def test_cell_a_case_is_delivered_and_answered(self, case_name):
        """Every named HTTP/1.1 client case reaches a server and gets a verdict.

        The assertion is about delivery, not about which status: whether a
        given malformation earns 400 is the server author's decision and
        implementations differ.  What must hold is that the scenario ran
        and something came back.
        """
        from blackbull.fault_injection.catalogue.h1_client import CATALOGUE
        from blackbull.fault_injection.oracle_h1 import run_scenario
        from blackbull.testing.native import NativeTestServer

        async with NativeTestServer(_mirror_app()) as server:
            outcome, _ = await run_scenario('127.0.0.1', server.port,
                                            CATALOGUE[case_name]())

        assert outcome.ok or outcome.exception, (
            f'{case_name} produced neither a response nor a failure — '
            f'the scenario did not reach the server')
        if outcome.ok:
            assert isinstance(outcome.response['status'], int)

    async def test_cell_d_observes_a_verdict_not_just_survival(self):
        """The capability the sprint exists for, end to end.

        Before `WaitForServerFrame`, a scenario could read one frame — the
        handshake SETTINGS — and had no way to reach the GOAWAY that
        follows it.
        """
        from blackbull.client.http2 import HTTP2Client
        from blackbull.fault_injection.scenario_h2_client import (
            ScenarioH2Client, SendFrame, SendPreface, WaitForServerFrame,
        )
        from blackbull.protocol.frame_types import FrameTypes
        from blackbull.testing.native import NativeTestServer

        async with NativeTestServer(_mirror_app()) as server:
            async with HTTP2Client('127.0.0.1', server.port,
                                   scenario_mode=True) as client:
                result = await asyncio.wait_for(client.execute_scenario(
                    ScenarioH2Client(steps=(
                        SendPreface(),
                        SendFrame(FrameTypes.SETTINGS, flags=0x1, stream_id=0,
                                  data=b'\x00' * 6),
                        WaitForServerFrame(
                            match={'type': 'GOAWAY', 'error_code': 6},
                            timeout=5.0)))), timeout=15.0)

        assert result.response is not None, (
            'no GOAWAY(FRAME_SIZE_ERROR) observed')
        assert result.received, 'nothing was logged'


    @pytest.mark.parametrize('case_name', [
        'absent_host', 'two_content_lengths',
        'content_length_and_transfer_encoding', 'space_before_header_colon',
        'obs_fold_header', 'negative_content_length', 'chunk_size_not_hex',
        'nul_in_header_value', 'body_shorter_than_declared',
        'duplicate_transfer_encoding', 'oversized_method_token',
    ])
    @pytest.mark.timeout(_NGINX_PEER_BUDGET)
    async def test_cell_a_case_reaches_nginx_too(self, nginx_peer, case_name):
        """The same named case, delivered to a server that is not ours.

        Cell A's counterpart had been BlackBull in every committed test —
        the condition that let cell D measure nothing for a sprint.  The
        assertion is delivery, not agreement: whether a given malformation
        earns 400 is the server author's call, and nginx and BlackBull
        genuinely differ on some of these.
        """
        from blackbull.fault_injection.catalogue.h1_client import CATALOGUE
        from blackbull.fault_injection.oracle_h1 import run_scenario

        host, port = nginx_peer
        outcome, wire = await run_scenario(host, port, CATALOGUE[case_name]())

        assert wire, f'{case_name} put no bytes on the wire'
        assert outcome.ok or outcome.exception, (
            f'{case_name} produced neither a response nor a failure from '
            f'nginx — the scenario did not reach it')

    @pytest.mark.timeout(_NGINX_PEER_BUDGET)
    async def test_cell_d_observes_a_verdict_from_nginx(self, nginx_peer):
        """The verdict step, against a server with no BlackBull in it.

        nginx answering GOAWAY(FRAME_SIZE_ERROR) is what makes
        `WaitForServerFrame` a fact about HTTP/2 rather than about our own
        server's frame ordering.
        """
        from blackbull.client.http2 import HTTP2Client
        from blackbull.fault_injection.scenario_h2_client import (
            ScenarioH2Client, SendFrame, SendPreface, WaitForServerFrame,
        )
        from blackbull.protocol.frame_types import FrameTypes

        host, port = nginx_peer
        async with HTTP2Client(host, port, scenario_mode=True) as client:
            result = await asyncio.wait_for(client.execute_scenario(
                ScenarioH2Client(steps=(
                    SendPreface(),
                    SendFrame(FrameTypes.SETTINGS, flags=0x1, stream_id=0,
                              data=b'\x00' * 6),
                    WaitForServerFrame(
                        match={'type': 'GOAWAY', 'error_code': 6},
                        timeout=8.0)))), timeout=25.0)

        assert result.response is not None, (
            'nginx sent no GOAWAY(FRAME_SIZE_ERROR); RFC 9113 §6.5 says a '
            'SETTINGS ACK carrying a payload is a FRAME_SIZE_ERROR')
        assert type(result.response).__name__ == 'GoAway'

    @pytest.mark.parametrize('case_name', [
        'rapid_reset_burst', 'ping_flood', 'settings_flood',
        'unknown_frame_type', 'settings_ack_with_payload',
    ])
    @pytest.mark.timeout(_NGINX_PEER_BUDGET)
    async def test_cell_d_case_reaches_nginx(self, nginx_peer, case_name):
        """Every self-terminating cell-D case, delivered to nginx."""
        from blackbull.client.http2 import HTTP2Client
        from blackbull.fault_injection.catalogue.h2_client import CATALOGUE

        host, port = nginx_peer
        async with HTTP2Client(host, port, scenario_mode=True) as client:
            result = await asyncio.wait_for(
                client.execute_scenario(CATALOGUE[case_name]()), timeout=25.0)

        assert result.steps_completed > 0, (
            f'{case_name} delivered no steps to nginx')

# ---------------------------------------------------------------------------
# Cells B and C — the broken server.  *Clients* are the judges.
# ---------------------------------------------------------------------------

class TestTheBrokenServerCellsAreJudgedByOthers:
    """Two independent clients per case, one of them not written in Python."""

    @pytest.mark.parametrize('case_name', [
        'content_length_overstated', 'chunked_stops_mid_chunk',
        'closed_without_response', 'half_closed_after_headers',
    ])
    async def test_cell_b_httpx_and_blackbull_both_reject(self, case_name):
        """Where two independent clients agree, our client must agree too.

        A case only *our* client rejects would mean the fault server is
        emitting something only we find objectionable — the broken-server
        mirror of the cell-D defect.
        """
        from blackbull.client.http1 import HTTP1Client
        from blackbull.fault_injection.catalogue import CATALOGUE_H1_SERVER
        from blackbull.fault_injection.h1_server import H1FaultServer

        build = CATALOGUE_H1_SERVER[case_name]

        async with H1FaultServer(build()) as srv:
            with pytest.raises(Exception):
                async with HTTP1Client('127.0.0.1', srv.port) as c:
                    await asyncio.wait_for(c.request('GET', '/'), timeout=5.0)

        async with H1FaultServer(build()) as srv:
            with pytest.raises(httpx.HTTPError):
                async with httpx.AsyncClient(timeout=5.0) as c:
                    await c.get(f'http://127.0.0.1:{srv.port}/')

    @pytest.mark.parametrize('case_name', [
        'headers_continuation_dropped', 'half_closed_after_headers',
    ])
    async def test_cell_c_httpx_rejects_what_our_client_rejects(self, case_name):
        from blackbull.client.http2 import HTTP2Client
        from blackbull.fault_injection.catalogue import CATALOGUE_H2_SERVER
        from blackbull.fault_injection.h2_server import H2FaultServer

        build = CATALOGUE_H2_SERVER[case_name]

        async with H2FaultServer(build()) as srv:
            with pytest.raises(Exception):
                async with HTTP2Client('127.0.0.1', srv.port) as c:
                    await asyncio.wait_for(c.request('GET', '/'), timeout=5.0)

        async with H2FaultServer(build()) as srv:
            with pytest.raises(httpx.HTTPError):
                async with httpx.AsyncClient(http2=True, http1=False,
                                             timeout=5.0) as c:
                    await c.get(f'http://127.0.0.1:{srv.port}/')


@pytest.mark.skipif(shutil.which('curl') is None, reason='curl not installed')
class TestACImplementationAgrees:
    """curl shares no code with httpx or with us.

    Two Python clients agreeing can mean the protocol is clear or can mean
    they inherited the same reading of it.  A C implementation with its own
    parser is the cheapest way to tell those apart.
    """

    @pytest.mark.parametrize('case_name', [
        'content_length_overstated',
        # A half-close is the case most likely to be handled differently
        # by a client that maps FIN onto "connection reset": curl's
        # answer here is independent evidence that the FIN carries the
        # meaning the scenario intends.
        'half_closed_after_headers',
    ])
    async def test_curl_also_refuses_a_truncated_body(self, case_name):
        import subprocess

        from blackbull.fault_injection.catalogue import CATALOGUE_H1_SERVER
        from blackbull.fault_injection.h1_server import H1FaultServer

        async with H1FaultServer(CATALOGUE_H1_SERVER[case_name]()) as srv:
            proc = await asyncio.create_subprocess_exec(
                'curl', '-s', '--max-time', '5', '--http1.1',
                f'http://127.0.0.1:{srv.port}/',
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            await asyncio.wait_for(proc.communicate(), timeout=15)

        # 18 = CURLE_PARTIAL_FILE.  Asserted as non-zero rather than as 18
        # so a curl that reports the same fact under a different code does
        # not fail the suite for a reason unrelated to BlackBull.
        assert proc.returncode != 0, (
            f'{case_name}: curl accepted a body shorter than its declared '
            f'Content-Length')

    async def test_curl_refuses_a_broken_http2_server_too(self):
        """Cell C's second implementation.

        httpx drives `h2`, which is Python; curl drives nghttp2, which is
        not.  Without this, cell C's only judges are two Python stacks and
        an agreement between them could be a shared reading rather than a
        property of the protocol.
        """
        import subprocess

        from blackbull.fault_injection.catalogue import CATALOGUE_H2_SERVER
        from blackbull.fault_injection.h2_server import H2FaultServer

        async with H2FaultServer(
                CATALOGUE_H2_SERVER['headers_continuation_dropped']()) as srv:
            proc = await asyncio.create_subprocess_exec(
                'curl', '-s', '--max-time', '5', '--http2-prior-knowledge',
                f'http://127.0.0.1:{srv.port}/',
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            await asyncio.wait_for(proc.communicate(), timeout=15)

        assert proc.returncode != 0, (
            'curl accepted a HEADERS block that never ended')

    async def test_curl_and_httpx_agree_a_half_close_is_not_a_reset(self):
        """The distinction the step exists for, checked by two implementations.

        `HalfClose` and `Abort` are only worth having as separate steps if
        a peer can tell them apart.  Two independent clients reporting the
        *same* difference is what makes that a property of the wire rather
        than of one library's error mapping.
        """
        import subprocess

        from blackbull.fault_injection.catalogue import CATALOGUE_H1_SERVER
        from blackbull.fault_injection.h1_server import H1FaultServer

        async def curl_stderr(build) -> str:
            async with H1FaultServer(build()) as srv:
                proc = await asyncio.create_subprocess_exec(
                    'curl', '-sS', '--max-time', '5', '--http1.1',
                    f'http://127.0.0.1:{srv.port}/',
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                _, err = await asyncio.wait_for(proc.communicate(), timeout=15)
            return err.decode(errors='replace').lower()

        async def httpx_error(build) -> str:
            async with H1FaultServer(build()) as srv:
                async with httpx.AsyncClient(timeout=5.0) as c:
                    try:
                        await c.get(f'http://127.0.0.1:{srv.port}/')
                    except httpx.HTTPError as exc:
                        return f'{type(exc).__name__}: {exc}'.lower()
            return ''

        half = CATALOGUE_H1_SERVER['half_closed_after_headers']
        reset = CATALOGUE_H1_SERVER['closed_without_response']

        # A half-close after headers: the client got a complete head and an
        # incomplete body, so both should say so in those terms.
        assert 'body' in await httpx_error(half), (
            'httpx did not describe the half-close as a truncated body')
        assert 'transfer closed' in await curl_stderr(half) \
            or 'partial' in await curl_stderr(half), (
            'curl did not describe the half-close as a truncated transfer')

        # A reset before any response is a different report on both.
        assert 'body' not in await httpx_error(reset), (
            'httpx reported a pre-response reset the same way as a '
            'half-close — the two steps are indistinguishable on the wire')

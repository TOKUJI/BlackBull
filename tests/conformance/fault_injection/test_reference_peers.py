"""The third-party servers the differential cells need, and how they start.

The two preparation tests come first and are the only ones that build; every
peer waits for them (see ``_reference_peers``).
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import time

import pytest

from . import _reference_peers as rp
from ._reference_peers import NGHTTPD, NGINX, PEERS

#: One worker runs the peers, so a run starts each container once.
pytestmark = [pytest.mark.asyncio, pytest.mark.xdist_group('reference_peers')]


@pytest.fixture(autouse=True)
def _peers_not_required(monkeypatch):
    """These tests exercise the skips themselves; the differential's own
    fixtures are what BB_REQUIRE_REFERENCE_PEERS makes fatal."""
    monkeypatch.delenv('BB_REQUIRE_REFERENCE_PEERS', raising=False)


@pytest.mark.timeout(rp.BUILD_BUDGET)
async def test_the_nginx_image_is_prepared():
    """Pay for the build here, once, under a budget that admits it is a build."""
    rp.require_docker()
    NGINX.prepare()
    assert NGINX.present(), f'{NGINX.image} missing after a build that reported success'


@pytest.mark.timeout(rp.BUILD_BUDGET)
async def test_the_nghttpd_image_is_prepared():
    rp.require_docker()
    NGHTTPD.prepare()
    assert NGHTTPD.present(), f'{NGHTTPD.image} missing after a build that reported success'


async def test_the_preparers_run_before_any_peer_is_used(request):
    """A peer waits for its preparer's build, so a preparer queued after one
    on the same worker would wait out the build budget (seen in CI)."""
    items = request.session.items
    names = [item.name for item in items]
    preparers = [peer.preparer for peer in PEERS]
    if not set(preparers) <= set(names):
        pytest.skip('the preparers are not selected')
    users = [i for i, item in enumerate(items)
             if {'nginx_peer', 'nghttpd_peer'} & set(getattr(item, 'fixturenames', ()))]
    assert max(names.index(p) for p in preparers) < min(users, default=len(items))


def test_each_preparer_is_named_after_a_test_here():
    assert all(callable(globals().get(peer.preparer)) for peer in PEERS)


async def test_the_tag_follows_the_context(tmp_path):
    """The tag changes with the content and the mode, but not with the path."""
    ctx = tmp_path / 'ctx'
    ctx.mkdir()
    (ctx / 'Dockerfile').write_text('FROM nginx:1.27-alpine\n')
    (ctx / 'nginx.conf').write_text('events {}\n')
    before = rp.context_digest(ctx)

    (ctx / 'nginx.conf').write_text('events { worker_connections 4; }\n')
    assert rp.context_digest(ctx) != before

    same = tmp_path / 'elsewhere'
    same.mkdir()
    (same / 'Dockerfile').write_text('FROM nginx:1.27-alpine\n')
    (same / 'nginx.conf').write_text('events { worker_connections 4; }\n')
    assert rp.context_digest(same) == rp.context_digest(ctx)

    (ctx / 'nginx.conf').chmod(0o400)
    assert rp.context_digest(ctx) != rp.context_digest(same)


async def test_a_present_image_is_not_rebuilt(monkeypatch):
    """The build is paid once per context, not once per run."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, 'run', fake_run)
    NGINX.prepare()

    assert [c[:3] for c in calls] == [['docker', 'image', 'inspect']], calls


@pytest.mark.parametrize('peer', PEERS, ids=lambda p: p.name)
async def test_an_absent_image_is_built_for_its_context(monkeypatch, peer):
    """A miss builds this context's tag, from this context's directory."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, int(argv[1] == 'image'))

    monkeypatch.setattr(subprocess, 'run', fake_run)
    peer.prepare()

    assert calls[-1][:2] == ['docker', 'build'], calls
    assert calls[-1][3:5] == ['-t', peer.image], calls
    assert calls[-1][5] == str(peer.context), calls


async def test_a_failed_build_fails_here_and_skips_the_peers(monkeypatch, tmp_path):
    """A build failure is one red item, not a page of them and not a skip."""
    def fake_run(argv, **kwargs):
        stderr = b'' if argv[1] == 'image' else b'no such host'
        return subprocess.CompletedProcess(argv, 1, b'', stderr)

    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.setattr(NGINX, 'build_failed', tmp_path / 'failed')

    with pytest.raises(pytest.fail.Exception, match='no such host'):
        NGINX.prepare()
    assert 'no such host' in (tmp_path / 'failed').read_text()

    # And the record is what ends a peer test's wait, message included.
    with pytest.raises(pytest.skip.Exception, match='no such host'):
        NGINX.require({NGINX.preparer})


async def test_a_build_that_overruns_fails_and_records(monkeypatch, tmp_path):
    """An overrun fails too, with the daemon's last words in the reason."""
    def fake_run(argv, **kwargs):
        if argv[1] == 'image':
            return subprocess.CompletedProcess(argv, 1)
        raise subprocess.TimeoutExpired(argv, kwargs.get('timeout'), b'',
                                        b'pulling nginx:1.27-alpine')

    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.setattr(NGINX, 'build_failed', tmp_path / 'failed')

    with pytest.raises(pytest.fail.Exception, match='did not finish'):
        NGINX.prepare()
    assert 'pulling nginx:1.27-alpine' in (tmp_path / 'failed').read_text()

    with pytest.raises(pytest.skip.Exception, match='pulling nginx'):
        NGINX.require({NGINX.preparer})


async def test_a_missing_image_is_waited_for_not_rebuilt(monkeypatch, tmp_path):
    """A peer test waits for the preparation test's build, and never builds."""
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
    monkeypatch.setattr(time, 'sleep', lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(NGINX, 'build_failed', tmp_path / 'failed')

    # The build lands: the wait ends, and nothing builds beside it.
    state['present_after'] = 2
    NGINX.require({NGINX.preparer})
    assert state['polls'] == 3, state
    assert [c[1] for c in calls] == ['image'] * 3, calls
    assert clock[0] < rp.BUILD_BUDGET, clock

    # The build never lands: skip at the build's own budget, and build nothing.
    state.update(present_after=None, polls=0)
    calls.clear()
    with pytest.raises(pytest.skip.Exception):
        NGINX.require({NGINX.preparer})
    assert clock[0] >= rp.BUILD_BUDGET, clock
    assert [c[1] for c in calls] == ['image'] * state['polls'], calls


async def test_a_peer_with_no_build_to_wait_for_skips_at_once(monkeypatch, tmp_path):
    """Neither wait is owed when no build can produce the image."""
    calls = []
    clock = [0.0]

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(time, 'sleep', lambda s: clock.__setitem__(0, clock[0] + s))

    with pytest.raises(pytest.skip.Exception, match=NGINX.preparer):
        NGINX.require(set())

    failed = tmp_path / 'failed'
    failed.write_text('could not resolve the registry')
    monkeypatch.setattr(NGINX, 'build_failed', failed)
    with pytest.raises(pytest.skip.Exception, match='could not resolve'):
        NGINX.require({NGINX.preparer})

    assert clock[0] == 0.0, clock
    assert len(calls) == 3, calls
    assert [c[1] for c in calls].count('build') == 0, calls


async def test_the_failure_record_is_scoped_to_this_run(monkeypatch, tmp_path):
    """A record belongs to the run whose name it carries, and to no other."""
    assert rp.RUN_ID in NGINX.build_failed.name
    assert NGINX.build_failed != NGHTTPD.build_failed

    monkeypatch.setattr(NGINX, 'build_failed', tmp_path / 'failed')
    assert NGINX.recorded_failure() is None

    (tmp_path / 'failed').write_text('registry was down last night')
    assert NGINX.recorded_failure() == 'registry was down last night'


@pytest.mark.parametrize('family,address,v6only', [
    pytest.param(socket.AF_INET, '127.0.0.1', None, id='ipv4-loopback'),
    pytest.param(socket.AF_INET6, '::1', 1, id='ipv6-only'),
])
async def test_the_port_probe_sees_holders_of_either_family(family, address, v6only):
    """Either kind of holder blocks the Docker Desktop relay."""
    if family == socket.AF_INET6 and not socket.has_dualstack_ipv6():
        pytest.skip('no dual-stack IPv6')
    with socket.socket(family) as holder:
        if v6only is not None:
            holder.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, v6only)
        holder.bind((address, 0))
        holder.listen(1)
        with pytest.raises(OSError):
            rp.bind_probe(holder.getsockname()[1]).close()


async def test_a_chosen_port_stays_out_of_the_dynamic_range():
    """Windows allocates and reserves its own ports from 49152 up."""
    assert all(rp.free_wsl_port() < rp.DYNAMIC_PORTS_START for _ in range(200))


async def test_a_host_with_only_dynamic_ports_skips(monkeypatch):
    class _Probe:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def getsockname(self):
            return ('::', rp.DYNAMIC_PORTS_START)

    monkeypatch.setattr(rp, 'bind_probe', lambda port: _Probe())
    with pytest.raises(pytest.skip.Exception, match='ip_local_port_range'):
        rp.free_wsl_port()


def _fake_peers(monkeypatch, *, start=None, wait=None):
    """Ports 1000, 1001, ...; a started peer is named after its port."""
    ports = iter(range(1000, 1100))
    started, removed = [], []

    def fake_start(port, lifetime=rp.PEER_LIFETIME):
        if start is not None:
            start(port)
        started.append(f'c{port}')
        return started[-1]

    def fake_run(argv, **kwargs):
        removed.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(rp, 'free_wsl_port', lambda: next(ports))
    monkeypatch.setattr(NGINX, 'start', fake_start)
    monkeypatch.setattr(rp, 'wait_for_h2c', wait or (lambda *a, **k: None))
    monkeypatch.setattr(subprocess, 'run', fake_run)
    return started, removed


async def test_a_port_windows_refuses_is_replaced(monkeypatch):
    """``docker run`` fails outright on a port Windows holds or excludes."""
    def start(port):
        if port == 1000:
            raise rp.PeerStartError('port is already allocated')

    started, removed = _fake_peers(monkeypatch, start=start)
    assert NGINX.start_answering() == ('c1001', 1001)
    assert (started, removed) == (['c1001'], [])


async def test_a_peer_that_never_answers_is_replaced(monkeypatch):
    """WSL took the port between our check and the relay: the port refuses
    for the container's life, so a new port is the cure."""
    def wait(host, port, timeout):
        if port == 1000:
            raise RuntimeError('never answered')

    started, removed = _fake_peers(monkeypatch, wait=wait)
    assert NGINX.start_answering() == ('c1001', 1001)
    assert removed == ['c1000']


@pytest.mark.parametrize('failure,expected,attempts', [
    pytest.param(RuntimeError('never answered'), RuntimeError, 3, id='never-answers'),
    pytest.param(OSError('relay error'), OSError, 1, id='other-error'),
])
async def test_every_peer_that_is_not_returned_is_removed(monkeypatch, failure, expected,
                                                         attempts):
    """Bounded retries, and no container outlives a failed start."""
    def wait(*args, **kwargs):
        raise failure

    started, removed = _fake_peers(monkeypatch, wait=wait)
    with pytest.raises(expected):
        NGINX.start_answering()
    assert removed == started
    assert len(started) == attempts


async def test_peers_that_cannot_start_skip(monkeypatch):
    def start(port):
        raise rp.PeerStartError('docker daemon unreachable')

    _fake_peers(monkeypatch, start=start)
    with pytest.raises(pytest.skip.Exception, match='daemon unreachable'):
        NGINX.start_answering()


@pytest.mark.parametrize('peer', PEERS, ids=lambda p: p.name)
@pytest.mark.timeout(rp.PEER_BUDGET)
async def test_a_peer_speaks_http2_and_ends_without_its_teardown(request, peer):
    """A worker killed before teardown must not leave the peer running."""
    rp.require_docker()
    peer.require({item.name for item in request.session.items})
    container, _port = peer.start_answering(lifetime=5)
    deadline = time.monotonic() + 60
    try:
        while subprocess.run(['docker', 'ps', '-q', '--filter', f'id={container}'],
                             capture_output=True, timeout=60).stdout.strip():
            assert time.monotonic() < deadline, f'{container} outlived its lifetime'
            time.sleep(1)
    finally:
        subprocess.run(['docker', 'rm', '-f', container], capture_output=True, timeout=120)


@pytest.mark.parametrize('required,outcome', [
    pytest.param(None, pytest.skip.Exception, id='skips-by-default'),
    pytest.param('1', pytest.fail.Exception, id='fails-when-required'),
])
async def test_a_missing_peer_fails_where_the_peers_are_required(monkeypatch, required, outcome):
    """The differential job sets BB_REQUIRE_REFERENCE_PEERS: a run that compared
    nothing must not report green."""
    if required is None:
        monkeypatch.delenv('BB_REQUIRE_REFERENCE_PEERS', raising=False)
    else:
        monkeypatch.setenv('BB_REQUIRE_REFERENCE_PEERS', required)
    monkeypatch.setattr(shutil, 'which', lambda name: None)
    with pytest.raises(outcome, match='docker CLI'):
        rp.require_docker()
    monkeypatch.setattr(subprocess, 'run', lambda argv, **kw: subprocess.CompletedProcess(argv, 1))
    with pytest.raises(outcome, match=NGINX.preparer):
        NGINX.require(set())
